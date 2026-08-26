"""Unit tests for in-notebook AI inference and IPython magics."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from freebsd_laboratory import ai
from freebsd_laboratory.magics import FreeBSDLabMagics, load_ipython_extension


class TestAIModule(unittest.TestCase):
    def test_list_models_nonexistent_dir(self) -> None:
        models = ai.list_models("/nonexistent/directory")
        self.assertEqual(models, [])

    def test_get_cached_llama_symlink_rejected(self) -> None:
        with patch("freebsd_laboratory.ai.resolve_default_model", return_value="/tmp/symlink.gguf"), \
             patch("pathlib.Path.is_symlink", return_value=True), \
             patch("pathlib.Path.is_file", return_value=True):
            with self.assertRaises(RuntimeError) as ctx:
                ai.get_cached_llama("/tmp/symlink.gguf")
            self.assertIn("symlink", str(ctx.exception).lower())

    def test_generate_calls_llama(self) -> None:
        mock_llm = MagicMock()
        mock_llm.create_chat_completion.return_value = {
            "choices": [{"message": {"content": "FreeBSD uses ZFS and UFS."}}]
        }

        with patch("freebsd_laboratory.ai.get_cached_llama", return_value=mock_llm):
            result = ai.generate("Explain FreeBSD storage", max_tokens=128)
            self.assertEqual(result, "FreeBSD uses ZFS and UFS.")
            mock_llm.create_chat_completion.assert_called_once()

    def test_ask_returns_markdown(self) -> None:
        mock_llm = MagicMock()
        mock_llm.create_chat_completion.return_value = {
            "choices": [{"message": {"content": "### Header\nContent"}}]
        }

        with patch("freebsd_laboratory.ai.get_cached_llama", return_value=mock_llm):
            result = ai.ask("Explain sysctl")
            # In non-notebook environment or with IPython, either Markdown object or text is returned
            self.assertTrue(hasattr(result, "data") or isinstance(result, str))


class TestMagics(unittest.TestCase):
    def setUp(self) -> None:
        self.shell = MagicMock()
        self.magics = FreeBSDLabMagics(shell=self.shell)

    def test_ai_line_magic(self) -> None:
        with patch("freebsd_laboratory.ai.ask", return_value="Result Markdown") as mock_ask, \
             patch("freebsd_laboratory.magics.display") as mock_display:
            self.magics.ai("What is VNET?")
            mock_ask.assert_called_once()
            self.assertIn("What is VNET?", mock_ask.call_args[1]["prompt"])
            mock_display.assert_called_once_with("Result Markdown")

    def test_ai_cell_magic(self) -> None:
        with patch("freebsd_laboratory.ai.ask", return_value="Result Markdown") as mock_ask, \
             patch("freebsd_laboratory.magics.display") as mock_display:
            self.magics.ai("--tokens 256", "Write a python script\nfor sysctl")
            mock_ask.assert_called_once()
            self.assertEqual(mock_ask.call_args[1]["max_tokens"], 256)
            self.assertEqual(mock_ask.call_args[1]["prompt"], "Write a python script\nfor sysctl")

    def test_agent_cell_magic(self) -> None:
        with patch("freebsd_laboratory.ai.run_agent", return_value="Diagnostic OK") as mock_run, \
             patch("freebsd_laboratory.magics.display") as mock_display:
            self.magics.agent("--mode jail --steps 5", "Check disk space")
            mock_run.assert_called_once()
            self.assertEqual(mock_run.call_args[1]["mode"], "jail")
            self.assertEqual(mock_run.call_args[1]["max_steps"], 5)
            self.assertEqual(mock_run.call_args[1]["goal"], "Check disk space")

    def test_token_tracking_and_summary(self) -> None:
        ai.reset_token_usage()
        u0 = ai.token_usage()
        self.assertEqual(u0["total_tokens"], 0)
        self.assertEqual(u0["requests"], 0)

        with patch("freebsd_laboratory.ai.get_cached_llama") as mock_get:
            mock_llm = MagicMock()
            mock_llm.create_chat_completion.return_value = {
                "choices": [{"message": {"content": "Test answer"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 25, "total_tokens": 35},
            }
            mock_get.return_value = mock_llm

            res = ai.generate("Test prompt")
            self.assertEqual(res, "Test answer")

            u1 = ai.token_usage()
            self.assertEqual(u1["prompt_tokens"], 10)
            self.assertEqual(u1["completion_tokens"], 25)
            self.assertEqual(u1["total_tokens"], 35)
            self.assertEqual(u1["requests"], 1)

            summary = ai.token_summary()
            self.assertIsNotNone(summary)

    def test_token_tracker_security_and_sync(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            usage_file = Path(td) / "token_usage.json"
            tracker_a = ai.TokenUsageTracker(usage_file=usage_file)
            tracker_b = ai.TokenUsageTracker(usage_file=usage_file)

            # Test bool-before-int type guard
            with self.assertRaises(TypeError):
                tracker_a.record(True, 10, 1.0)  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                tracker_a.record(10, False, 1.0)  # type: ignore[arg-type]
            with self.assertRaises(TypeError):
                tracker_a.record(10, 20, True)  # type: ignore[arg-type]

            # Record in tracker_a
            tracker_a.record(100, 50, 2.5)
            self.assertEqual(tracker_a.prompt_tokens, 100)
            self.assertEqual(tracker_a.completion_tokens, 50)
            self.assertEqual(tracker_a.total_tokens, 150)

            # Tracker_b reads file
            summary_b = tracker_b.summary_dict()
            self.assertEqual(summary_b["total_tokens"], 150)
            self.assertEqual(summary_b["prompt_tokens"], 100)
            self.assertEqual(summary_b["completion_tokens"], 50)
            self.assertEqual(summary_b["requests"], 1)

            # Reset
            tracker_a.reset()
            self.assertEqual(tracker_a.total_tokens, 0)
            self.assertEqual(tracker_b.summary_dict()["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
