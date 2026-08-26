"""High-level Python API for AI inference and autonomous agents in Jupyter notebooks."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from freebsd_laboratory.agent.cli import resolve_default_model
from freebsd_laboratory.agent.controller import AgentController
from freebsd_laboratory.agent.evidence import AgentEvidenceLog
from freebsd_laboratory.agent.model import AgentModel
from freebsd_laboratory.agent.policy import AgentPolicy
from freebsd_laboratory.agent.runtime import AgentRuntime
from freebsd_laboratory.runtime_client import DEFAULT_RUNTIME_SOCKET

# In-memory cache of loaded Llama instances by model path
_MODEL_CACHE: dict[str, Any] = {}


# Shared session token file for cross-process sync (Kernel -> Server)
DEFAULT_TOKEN_USAGE_FILE = Path(
    os.environ.get("FREEBSD_LAB_TOKEN_USAGE_FILE", "/tmp/freebsd-lab-token-usage.json")
)


class TokenUsageTracker:
    """Tracks cumulative token usage and performance across a kernel session."""

    def __init__(self, usage_file: Path | None = None) -> None:
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.total_tokens: int = 0
        self.request_count: int = 0
        self.total_elapsed_seconds: float = 0.0
        self.usage_file = usage_file or DEFAULT_TOKEN_USAGE_FILE

    def _read_file(self) -> dict[str, Any] | None:
        path = self.usage_file
        if path.is_symlink() or not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8").strip()
            if not content:
                return None
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except Exception:
            return None
        return None

    def _write_file(self, data: dict[str, Any]) -> None:
        path = self.usage_file
        if path.is_symlink():
            path.unlink()
        parent = path.parent
        if parent.is_symlink():
            return
        parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        if temp.is_symlink():
            temp.unlink()
        temp.write_text(
            json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(temp, 0o600, follow_symlinks=False)
        temp.replace(path)

    def _sync_to_file(self, p: int, c: int, e: float) -> None:
        try:
            curr = self._read_file()
            if curr:
                new_p = curr.get("prompt_tokens", 0) + p
                new_c = curr.get("completion_tokens", 0) + c
                new_reqs = curr.get("requests", 0) + 1
                new_e = curr.get("elapsed_seconds", 0.0) + e
            else:
                new_p = self.prompt_tokens
                new_c = self.completion_tokens
                new_reqs = self.request_count
                new_e = self.total_elapsed_seconds

            data = {
                "requests": new_reqs,
                "prompt_tokens": new_p,
                "completion_tokens": new_c,
                "total_tokens": new_p + new_c,
                "elapsed_seconds": round(new_e, 2),
            }
            self._write_file(data)
        except Exception:
            pass

    def _sync_to_server(self, p: int, c: int, e: float) -> None:
        try:
            _query_server(
                "/ai/usage",
                {
                    "action": "record",
                    "prompt_tokens": p,
                    "completion_tokens": c,
                    "elapsed_seconds": e,
                },
                timeout=1.0,
            )
        except Exception:
            pass

    def record(
        self, prompt_tokens: int, completion_tokens: int, elapsed_seconds: float = 0.0
    ) -> None:
        if (
            isinstance(prompt_tokens, bool)
            or isinstance(completion_tokens, bool)
            or isinstance(elapsed_seconds, bool)
        ):
            raise TypeError("Boolean values are not valid token counts or durations")

        p = max(0, int(prompt_tokens))
        c = max(0, int(completion_tokens))
        e = max(0.0, float(elapsed_seconds))

        self.prompt_tokens += p
        self.completion_tokens += c
        self.total_tokens += p + c
        self.request_count += 1
        self.total_elapsed_seconds += e

        self._sync_to_file(p, c, e)
        self._sync_to_server(p, c, e)

    def summary_dict(self) -> dict[str, Any]:
        file_data = self._read_file()
        if file_data:
            p = max(self.prompt_tokens, file_data.get("prompt_tokens", 0))
            c = max(self.completion_tokens, file_data.get("completion_tokens", 0))
            reqs = max(self.request_count, file_data.get("requests", 0))
            e = max(self.total_elapsed_seconds, file_data.get("elapsed_seconds", 0.0))
            total = p + c
        else:
            p = self.prompt_tokens
            c = self.completion_tokens
            reqs = self.request_count
            e = self.total_elapsed_seconds
            total = self.total_tokens

        tps = (c / e) if e > 0 else 0.0
        return {
            "requests": reqs,
            "prompt_tokens": p,
            "completion_tokens": c,
            "total_tokens": total,
            "elapsed_seconds": round(e, 2),
            "tokens_per_second": round(tps, 1),
        }

    def reset(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.request_count = 0
        self.total_elapsed_seconds = 0.0
        try:
            if self.usage_file.is_symlink():
                self.usage_file.unlink()
            elif self.usage_file.is_file():
                self.usage_file.unlink()
        except Exception:
            pass
        try:
            _query_server(
                "/ai/usage",
                {"action": "reset"},
                timeout=1.0,
            )
        except Exception:
            pass


# Session-lifetime singleton that resets whenever the kernel restarts
_SESSION_TRACKER = TokenUsageTracker()


def token_usage() -> dict[str, Any]:
    """Return dictionary of cumulative token usage in the current kernel session."""
    return _SESSION_TRACKER.summary_dict()


def reset_token_usage() -> None:
    """Reset cumulative token usage counter for current kernel session."""
    _SESSION_TRACKER.reset()


def token_summary() -> Any:
    """Return rendered Markdown summary card of cumulative token usage in current session."""
    u = token_usage()
    md_text = (
        "| Metric | Value |\n"
        "|:---|:---:|\n"
        f"| **Total Tokens Used** | **`{u['total_tokens']:,}`** |\n"
        f"| ↳ Prompt / Ingested Tokens | `{u['prompt_tokens']:,}` |\n"
        f"| ↳ Generated Completion Tokens | `{u['completion_tokens']:,}` |\n"
        f"| **Total Requests** | `{u['requests']}` |\n"
        f"| **Total Inference Time** | `{u['elapsed_seconds']:.2f} s` |\n"
        f"| **Average Generation Speed** | `{u['tokens_per_second']:.1f} tok/s` |\n"
    )
    try:
        from IPython.display import Markdown
        return Markdown(md_text)
    except ImportError:
        return md_text


def _get_server_urls() -> list[str]:
    explicit = os.environ.get("FREEBSD_LAB_SERVER_URL") or os.environ.get("JUPYTER_SERVER_URL")
    if explicit:
        return [explicit.rstrip("/")]
    return [
        "http://127.0.0.1:8888",
        "http://172.31.254.1:8888",
    ]


def _query_server(
    endpoint: str, payload: dict[str, Any] | None = None, timeout: float = 300.0
) -> Any:
    for base in _get_server_urls():
        url = f"{base}/freebsd-lab/api{endpoint}"
        try:
            req_data = json.dumps(payload).encode("utf-8") if payload is not None else None
            headers = {"Content-Type": "application/json"} if req_data else {}
            req = urllib.request.Request(url, data=req_data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data
        except Exception:
            continue
    raise RuntimeError(
        f"Unable to reach FreeBSD Laboratory AI service locally or via Jupyter server. "
        "Ensure the server is running on port 8888."
    )


def get_cached_llama(
    model_path: str | Path | None = None,
    n_ctx: int = 2048,
    n_gpu_layers: int = 0,
) -> Any:
    """Retrieve or initialize a cached Llama instance."""
    resolved = resolve_default_model(str(model_path) if model_path else None)
    if not resolved:
        raise RuntimeError("No GGUF model found. Specify model_path or place a model in /home/freebsd/models/")

    p = Path(resolved)
    if p.is_symlink() or not p.is_file():
        raise RuntimeError(f"Model path must be a regular file, not a symlink: {resolved}")

    cache_key = f"{resolved}:{n_ctx}:{n_gpu_layers}"
    if cache_key in _MODEL_CACHE:
        return _MODEL_CACHE[cache_key]

    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise RuntimeError(
            "llama-cpp-python is required for local in-process AI inference. "
            "Install it with: pip install 'freebsd-laboratory[agent]'"
        ) from exc

    llm = Llama(
        model_path=str(p),
        n_ctx=n_ctx,
        n_gpu_layers=n_gpu_layers,
        verbose=False,
    )
    _MODEL_CACHE[cache_key] = llm
    return llm


def generate(
    prompt: str,
    model_path: str | Path | None = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    system_prompt: str | None = None,
    n_ctx: int = 2048,
) -> str:
    """Generate raw text completion from the local LLM, tracking cumulative token usage."""
    t0 = time.perf_counter()
    try:
        llm = get_cached_llama(model_path=model_path, n_ctx=n_ctx)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            response = llm.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except ValueError as exc:
            if "system" in str(exc).lower() and system_prompt:
                merged = [{"role": "user", "content": f"{system_prompt}\n\n{prompt}"}]
                response = llm.create_chat_completion(
                    messages=merged,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            else:
                raise

        elapsed = time.perf_counter() - t0
        choices = response.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") or "" if choices else ""

        usage = response.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens") or len(llm.tokenize(prompt.encode("utf-8")))
        completion_tokens = usage.get("completion_tokens") or len(llm.tokenize(content.encode("utf-8")))
        _SESSION_TRACKER.record(prompt_tokens, completion_tokens, elapsed)

        return content
    except Exception:
        # Fallback to host Jupyter Server endpoint
        data = _query_server(
            "/ai/generate",
            {
                "prompt": prompt,
                "model": str(model_path) if model_path else None,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system_prompt": system_prompt,
            },
        )
        elapsed = time.perf_counter() - t0
        res = data.get("result", "")
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", len(prompt) // 4)
        completion_tokens = usage.get("completion_tokens", len(res) // 4)
        _SESSION_TRACKER.record(prompt_tokens, completion_tokens, elapsed)
        return res


def ask(
    prompt: str,
    model_path: str | Path | None = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    system_prompt: str = "You are a knowledgeable FreeBSD and Unix systems engineering assistant.",
    show_usage: bool = False,
) -> Any:
    """Query the model and return rich Markdown display for Jupyter Notebooks."""
    text = generate(
        prompt=prompt,
        model_path=model_path,
        max_tokens=max_tokens,
        temperature=temperature,
        system_prompt=system_prompt,
    )

    if show_usage:
        u = token_usage()
        text += f"\n\n---\n*Session Token Usage: {u['total_tokens']:,} tokens across {u['requests']} request(s)*"

    try:
        from IPython.display import Markdown
        return Markdown(text)
    except ImportError:
        return text


def run_agent(
    goal: str,
    mode: str = "bhyve",
    model_path: str | Path | None = None,
    max_steps: int = 16,
    max_runtime: int = 300,
    socket_path: str = DEFAULT_RUNTIME_SOCKET,
    evidence_dir: str | None = None,
) -> str:
    """Run an autonomous troubleshooting agent in an isolated jail or bhyve VM."""
    t0 = time.perf_counter()
    try:
        resolved = resolve_default_model(str(model_path) if model_path else None)
        if not resolved:
            raise RuntimeError("No local GGUF model found")

        policy = AgentPolicy(
            max_command_bytes=4096,
            max_steps=max_steps,
            max_runtime_seconds=max_runtime,
        )

        startup_timeout = 90 if mode == "bhyve" else 30
        runtime = AgentRuntime(
            mode=mode,
            socket_path=socket_path,
            startup_timeout=startup_timeout,
        )

        evidence_log = None
        if evidence_dir:
            evidence_path = Path(evidence_dir) / "agent.jsonl"
            evidence_log = AgentEvidenceLog(evidence_path)

        try:
            model = AgentModel(model_path=resolved)
            controller = AgentController(
                model=model,
                runtime=runtime,
                policy=policy,
                evidence_log=evidence_log,
            )
            result = controller.run(goal)
            elapsed = time.perf_counter() - t0
            # Rough approximation for multi-turn agent token usage
            _SESSION_TRACKER.record(len(goal) // 4, len(result) // 4, elapsed)
            return result
        finally:
            if evidence_log is not None:
                evidence_log.close()
    except Exception:
        # Fallback to host server endpoint
        data = _query_server(
            "/ai/agent",
            {
                "goal": goal,
                "mode": mode,
                "model": str(model_path) if model_path else None,
                "max_steps": max_steps,
                "max_runtime": max_runtime,
            },
        )
        elapsed = time.perf_counter() - t0
        res = data.get("result", "")
        _SESSION_TRACKER.record(len(goal) // 4, len(res) // 4, elapsed)
        return res


def list_models(models_dir: str | Path = "/home/freebsd/models") -> list[dict[str, Any]]:
    """List all available local GGUF models with sizes."""
    p = Path(models_dir)
    if p.is_dir() and not p.is_symlink():
        models = []
        for f in sorted(p.glob("*.gguf")):
            if f.is_file() and not f.is_symlink():
                size_mb = f.stat().st_size / (1024 * 1024)
                models.append({
                    "name": f.name,
                    "path": str(f),
                    "size_mb": round(size_mb, 1),
                    "size_gb": round(size_mb / 1024, 2),
                })
        return models

    # Fallback to host server API only when default models directory requested
    if str(models_dir) == "/home/freebsd/models":
        try:
            data = _query_server("/ai/models")
            return data.get("models", [])
        except Exception:
            return []
    return []
