from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .types import Action, BoundedOutput, Command, FinalAnswer, Observation

if TYPE_CHECKING:
    from llama_cpp import Llama

SYSTEM_PROMPT = """\
You are an autonomous FreeBSD system administrator agent.
You operate on a disposable FreeBSD runtime to accomplish the user's goal.

You MUST respond in ONE of two formats:
COMMAND: <single shell command to execute>
FINAL: <answer or summary of the completed task>

Guidelines:
- Output ONLY a single COMMAND: or FINAL: action per turn.
- For conversational greetings or pure questions (e.g. "Hi", "What is 2+2?"), respond immediately with FINAL: <response>.
- To inspect or modify the system, run one non-interactive command with COMMAND: <cmd>.
- Do not use interactive tools (vi, less, top).
- Once you see the command output and have the answer to the user's goal, summarize the result with FINAL: <answer>.
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
        elif line_clean.upper().startswith("FINAL ANSWER:"):
            ans = line_clean[len("FINAL ANSWER:") :].strip()
            return FinalAnswer(ans or stripped)

    # If first line looks like a single command without prompt decoration
    first_line = stripped.splitlines()[0].strip()
    if len(stripped.splitlines()) == 1:
        # Check if the single line is conversational prose or sentence
        is_prose = (
            any(first_line.endswith(p) for p in (".", "?", "!"))
            or first_line.startswith((
                "#", "//", "I ", "Let ", "Hello", "Hi", "Sure", "There ", "Here ",
                "The ", "This ", "You ", "We ", "Please ", "What ", "How ", "Why ", "Is ", "Can "
            ))
        )
        if not is_prose and len(first_line.split()) <= 8:
            return Command(first_line)

    return FinalAnswer(stripped)


class AgentModel:
    """Interface to local LLM engines (llama_cpp, vLLM, or vLLM server) for action generation."""

    def __init__(
        self,
        model_path: str | Path | None = None,
        n_ctx: int = 2048,
        n_gpu_layers: int = 0,
        backend: str = "auto",
        vllm_url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        if isinstance(n_ctx, bool) or not isinstance(n_ctx, int) or n_ctx < 256:
            raise ValueError("n_ctx must be an integer >= 256")
        if isinstance(n_gpu_layers, bool) or not isinstance(n_gpu_layers, int) or n_gpu_layers < 0:
            raise ValueError("n_gpu_layers must be a non-negative integer")

        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.api_key = api_key or os.environ.get("VLLM_API_KEY", "")

        # Resolve backend
        resolved_backend = backend.lower()
        if resolved_backend == "auto":
            if vllm_url or os.environ.get("VLLM_BASE_URL"):
                resolved_backend = "vllm_server"
            elif model_path and str(model_path).startswith(("http://", "https://")):
                resolved_backend = "vllm_server"
            elif os.environ.get("FREEBSD_LAB_AGENT_BACKEND"):
                resolved_backend = os.environ["FREEBSD_LAB_AGENT_BACKEND"].lower()
            else:
                resolved_backend = "llama_cpp"

        self.backend = resolved_backend
        self.vllm_url = vllm_url or os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
        self.model_path = str(model_path) if model_path else ""

        if self.backend == "vllm_server":
            if not self.model_path:
                self.model_path = "default"
            self.llm = None
        elif self.backend == "vllm":
            try:
                from vllm import LLM, SamplingParams
            except ImportError as exc:
                raise RuntimeError(
                    "vLLM is required for backend='vllm'. Install with pkg install py312-vllm"
                ) from exc
            self.sampling_params = SamplingParams(
                temperature=0.0,
                max_tokens=512,
            )
            self.llm = LLM(
                model=self.model_path,
                max_model_len=n_ctx,
                trust_remote_code=True,
            )
        else:
            # llama_cpp backend
            model_file = Path(self.model_path)
            if model_file.is_symlink() or not model_file.is_file():
                raise RuntimeError(f"GGUF model must be a regular file, not a symlink: {self.model_path}")
            if not os.access(model_file, os.R_OK):
                raise RuntimeError(f"GGUF model file is not readable: {self.model_path}")

            try:
                from llama_cpp import Llama
            except ImportError as exc:
                raise RuntimeError(
                    "llama-cpp-python is required for AgentModel with llama_cpp backend. "
                    "Install it with: pip install 'freebsd-laboratory[agent]'"
                ) from exc

            self.llm = Llama(
                model_path=self.model_path,
                n_ctx=n_ctx,
                n_gpu_layers=n_gpu_layers,
                verbose=False,
            )

    def _token_count(self, text: str) -> int:
        if self.backend == "llama_cpp" and self.llm is not None:
            return len(self.llm.tokenize(text.encode("utf-8")))
        # Rough heuristic token count for vllm / vllm_server (~4 chars per token)
        return max(1, len(text) // 4)

    def _query_vllm_server(self, messages: list[dict[str, str]]) -> str:
        import urllib.error
        import urllib.request

        url = f"{self.vllm_url.rstrip('/')}/chat/completions"
        payload = json.dumps({
            "model": self.model_path or "default",
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 512,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                choices = data.get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")
                return ""
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Failed to communicate with vLLM server at {url}: {exc}") from exc

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
                "content": (
                    f"EXIT: {obs.exit_status}\n"
                    f"STDOUT:\n{stdout_text}\n"
                    f"STDERR:\n{stderr_text}\n\n"
                    "Provide the next COMMAND: <cmd> or FINAL: <result>."
                ),
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

        if self.backend == "vllm_server":
            content = self._query_vllm_server(messages)
            return parse_action(content)

        if self.backend == "vllm":
            # Format chat prompt or query vllm engine
            prompt_text = ""
            for m in messages:
                role = m["role"].upper()
                prompt_text += f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n"
            prompt_text += "<|im_start|>assistant\n"
            outputs = self.llm.generate([prompt_text], self.sampling_params)
            content = outputs[0].outputs[0].text if outputs and outputs[0].outputs else ""
            return parse_action(content)

        # llama_cpp backend
        try:
            response: dict[str, Any] = self.llm.create_chat_completion(messages=messages)
        except ValueError as exc:
            if "system" in str(exc).lower():
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
