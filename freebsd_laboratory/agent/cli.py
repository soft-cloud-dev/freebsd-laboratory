from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from ..runtime_client import DEFAULT_RUNTIME_SOCKET
from .controller import AgentController
from .evidence import AgentEvidenceLog
from .model import AgentModel
from .policy import AgentPolicy
from .runtime import AgentRuntime


def resolve_default_model(explicit_path: str | None) -> str | None:
    if explicit_path:
        return explicit_path
    env_model = os.environ.get("FREEBSD_LAB_AGENT_MODEL")
    if env_model:
        p = Path(env_model)
        if p.is_file() and not p.is_symlink():
            return env_model
    candidates = [
        Path("/home/freebsd/models/qwen2.5-1.5b-instruct-q4_k_m.gguf"),
        Path("/home/freebsd/models/gemma-2-2b-it-Q4_K_M.gguf"),
    ]
    for c in candidates:
        if c.is_file() and not c.is_symlink():
            return str(c)
    models_dir = Path("/home/freebsd/models")
    if models_dir.is_dir() and not models_dir.is_symlink():
        for f in models_dir.glob("*.gguf"):
            if f.is_file() and not f.is_symlink():
                return str(f)
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="freebsd-lab-agent",
        description="Run an unprivileged autonomous agent in an isolated FreeBSD runtime.",
    )
    parser.add_argument(
        "goal",
        nargs="?",
        default=None,
        help="Task description or goal for the agent (prompts interactively if omitted)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Path to local GGUF model file (default: $FREEBSD_LAB_AGENT_MODEL or /home/freebsd/models/*.gguf)",
    )
    parser.add_argument(
        "--mode",
        choices=["bhyve", "jail"],
        default="bhyve",
        help="Runtime isolation mode (default: bhyve)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=16,
        help="Maximum action steps (1-25, default: 16)",
    )
    parser.add_argument(
        "--max-runtime",
        type=int,
        default=300,
        help="Maximum wall-clock execution seconds (default: 300)",
    )
    parser.add_argument(
        "--max-output",
        type=int,
        default=8192,
        help="Maximum output capture bytes per command (default: 8192)",
    )
    parser.add_argument(
        "--command-timeout",
        type=int,
        default=30,
        help="Per-command SSH execution timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--startup-timeout",
        type=int,
        default=None,
        help="Runtime startup readiness timeout in seconds (default: 90 for bhyve, 30 for jail)",
    )
    parser.add_argument(
        "--n-ctx",
        type=int,
        default=2048,
        help="Model context window size in tokens (default: 2048)",
    )
    parser.add_argument(
        "--n-gpu-layers",
        type=int,
        default=0,
        help="Number of GPU layers for llama.cpp offloading (default: 0)",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "llama_cpp", "vllm", "vllm_server"],
        default="auto",
        help="Inference engine backend (default: auto)",
    )
    parser.add_argument(
        "--vllm-url",
        default=None,
        help="OpenAI-compatible HTTP endpoint for vLLM server (default: $VLLM_BASE_URL or http://localhost:8000/v1)",
    )
    parser.add_argument(
        "--evidence-dir",
        type=str,
        default=None,
        help="Directory to write standalone agent.jsonl evidence log",
    )
    parser.add_argument(
        "--socket",
        default=DEFAULT_RUNTIME_SOCKET,
        help="Unix domain socket path for runtime daemon",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    goal = args.goal
    if not goal:
        if sys.stdin.isatty():
            try:
                goal = input("\033[1;36m[FreeBSD AI Agent]\033[0m Goal: ").strip()
            except (EOFError, KeyboardInterrupt):
                sys.exit(0)
        if not goal:
            sys.exit("Error: No goal provided.")

    resolved_model = None
    if args.backend != "vllm_server":
        resolved_model = resolve_default_model(args.model)
        if not resolved_model and args.backend != "vllm":
            sys.exit(
                "Error: --model must be specified or a valid GGUF model placed in /home/freebsd/models/"
            )
    else:
        resolved_model = args.model or "default"

    if args.max_steps < 1 or args.max_steps > 25:
        sys.exit("Error: --max-steps must be between 1 and 25.")

    if args.backend == "llama_cpp" or (args.backend == "auto" and not args.vllm_url and not os.environ.get("VLLM_BASE_URL")):
        if resolved_model:
            model_path = Path(resolved_model)
            if model_path.is_symlink() or not model_path.is_file():
                sys.exit(f"Error: Model path must be a regular file, not a symlink: {resolved_model}")

    startup_timeout = args.startup_timeout
    if startup_timeout is None:
        startup_timeout = 90 if args.mode == "bhyve" else 30

    head_limit = args.max_output // 2
    tail_limit = args.max_output // 2

    policy = AgentPolicy(
        max_command_bytes=4096,
        max_steps=args.max_steps,
        max_runtime_seconds=args.max_runtime,
    )

    runtime = AgentRuntime(
        mode=args.mode,
        socket_path=args.socket,
        startup_timeout=startup_timeout,
        command_timeout=args.command_timeout,
        head_limit=head_limit,
        tail_limit=tail_limit,
    )

    evidence_log = None
    if args.evidence_dir:
        evidence_path = Path(args.evidence_dir) / "agent.jsonl"
        evidence_log = AgentEvidenceLog(evidence_path)

    try:
        model = AgentModel(
            model_path=resolved_model,
            n_ctx=args.n_ctx,
            n_gpu_layers=args.n_gpu_layers,
            backend=args.backend,
            vllm_url=args.vllm_url,
        )
        controller = AgentController(
            model=model,
            runtime=runtime,
            policy=policy,
            evidence_log=evidence_log,
        )
        result = controller.run(goal)
        print(result)
    finally:
        if evidence_log is not None:
            evidence_log.close()


if __name__ == "__main__":
    main()
