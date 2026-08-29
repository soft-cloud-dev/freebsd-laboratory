#!/usr/bin/env python3
"""Comprehensive benchmark for all local GGUF models in /home/freebsd/models/."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def benchmark_single(model_path: Path, n_threads: int, eval_tokens: int = 128) -> dict:
    from llama_cpp import Llama

    t0_load = time.perf_counter()
    llm = Llama(
        model_path=str(model_path),
        n_ctx=2048,
        n_threads=n_threads,
        n_gpu_layers=0,
        verbose=False,
    )
    t_load = (time.perf_counter() - t0_load) * 1000.0

    test_prompt = (
        "You are an autonomous FreeBSD system administrator agent. "
        "Analyze the system state, disk partitions, kernel sysctl parameters, "
        "and active jail network interfaces, then provide a detailed diagnostic report."
    )

    # Warmup pass (16 tokens)
    _ = llm(test_prompt, max_tokens=16, temperature=0.0)

    # 1. Prompt Processing
    tokens = llm.tokenize(test_prompt.encode("utf-8"))
    n_prompt_tokens = len(tokens)

    t0_prompt = time.perf_counter()
    llm.reset()
    llm.eval(tokens)
    t_prompt = time.perf_counter() - t0_prompt
    prompt_tps = n_prompt_tokens / t_prompt if t_prompt > 0 else 0.0

    # 2. Text Generation
    t0_gen = time.perf_counter()
    generated_count = 0
    for _ in llm.generate(tokens, temp=0.0):
        generated_count += 1
        if generated_count >= eval_tokens:
            break
    t_gen = time.perf_counter() - t0_gen
    gen_tps = generated_count / t_gen if t_gen > 0 else 0.0

    size_mb = model_path.stat().st_size / (1024 * 1024)

    return {
        "name": model_path.name,
        "size_mb": size_mb,
        "threads": n_threads,
        "load_ms": t_load,
        "prompt_tps": prompt_tps,
        "gen_tps": gen_tps,
        "ttft_ms": t_prompt * 1000.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark all models in a directory")
    parser.add_argument(
        "--models-dir",
        default="/home/freebsd/models",
        help="Directory containing GGUF models",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=16,
        help="Number of threads to use (default: 16)",
    )
    parser.add_argument(
        "--tokens",
        type=int,
        default=128,
        help="Number of tokens to generate per model (default: 128)",
    )
    args = parser.parse_args()

    models_dir = Path(args.models_dir)
    if not models_dir.is_dir():
        sys.exit(f"Error: Models directory not found: {models_dir}")

    model_files = sorted(models_dir.glob("*.gguf"))
    if not model_files:
        sys.exit(f"Error: No .gguf model files found in {models_dir}")

    print("=" * 86)
    print(f" COMPREHENSIVE LOCAL LLM BENCHMARK — FreeBSD Laboratory")
    print(f" Directory  : {models_dir}")
    print(f" Models Found: {len(model_files)}")
    print(f" Threads    : {args.threads}")
    print(f" Tokens/Run : {args.tokens}")
    print("=" * 86)
    print()

    results = []
    for idx, mf in enumerate(model_files, 1):
        print(f"[{idx}/{len(model_files)}] Benchmarking {mf.name} ({mf.stat().st_size / (1024**2):.1f} MB)...", flush=True)
        try:
            r = benchmark_single(mf, n_threads=args.threads, eval_tokens=args.tokens)
            results.append(r)
            print(f"      ↳ Prompt: {r['prompt_tps']:6.1f} tok/s | Generation: {r['gen_tps']:5.1f} tok/s | Load: {r['load_ms']:.0f} ms")
        except Exception as e:
            print(f"      ↳ FAILED: {e}")

    print()
    print("=" * 86)
    print(f" {'Model Name':<34} | {'Size':<8} | {'Prompt Eval':<12} | {'Generation':<12} | {'Load':<8}")
    print(f" {'':<34} | {'':<8} | {'(tokens/s)':<12} | {'(tokens/s)':<12} | {'(ms)':<8}")
    print("-" * 86)
    for r in sorted(results, key=lambda x: x["gen_tps"], reverse=True):
        size_str = f"{r['size_mb']/1024:.2f} GB" if r['size_mb'] >= 1024 else f"{r['size_mb']:.0f} MB"
        print(
            f" {r['name']:<34} | "
            f"{size_str:<8} | "
            f"{r['prompt_tps']:>10.1f} t/s | "
            f"{r['gen_tps']:>10.1f} t/s | "
            f"{r['load_ms']:>6.0f} ms"
        )
    print("=" * 86)


if __name__ == "__main__":
    main()
