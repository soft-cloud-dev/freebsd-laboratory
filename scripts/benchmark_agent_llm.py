#!/usr/bin/env python3
"""Benchmark local GGUF models on FreeBSD in terms of tokens/second."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def run_benchmark(
    model_path: str,
    n_ctx: int = 2048,
    threads_list: list[int] | None = None,
    eval_tokens: int = 128,
    warmup_tokens: int = 16,
) -> None:
    try:
        from llama_cpp import Llama
    except ImportError:
        sys.exit("Error: llama-cpp-python is not installed in the current environment.")

    model_file = Path(model_path)
    if not model_file.is_file():
        sys.exit(f"Error: Model file not found: {model_path}")

    model_name = model_file.stem
    size_mb = model_file.stat().st_size / (1024 * 1024)

    if threads_list is None:
        threads_list = [1, 2, 4, 6, 8, 12, 16]

    print("=" * 72)
    print(f" LLM INFERENCE BENCHMARK — FreeBSD Laboratory")
    print(f" Model       : {model_name}")
    print(f" File Size   : {size_mb:.1f} MB ({size_mb / 1024:.2f} GB)")
    print(f" Target Gen  : {eval_tokens} tokens")
    print("=" * 72)
    print()

    test_prompt = (
        "You are an autonomous FreeBSD system administrator agent. "
        "Analyze the system state, disk partitions, kernel sysctl parameters, "
        "and active jail network interfaces, then provide a detailed diagnostic report."
    )

    results = []

    for n_threads in threads_list:
        print(f"Testing with n_threads={n_threads:2d} ...", end=" ", flush=True)

        t0_load = time.perf_counter()
        llm = Llama(
            model_path=str(model_file),
            n_ctx=n_ctx,
            n_threads=n_threads,
            n_gpu_layers=0,
            verbose=False,
        )
        t_load = (time.perf_counter() - t0_load) * 1000.0

        # Warmup pass
        _ = llm(test_prompt, max_tokens=warmup_tokens, temperature=0.0)

        # 1. Benchmark Prompt Processing
        tokens = llm.tokenize(test_prompt.encode("utf-8"))
        n_prompt_tokens = len(tokens)

        t0_prompt = time.perf_counter()
        llm.reset()
        llm.eval(tokens)
        t_prompt = time.perf_counter() - t0_prompt
        prompt_tps = n_prompt_tokens / t_prompt if t_prompt > 0 else 0.0

        # 2. Benchmark Token Generation
        t0_gen = time.perf_counter()
        generated_count = 0
        gen_tokens = []
        for token in llm.generate(tokens, temp=0.0):
            gen_tokens.append(token)
            generated_count += 1
            if generated_count >= eval_tokens:
                break
        t_gen = time.perf_counter() - t0_gen
        gen_tps = generated_count / t_gen if t_gen > 0 else 0.0

        results.append({
            "threads": n_threads,
            "load_ms": t_load,
            "prompt_tokens": n_prompt_tokens,
            "prompt_sec": t_prompt,
            "prompt_tps": prompt_tps,
            "gen_tokens": generated_count,
            "gen_sec": t_gen,
            "gen_tps": gen_tps,
        })

        print(f"Prompt: {prompt_tps:6.1f} t/s | Generation: {gen_tps:5.1f} t/s")

    print()
    print("=" * 72)
    print(f" {'Threads':<8} | {'Prompt Processing':<18} | {'Text Generation':<18} | {'Load Time':<10}")
    print(f" {'(cores)':<8} | {'(tokens / sec)':<18} | {'(tokens / sec)':<18} | {'(ms)':<10}")
    print("-" * 72)
    best_gen = max(results, key=lambda r: r["gen_tps"])
    for r in results:
        is_best = " ★ (fastest)" if r["threads"] == best_gen["threads"] else ""
        print(
            f" {r['threads']:<8d} | "
            f"{r['prompt_tps']:>10.1f} tok/s    | "
            f"{r['gen_tps']:>10.1f} tok/s    | "
            f"{r['load_ms']:>8.0f} ms{is_best}"
        )
    print("=" * 72)
    print(f"Optimal configuration: n_threads={best_gen['threads']} ({best_gen['gen_tps']:.1f} tokens/s generation)")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark local GGUF models in tokens/sec.")
    parser.add_argument(
        "--model",
        default="/home/freebsd/models/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        help="Path to GGUF model file",
    )
    parser.add_argument(
        "--threads",
        nargs="+",
        type=int,
        default=[1, 2, 4, 6, 8, 12, 16],
        help="Thread counts to test",
    )
    parser.add_argument(
        "--tokens",
        type=int,
        default=128,
        help="Number of tokens to generate per test (default: 128)",
    )
    parser.add_argument(
        "--n-ctx",
        type=int,
        default=2048,
        help="Context window size (default: 2048)",
    )
    args = parser.parse_args()
    run_benchmark(
        model_path=args.model,
        n_ctx=args.n_ctx,
        threads_list=args.threads,
        eval_tokens=args.tokens,
    )


if __name__ == "__main__":
    main()
