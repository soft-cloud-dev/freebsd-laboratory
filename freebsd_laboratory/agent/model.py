from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .types import Action, BoundedOutput, Command, FinalAnswer, Observation

if TYPE_CHECKING:
    from llama_cpp import Llama

SYSTEM_PROMPT = """\
You control one disposable FreeBSD runtime.
Return exactly one action per turn.

COMMAND: <single shell action>
or:
FINAL: <task result>

Rules:
- One action per turn. No background processes.
- Do not use interactive commands (vi, less, top, etc).
- When the task is complete, use FINAL: with your result.
"""

MODEL_CONTEXT_STDOUT_LIMIT = 1024
MODEL_CONTEXT_STDERR_LIMIT = 1024
GENERATION_RESERVE_TOKENS = 256


def _truncate_stream_for_model(bounded: BoundedOutput, limit_per_side: int = 1024) -> str:
    if not bounded.head and not bounded.tail:
        return ""
    if not bounded.tail:
        # If output was not split into head+tail, take up to 2 * limit_per_side bytes
        max_bytes = limit_per_side * 2
        content = bounded.head[:max_bytes].decode("utf-8", errors="replace")
        if bounded.total_bytes > max_bytes:
            content += f"\n... [TRUNCATED {bounded.total_bytes} bytes total] ..."
        return content

    head_sub = bounded.head[:limit_per_side].decode("utf-8", errors="replace")
    tail_sub = bounded.tail[-limit_per_side:].decode("utf-8", errors="replace")
    return f"{head_sub}\n... [TRUNCATED {bounded.total_bytes} bytes total] ...\n{tail_sub}"


def parse_action(text: str) -> Action:
    """Parse raw LLM response text into a Command or FinalAnswer action."""
    stripped = text.strip()
    if not stripped:
        return FinalAnswer("Task complete (empty response).")

    # Look line-by-line for action prefixes
    for line in stripped.splitlines():
        line_clean = line.strip()
        if line_clean.upper().startswith("COMMAND:"):
            cmd = line_clean[len("COMMAND:") :].strip()
            if cmd:
                return Command(cmd)
        elif line_clean.upper().startswith("FINAL:"):
            ans = line_clean[len("FINAL:") :].strip()
            return FinalAnswer(ans or stripped)

    # If first line looks like a single command without prompt decoration
    first_line = stripped.splitlines()[0].strip()
    if len(stripped.splitlines()) == 1 and not first_line.startswith(("#", "//", "I ", "Let ")):
        return Command(first_line)

    return FinalAnswer(stripped)


class AgentModel:
    """Interface to a local GGUF model via llama-cpp-python for action generation."""

    def __init__(
        self,
        model_path: str | Path,
        n_ctx: int = 2048,
        n_gpu_layers: int = 0,
    ) -> None:
        if isinstance(n_ctx, bool) or not isinstance(n_ctx, int) or n_ctx < 256:
            raise ValueError("n_ctx must be an integer >= 256")
        if isinstance(n_gpu_layers, bool) or not isinstance(n_gpu_layers, int) or n_gpu_layers < 0:
            raise ValueError("n_gpu_layers must be a non-negative integer")

        model_file = Path(model_path)
        if model_file.is_symlink() or not model_file.is_file():
            raise RuntimeError(f"GGUF model must be a regular file, not a symlink: {model_path}")
        if not os.access(model_file, os.R_OK):
            raise RuntimeError(f"GGUF model file is not readable: {model_path}")

        try:
            from llama_cpp import Llama
        except ImportError as exc:
            raise RuntimeError(
                "llama-cpp-python is required for AgentModel. "
                "Install it with: pip install 'freebsd-laboratory[agent]'"
            ) from exc

        self.model_path = str(model_file)
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.llm: Llama = Llama(
            model_path=self.model_path,
            n_ctx=n_ctx,
            n_gpu_layers=n_gpu_layers,
            verbose=False,
        )

    def _token_count(self, text: str) -> int:
        return len(self.llm.tokenize(text.encode("utf-8")))

    def _build_messages(
        self, goal: str, observations: list[Observation]
    ) -> list[dict[str, str]]:
        base_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"GOAL: {goal}"},
        ]
        fixed_cost = sum(self._token_count(m["content"]) for m in base_messages)
        budget = self.n_ctx - fixed_cost - GENERATION_RESERVE_TOKENS

        pending_pairs: list[tuple[dict[str, str], dict[str, str]]] = []
        for obs in reversed(observations):
            stdout_text = _truncate_stream_for_model(
                obs.stdout, MODEL_CONTEXT_STDOUT_LIMIT
            )
            stderr_text = _truncate_stream_for_model(
                obs.stderr, MODEL_CONTEXT_STDERR_LIMIT
            )
            assistant_msg = {"role": "assistant", "content": f"COMMAND: {obs.command}"}
            user_msg = {
                "role": "user",
                "content": f"EXIT: {obs.exit_status}\nSTDOUT:\n{stdout_text}\nSTDERR:\n{stderr_text}",
            }
            pair_cost = self._token_count(assistant_msg["content"]) + self._token_count(
                user_msg["content"]
            )
            if pair_cost > budget:
                break
            budget -= pair_cost
            pending_pairs.append((assistant_msg, user_msg))

        messages = list(base_messages)
        for assistant_msg, user_msg in reversed(pending_pairs):
            messages.append(assistant_msg)
            messages.append(user_msg)

        return messages

    def next_action(self, goal: str, observations: list[Observation]) -> Action:
        messages = self._build_messages(goal, observations)
        try:
            response: dict[str, Any] = self.llm.create_chat_completion(messages=messages)
        except ValueError as exc:
            if "system" in str(exc).lower():
                # Merge system prompt into first user turn for models like Gemma that reject the system role
                merged_messages: list[dict[str, str]] = []
                system_content = ""
                for m in messages:
                    if m["role"] == "system":
                        system_content = m["content"]
                    elif m["role"] == "user" and system_content:
                        merged_messages.append(
                            {"role": "user", "content": f"{system_content}\n\n{m['content']}"}
                        )
                        system_content = ""
                    else:
                        merged_messages.append(m)
                response = self.llm.create_chat_completion(messages=merged_messages)
            else:
                raise

        choices = response.get("choices", [])
        if not choices:
            return FinalAnswer("Task ended: model returned no response.")
        message = choices[0].get("message", {})
        content = message.get("content") or ""
        return parse_action(content)
