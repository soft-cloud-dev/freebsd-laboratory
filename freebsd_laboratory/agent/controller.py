from __future__ import annotations

import signal
import time
import uuid
from typing import Any

from .evidence import AgentEvidenceEvent, AgentEvidenceLog, make_command_event, utc_now
from .model import AgentModel
from .policy import AgentPolicy
from .runtime import AgentRuntime
from .types import BoundedOutput, Command, FinalAnswer, Observation, RuntimeHandle


class AgentController:
    """Orchestrates model action proposals, policy authorization, runtime execution, and evidence logging."""

    def __init__(
        self,
        model: AgentModel,
        runtime: AgentRuntime,
        policy: AgentPolicy,
        evidence_log: AgentEvidenceLog | None = None,
    ) -> None:
        self.model = model
        self.runtime = runtime
        self.policy = policy
        self.evidence_log = evidence_log

    def _emit_session_event(
        self,
        event_name: str,
        session_id: str,
        handle: RuntimeHandle,
        step: int,
    ) -> None:
        if self.evidence_log is None:
            return
        event = AgentEvidenceEvent(
            event=event_name,
            session_id=session_id,
            runtime_id=handle.runtime_name,
            runtime_type=handle.runtime_type,
            step=step,
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
        self.evidence_log.emit(event)

    def run(self, goal: str) -> str:
        handle = self.runtime.create()
        session_id = uuid.uuid4().hex
        self._emit_session_event("agent-session-start", session_id, handle, step=0)

        prev_sigint = None
        prev_sigterm = None

        def _handle_interrupt(signum: int, frame: Any) -> None:
            self.runtime.destroy(handle)
            raise KeyboardInterrupt(f"Agent received signal {signum}")

        try:
            try:
                prev_sigint = signal.signal(signal.SIGINT, _handle_interrupt)
                prev_sigterm = signal.signal(signal.SIGTERM, _handle_interrupt)
            except (ValueError, OSError):
                pass

            observations: list[Observation] = []
            start_time = time.monotonic()

            for step in range(self.policy.max_steps):
                elapsed = time.monotonic() - start_time
                if elapsed >= self.policy.max_runtime_seconds:
                    self._emit_session_event(
                        "agent-session-end", session_id, handle, step=step
                    )
                    return "Agent session deadline reached."

                action = self.model.next_action(goal, observations)

                if isinstance(action, FinalAnswer):
                    self._emit_session_event(
                        "agent-session-end", session_id, handle, step=step
                    )
                    return action.answer

                if not isinstance(action, Command):
                    self._emit_session_event(
                        "agent-session-end", session_id, handle, step=step
                    )
                    return "Agent produced an unparseable action."

                decision = self.policy.authorize(action.command, step, elapsed)
                if not decision.authorized:
                    reason_bytes = decision.reason.encode("utf-8")
                    rejection_obs = Observation(
                        step=step,
                        command=action.command,
                        exit_status=-1,
                        stdout=BoundedOutput(b"", b"", 0, False),
                        stderr=BoundedOutput(
                            head=reason_bytes,
                            tail=b"",
                            total_bytes=len(reason_bytes),
                            truncated=False,
                        ),
                        duration_ms=0,
                    )
                    observations.append(rejection_obs)
                    if self.evidence_log is not None:
                        self.evidence_log.emit(
                            make_command_event(
                                session_id,
                                handle.runtime_name,
                                handle.runtime_type,
                                rejection_obs,
                            )
                        )
                    continue

                raw_obs = self.runtime.execute(handle, action.command)
                obs = Observation(
                    step=step,
                    command=raw_obs.command,
                    exit_status=raw_obs.exit_status,
                    stdout=raw_obs.stdout,
                    stderr=raw_obs.stderr,
                    duration_ms=raw_obs.duration_ms,
                )
                observations.append(obs)

                if self.evidence_log is not None:
                    self.evidence_log.emit(
                        make_command_event(
                            session_id,
                            handle.runtime_name,
                            handle.runtime_type,
                            obs,
                        )
                    )

            self._emit_session_event(
                "agent-session-end", session_id, handle, step=self.policy.max_steps
            )
            return "Agent reached maximum step limit without a final answer."
        finally:
            try:
                if prev_sigint is not None:
                    signal.signal(signal.SIGINT, prev_sigint)
                if prev_sigterm is not None:
                    signal.signal(signal.SIGTERM, prev_sigterm)
            except (ValueError, OSError):
                pass
            self.runtime.destroy(handle)
