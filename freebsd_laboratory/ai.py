"""High-level Python API for AI inference and autonomous agents in Jupyter notebooks."""

from __future__ import annotations

import json
import os
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


def _get_server_urls() -> list[str]:
    explicit = os.environ.get("FREEBSD_LAB_SERVER_URL") or os.environ.get("JUPYTER_SERVER_URL")
    if explicit:
        return [explicit.rstrip("/")]
    return [
        "http://127.0.0.1:8888",
        "http://172.31.254.1:8888",
    ]


def _query_server(endpoint: str, payload: dict[str, Any] | None = None) -> Any:
    for base in _get_server_urls():
        url = f"{base}/freebsd-lab/api{endpoint}"
        try:
            req_data = json.dumps(payload).encode("utf-8") if payload is not None else None
            headers = {"Content-Type": "application/json"} if req_data else {}
            req = urllib.request.Request(url, data=req_data, headers=headers)
            with urllib.request.urlopen(req, timeout=300.0) as resp:
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
    """Generate raw text completion from the local LLM, with automatic fallback to Jupyter Server API."""
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

        choices = response.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "") or ""
    except Exception:
        # Fallback to host Jupyter Server endpoint (e.g. when executing inside guest runtime)
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
        return data.get("result", "")


def ask(
    prompt: str,
    model_path: str | Path | None = None,
    max_tokens: int = 512,
    temperature: float = 0.0,
    system_prompt: str = "You are a knowledgeable FreeBSD and Unix systems engineering assistant.",
) -> Any:
    """Query the model and return rich Markdown display for Jupyter Notebooks."""
    text = generate(
        prompt=prompt,
        model_path=model_path,
        max_tokens=max_tokens,
        temperature=temperature,
        system_prompt=system_prompt,
    )

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
            return controller.run(goal)
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
        return data.get("result", "")


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
        if models:
            return models

    # Fallback to host server API
    try:
        data = _query_server("/ai/models")
        return data.get("models", [])
    except Exception:
        return []
