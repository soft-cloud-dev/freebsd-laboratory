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

    def test_load_ipython_extension(self) -> None:
        mock_ip = MagicMock()
        load_ipython_extension(mock_ip)
        mock_ip.register_magics.assert_called_once_with(FreeBSDLabMagics)


if __name__ == "__main__":
    unittest.main()
