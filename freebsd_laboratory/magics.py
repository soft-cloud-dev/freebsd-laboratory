"""IPython and Jupyter Notebook Magics for FreeBSD Laboratory AI inference."""

from __future__ import annotations

import argparse
import shlex
from typing import Any

try:
    from IPython.core.magic import Magics, cell_magic, line_cell_magic, line_magic, magics_class
    from IPython.display import Markdown, display
except ImportError:
    # Graceful fallback when IPython is not installed
    class Magics:  # type: ignore[no-redef]
        def __init__(self, shell: Any = None) -> None:
            self.shell = shell

    def magics_class(cls: Any) -> Any:
        return cls

    def line_magic(func: Any) -> Any:
        return func

    def cell_magic(func: Any) -> Any:
        return func

    def line_cell_magic(func: Any) -> Any:
        return func

    def display(*args: Any, **kwargs: Any) -> None:
        pass

    class Markdown:  # type: ignore[no-redef]
        def __init__(self, data: str) -> None:
            self.data = data

from freebsd_laboratory import ai


@magics_class
class FreeBSDLabMagics(Magics):
    """Jupyter notebook magics for FreeBSD Laboratory AI inference and agent operations."""

    def _parse_ai_args(self, line: str) -> tuple[argparse.Namespace, str]:
        parser = argparse.ArgumentParser(prog="%ai", add_help=False)
        parser.add_argument("--model", default=None, help="Path to GGUF model")
        parser.add_argument("--tokens", type=int, default=512, help="Max tokens to generate")
        parser.add_argument("--temp", type=float, default=0.0, help="Sampling temperature")
        parser.add_argument("--system", default=None, help="Custom system prompt")

        args, remaining = parser.parse_known_args(shlex.split(line))
        return args, " ".join(remaining)

    def _parse_agent_args(self, line: str) -> tuple[argparse.Namespace, str]:
        parser = argparse.ArgumentParser(prog="%agent", add_help=False)
        parser.add_argument("--mode", choices=["bhyve", "jail"], default="bhyve", help="Runtime mode")
        parser.add_argument("--model", default=None, help="Path to GGUF model")
        parser.add_argument("--steps", type=int, default=16, help="Max agent steps")
        parser.add_argument("--runtime", type=int, default=300, help="Max runtime seconds")

        args, remaining = parser.parse_known_args(shlex.split(line))
        return args, " ".join(remaining)

    @line_cell_magic
    def ai(self, line: str = "", cell: str | None = None) -> Any:
        """Run local AI inference from a notebook line or cell.

        Examples:
            %ai What are VNET jails in FreeBSD?
            %%ai --tokens 256
            Explain the difference between UFS and ZFS.
        """
        args, remaining = self._parse_ai_args(line)
        prompt = cell if cell is not None else remaining
        if not prompt or not prompt.strip():
            print("Usage: %ai <prompt> or %%ai [options]\\n<prompt>")
            return None

        result_md = ai.ask(
            prompt=prompt.strip(),
            model_path=args.model,
            max_tokens=args.tokens,
            temperature=args.temp,
            system_prompt=args.system or "You are a knowledgeable FreeBSD and Unix systems engineering assistant.",
        )
        display(result_md)
        return None

    @line_cell_magic
    def agent(self, line: str = "", cell: str | None = None) -> Any:
        """Launch an autonomous troubleshooting agent in an isolated runtime from a notebook.

        Examples:
            %agent Check available disk space on zpools
            %%agent --mode bhyve --steps 8
            Inspect network interfaces and verify bridge0 IP address
        """
        args, remaining = self._parse_agent_args(line)
        goal = cell if cell is not None else remaining
        if not goal or not goal.strip():
            print("Usage: %agent <goal> or %%agent [options]\\n<goal>")
            return None

        print(f"\033[1;36m[FreeBSD Laboratory Agent]\033[0m Starting autonomous task in \033[1m{args.mode}\033[0m runtime...")
        output = ai.run_agent(
            goal=goal.strip(),
            mode=args.mode,
            model_path=args.model,
            max_steps=args.steps,
            max_runtime=args.runtime,
        )
        display(Markdown(f"### Agent Diagnostic Report\n\n```text\n{output}\n```"))
        return None

    @line_magic
    def ai_summary(self, line: str = "") -> Any:
        """Display summary card of cumulative token usage in the current kernel session."""
        display(ai.token_summary())
        return None

    @line_magic
    def ai_usage(self, line: str = "") -> Any:
        """Display summary card of cumulative token usage in the current kernel session."""
        display(ai.token_summary())
        return None

    @line_magic
    def ai_reset(self, line: str = "") -> Any:
        """Reset the session token usage counters."""
        ai.reset_token_usage()
        print("Session token usage counter reset to 0.")
        return None


def load_ipython_extension(ipython: Any) -> None:
    """Register magics when extension is loaded via %load_ext freebsd_laboratory.magics."""
    ipython.register_magics(FreeBSDLabMagics)
