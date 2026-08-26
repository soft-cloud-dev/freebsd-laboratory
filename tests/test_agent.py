from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from freebsd_laboratory.agent.bounded_exec import bounded_exec
from freebsd_laboratory.agent.controller import AgentController
from freebsd_laboratory.agent.evidence import (
    AgentEvidenceEvent,
    AgentEvidenceLog,
    canonical_json,
    make_command_event,
    sha256_bytes,
    utc_now,
)
from freebsd_laboratory.agent.model import (
    AgentModel,
    _truncate_stream_for_model,
    parse_action,
)
from freebsd_laboratory.agent.policy import AgentPolicy, Decision
from freebsd_laboratory.agent.runtime import AgentRuntime, generate_agent_runtime_name
from freebsd_laboratory.agent.types import (
    BoundedOutput,
    Command,
    FinalAnswer,
    Observation,
    RuntimeHandle,
)
from freebsd_laboratory.ssh_transport import create_runtime_ssh_key


class TestProtocolParsing(unittest.TestCase):
    def test_parse_command_action(self) -> None:
        action = parse_action("COMMAND: ls -la /tmp")
        self.assertIsInstance(action, Command)
        self.assertEqual(action.command, "ls -la /tmp")

    def test_parse_command_with_leading_whitespace(self) -> None:
        action = parse_action("   COMMAND:   pwd   ")
        self.assertIsInstance(action, Command)
        self.assertEqual(action.command, "pwd")

    def test_parse_final_action(self) -> None:
        action = parse_action("FINAL: Task completed successfully.")
        self.assertIsInstance(action, FinalAnswer)
        self.assertEqual(action.answer, "Task completed successfully.")

    def test_parse_empty_response(self) -> None:
        action = parse_action("   \n\t  ")
        self.assertIsInstance(action, FinalAnswer)

    def test_parse_multiline_with_command(self) -> None:
        text = "Here is the command to run:\nCOMMAND: uname -a\nHope this helps."
        action = parse_action(text)
        self.assertIsInstance(action, Command)
        self.assertEqual(action.command, "uname -a")

    def test_parse_single_line_fallback_command(self) -> None:
        action = parse_action("df -h")
        self.assertIsInstance(action, Command)
        self.assertEqual(action.command, "df -h")

    def test_parse_multiline_fallback_final(self) -> None:
        text = "I have inspected the system.\nEverything is fine."
        action = parse_action(text)
        self.assertIsInstance(action, FinalAnswer)
        self.assertEqual(action.answer, text)


class TestAgentPolicy(unittest.TestCase):
    def test_policy_authorize_valid(self) -> None:
        policy = AgentPolicy(max_command_bytes=4096, max_steps=16, max_runtime_seconds=300)
        decision = policy.authorize("ls -la", step=0, elapsed=1.5)
        self.assertTrue(decision.authorized)
        self.assertEqual(decision.reason, "")

    def test_policy_reject_empty(self) -> None:
        policy = AgentPolicy()
        decision = policy.authorize("   ", step=0, elapsed=1.0)
        self.assertFalse(decision.authorized)
        self.assertIn("empty", decision.reason)

    def test_policy_reject_oversized(self) -> None:
        policy = AgentPolicy(max_command_bytes=100)
        long_cmd = "echo " + "a" * 120
        decision = policy.authorize(long_cmd, step=0, elapsed=1.0)
        self.assertFalse(decision.authorized)
        self.assertIn("exceeds limit", decision.reason)

    def test_policy_reject_step_limit(self) -> None:
        policy = AgentPolicy(max_steps=10)
        decision = policy.authorize("ls", step=10, elapsed=1.0)
        self.assertFalse(decision.authorized)
        self.assertIn("step limit", decision.reason)

    def test_policy_reject_deadline(self) -> None:
        policy = AgentPolicy(max_runtime_seconds=30)
        decision = policy.authorize("ls", step=1, elapsed=35.0)
        self.assertFalse(decision.authorized)
        self.assertIn("session deadline", decision.reason)

    def test_policy_max_steps_ceiling(self) -> None:
        with self.assertRaises(ValueError):
            AgentPolicy(max_steps=30)  # ceiling is 25

    def test_policy_max_steps_bool_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AgentPolicy(max_steps=True)  # bool before int guard

    def test_policy_destructive_command_allowed(self) -> None:
        # No destructive-command denylist: guest is disposable
        policy = AgentPolicy()
        decision = policy.authorize("rm -rf /", step=0, elapsed=1.0)
        self.assertTrue(decision.authorized)


class TestBoundedExecution(unittest.TestCase):
    def test_bounded_output_small(self) -> None:
        exit_code, stdout_out, stderr_out = bounded_exec(
            [sys.executable, "-c", "print('hello world')"],
            timeout=10.0,
            head_limit=4096,
            tail_limit=4096,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout_out.head.strip(), b"hello world")
        self.assertEqual(stdout_out.tail, b"")
        self.assertFalse(stdout_out.truncated)
        self.assertEqual(stdout_out.total_bytes, len(b"hello world\n"))

    def test_bounded_output_large(self) -> None:
        # Generate 100 KiB of output
        code = "import sys; sys.stdout.write('A' * 4096 + 'M' * 90000 + 'Z' * 4096)"
        exit_code, stdout_out, stderr_out = bounded_exec(
            [sys.executable, "-c", code],
            timeout=10.0,
            head_limit=4096,
            tail_limit=4096,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(stdout_out.head), 4096)
        self.assertEqual(stdout_out.head, b"A" * 4096)
        self.assertEqual(len(stdout_out.tail), 4096)
        self.assertEqual(stdout_out.tail, b"Z" * 4096)
        self.assertEqual(stdout_out.total_bytes, 4096 + 90000 + 4096)
        self.assertTrue(stdout_out.truncated)

    def test_bounded_exec_timeout(self) -> None:
        code = "import time; time.sleep(10)"
        exit_code, stdout_out, stderr_out = bounded_exec(
            [sys.executable, "-c", code],
            timeout=0.2,
        )
        self.assertEqual(exit_code, -1)

    def test_bounded_exec_stdin_devnull(self) -> None:
        # sys.stdin.read() with DEVNULL returns immediately (EOF)
        code = "import sys; sys.stdout.write(f'STDIN_EMPTY:{len(sys.stdin.read())}')"
        exit_code, stdout_out, stderr_out = bounded_exec(
            [sys.executable, "-c", code],
            timeout=5.0,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout_out.head, b"STDIN_EMPTY:0")

    def test_bounded_exec_concurrent_draining(self) -> None:
        # Emit large chunks to both stdout and stderr simultaneously
        code = (
            "import sys\n"
            "sys.stdout.write('O' * 70000)\n"
            "sys.stderr.write('E' * 70000)\n"
            "sys.stdout.flush()\n"
            "sys.stderr.flush()\n"
        )
        exit_code, stdout_out, stderr_out = bounded_exec(
            [sys.executable, "-c", code],
            timeout=10.0,
            head_limit=2048,
            tail_limit=2048,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout_out.total_bytes, 70000)
        self.assertEqual(stderr_out.total_bytes, 70000)
        self.assertTrue(stdout_out.truncated)
        self.assertTrue(stderr_out.truncated)


class TestAgentEvidence(unittest.TestCase):
    def test_evidence_event_no_raw_content(self) -> None:
        obs = Observation(
            step=1,
            command="secret_key=12345 && run_command",
            exit_status=0,
            stdout=BoundedOutput(head=b"sensitive output data", tail=b"", total_bytes=21, truncated=False),
            stderr=BoundedOutput(head=b"", tail=b"", total_bytes=0, truncated=False),
            duration_ms=42,
        )
        event = make_command_event("sess-1", "freebsd-lab-a1", "bhyve", obs)
        serialized = canonical_json(event.__dict__).decode("utf-8")

        # Crucial security check: raw command and stdout text MUST NOT appear in serialized event
        self.assertNotIn("secret_key", serialized)
        self.assertNotIn("sensitive output data", serialized)

        # Hashes and metrics must be present
        self.assertEqual(event.command_sha256, sha256_bytes(b"secret_key=12345 && run_command"))
        self.assertEqual(event.stdout_sha256, sha256_bytes(b"sensitive output data"))
        self.assertEqual(event.stdout_bytes, 21)
        self.assertEqual(event.duration_ms, 42)

    def test_evidence_log_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "evidence.jsonl"
            log = AgentEvidenceLog(log_path, fsync=False)
            try:
                event = AgentEvidenceEvent(
                    event="agent-session-start",
                    session_id="s1",
                    runtime_id="r1",
                    runtime_type="bhyve",
                    step=0,
                    command_sha256="",
                    exit_status=0,
                    stdout_sha256="",
                    stdout_bytes=0,
                    stderr_sha256="",
                    stderr_bytes=0,
                    duration_ms=0,
                    truncated=False,
                    timestamp=utc_now(),
                )
                log.emit(event)
            finally:
                log.close()

            self.assertTrue(log_path.is_file())
            mode = stat.S_IMODE(log_path.stat().st_mode)
            self.assertEqual(mode, 0o600)

            content = log_path.read_text(encoding="utf-8")
            self.assertIn('"event":"agent-session-start"', content)

    def test_evidence_log_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            real_file = Path(temp_dir) / "real.jsonl"
            real_file.touch()
            symlink_file = Path(temp_dir) / "symlink.jsonl"
            symlink_file.symlink_to(real_file)

            with self.assertRaises(RuntimeError):
                AgentEvidenceLog(symlink_file)


class TestTypeGuardsAndImmutability(unittest.TestCase):
    def test_observation_frozen(self) -> None:
        obs = Observation(
            step=0,
            command="ls",
            exit_status=0,
            stdout=BoundedOutput(b"", b"", 0, False),
            stderr=BoundedOutput(b"", b"", 0, False),
            duration_ms=10,
        )
        with self.assertRaises((AttributeError, TypeError)):
            obs.step = 1  # type: ignore

    def test_observation_bool_type_guard(self) -> None:
        with self.assertRaises(TypeError):
            Observation(
                step=True,  # type: ignore # bool before int guard
                command="ls",
                exit_status=0,
                stdout=BoundedOutput(b"", b"", 0, False),
                stderr=BoundedOutput(b"", b"", 0, False),
                duration_ms=10,
            )

    def test_runtime_handle_frozen(self) -> None:
        handle = RuntimeHandle(
            runtime_name="freebsd-lab-a123",
            guest_ip="172.31.254.10",
            runtime_type="bhyve",
            private_key=Path("/tmp/k"),
            known_hosts_file=Path("/tmp/kh"),
        )
        with self.assertRaises((AttributeError, TypeError)):
            handle.guest_ip = "1.2.3.4"  # type: ignore


class TestSharedSSHKeyHelper(unittest.TestCase):
    def test_create_runtime_ssh_key_generates_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dir_path = Path(temp_dir) / "runtime_01"
            dir_path.mkdir(mode=0o700)

            private_key_path, pub_material = create_runtime_ssh_key(dir_path)

            self.assertTrue(private_key_path.is_file())
            self.assertFalse(private_key_path.is_symlink())
            self.assertEqual(stat.S_IMODE(private_key_path.stat().st_mode), 0o600)

            public_key_path = dir_path / "id_ed25519.pub"
            self.assertTrue(public_key_path.is_file())
            self.assertFalse(public_key_path.is_symlink())
            self.assertEqual(stat.S_IMODE(public_key_path.stat().st_mode), 0o600)

            self.assertTrue(pub_material.startswith("ssh-ed25519 "))
            self.assertNotIn("\n", pub_material)

    def test_create_runtime_ssh_key_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            real_dir = Path(temp_dir) / "real"
            real_dir.mkdir()
            symlink_dir = Path(temp_dir) / "symlink"
            symlink_dir.symlink_to(real_dir)

            with self.assertRaises(RuntimeError):
                create_runtime_ssh_key(symlink_dir)


class TestControllerIntegration(unittest.TestCase):
    def test_controller_reaches_final(self) -> None:
        mock_model = MagicMock()
        mock_model.next_action.side_effect = [
            Command("uname -s"),
            FinalAnswer("OS is FreeBSD"),
        ]

        mock_runtime = MagicMock()
        mock_handle = RuntimeHandle(
            runtime_name="freebsd-lab-a123",
            guest_ip="172.31.254.10",
            runtime_type="bhyve",
            private_key=Path("/tmp/k"),
            known_hosts_file=Path("/tmp/kh"),
        )
        mock_runtime.create.return_value = mock_handle
        mock_runtime.execute.return_value = Observation(
            step=0,
            command="uname -s",
            exit_status=0,
            stdout=BoundedOutput(b"FreeBSD\n", b"", 8, False),
            stderr=BoundedOutput(b"", b"", 0, False),
            duration_ms=50,
        )

        policy = AgentPolicy(max_steps=5, max_runtime_seconds=60)
        controller = AgentController(mock_model, mock_runtime, policy)

        result = controller.run("Check OS name")
        self.assertEqual(result, "OS is FreeBSD")
        mock_runtime.destroy.assert_called_once_with(mock_handle)

    def test_controller_respects_max_steps(self) -> None:
        mock_model = MagicMock()
        mock_model.next_action.return_value = Command("echo loop")

        mock_runtime = MagicMock()
        mock_handle = RuntimeHandle(
            runtime_name="freebsd-lab-a123",
            guest_ip="172.31.254.10",
            runtime_type="bhyve",
            private_key=Path("/tmp/k"),
            known_hosts_file=Path("/tmp/kh"),
        )
        mock_runtime.create.return_value = mock_handle
        mock_runtime.execute.return_value = Observation(
            step=0,
            command="echo loop",
            exit_status=0,
            stdout=BoundedOutput(b"loop\n", b"", 5, False),
            stderr=BoundedOutput(b"", b"", 0, False),
            duration_ms=10,
        )

        policy = AgentPolicy(max_steps=3, max_runtime_seconds=60)
        controller = AgentController(mock_model, mock_runtime, policy)

        result = controller.run("Infinite loop task")
        self.assertIn("maximum step limit", result)
        self.assertEqual(mock_runtime.execute.call_count, 3)
        mock_runtime.destroy.assert_called_once_with(mock_handle)

    def test_controller_respects_deadline(self) -> None:
        mock_model = MagicMock()
        mock_model.next_action.return_value = Command("sleep 2")

        mock_runtime = MagicMock()
        mock_handle = RuntimeHandle(
            runtime_name="freebsd-lab-a123",
            guest_ip="172.31.254.10",
            runtime_type="bhyve",
            private_key=Path("/tmp/k"),
            known_hosts_file=Path("/tmp/kh"),
        )
        mock_runtime.create.return_value = mock_handle

        def slow_execute(h: RuntimeHandle, cmd: str) -> Observation:
            time.sleep(0.3)
            return Observation(
                step=0,
                command=cmd,
                exit_status=0,
                stdout=BoundedOutput(b"", b"", 0, False),
                stderr=BoundedOutput(b"", b"", 0, False),
                duration_ms=300,
            )

        mock_runtime.execute.side_effect = slow_execute

        # Set 0.25s deadline with 10 max steps
        policy = AgentPolicy(max_steps=10, max_runtime_seconds=0.25)
        controller = AgentController(mock_model, mock_runtime, policy)

        result = controller.run("Slow task")
        self.assertIn("session deadline reached", result)
        mock_runtime.destroy.assert_called_once_with(mock_handle)

    def test_controller_cleanup_on_exception(self) -> None:
        mock_model = MagicMock()
        mock_model.next_action.side_effect = RuntimeError("Inference engine crashed")

        mock_runtime = MagicMock()
        mock_handle = RuntimeHandle(
            runtime_name="freebsd-lab-a123",
            guest_ip="172.31.254.10",
            runtime_type="bhyve",
            private_key=Path("/tmp/k"),
            known_hosts_file=Path("/tmp/kh"),
        )
        mock_runtime.create.return_value = mock_handle

        policy = AgentPolicy()
        controller = AgentController(mock_model, mock_runtime, policy)

        with self.assertRaises(RuntimeError):
            controller.run("Crash task")

        mock_runtime.destroy.assert_called_once_with(mock_handle)

    def test_controller_policy_rejection_fed_back(self) -> None:
        mock_model = MagicMock()
        # Step 0: oversized command; Step 1: final answer
        mock_model.next_action.side_effect = [
            Command("echo " + "x" * 5000),
            FinalAnswer("Recovered after rejection"),
        ]

        mock_runtime = MagicMock()
        mock_handle = RuntimeHandle(
            runtime_name="freebsd-lab-a123",
            guest_ip="172.31.254.10",
            runtime_type="bhyve",
            private_key=Path("/tmp/k"),
            known_hosts_file=Path("/tmp/kh"),
        )
        mock_runtime.create.return_value = mock_handle

        policy = AgentPolicy(max_command_bytes=100)
        controller = AgentController(mock_model, mock_runtime, policy)

        result = controller.run("Oversized task")
        self.assertEqual(result, "Recovered after rejection")
        # execute should NOT have been called for the rejected command
        mock_runtime.execute.assert_not_called()
        mock_runtime.destroy.assert_called_once_with(mock_handle)


class TestAgentModelSystemRoleFallback(unittest.TestCase):
    def test_system_role_rejection_merges_into_first_user_turn(self) -> None:
        model = object.__new__(AgentModel)
        model.llm = MagicMock()
        model.n_ctx = 2048
        model._token_count = lambda text: len(text.split())

        # First call with system role raises ValueError (like Gemma 2 chat template)
        # Second call with merged system prompt into user turn succeeds
        def fake_create_chat_completion(messages: list[dict[str, str]]) -> dict:
            for m in messages:
                if m["role"] == "system":
                    raise ValueError("System role not supported")
            return {"choices": [{"message": {"content": "COMMAND: sysctl hw.model"}}]}

        model.llm.create_chat_completion.side_effect = fake_create_chat_completion

        action = model.next_action("Inspect CPU model", [])
        self.assertIsInstance(action, Command)
        self.assertEqual(action.command, "sysctl hw.model")
        # Should have called create_chat_completion twice: initial + retry with merged user message
        self.assertEqual(model.llm.create_chat_completion.call_count, 2)
        second_call_messages = model.llm.create_chat_completion.call_args_list[1][1]["messages"]
        self.assertEqual(len(second_call_messages), 1)
        self.assertEqual(second_call_messages[0]["role"], "user")
        self.assertIn("GOAL: Inspect CPU model", second_call_messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
