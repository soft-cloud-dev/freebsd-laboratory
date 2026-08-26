#!/usr/bin/env python3
"""Benchmark vLLM inference engine on FreeBSD (CPU / OpenVINO / GPU)."""

from __future__ import annotations

import argparse
import os
import sys
import time

os.environ["VLLM_TARGET_DEVICE"] = "cpu"
os.environ["VLLM_USE_V1"] = "0"


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark vLLM on FreeBSD")
    parser.add_argument(
        "--model",
        default="Qwen/Qwen2.5-1.5B-Instruct",
        help="HuggingFace model ID or local directory",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=128,
        help="Tokens to generate",
    )
    args = parser.parse_args()

    try:
        from vllm import LLM, SamplingParams
    except ImportError as exc:
        sys.exit(f"Error: vLLM not available: {exc}")

    print("=" * 64)
    print(f" vLLM INFERENCE BENCHMARK — FreeBSD Laboratory")
    print(f" Model       : {args.model}")
    print(f" Device      : CPU (V0 engine)")
    print(f" Max Tokens  : {args.max_tokens}")
    print("=" * 64)
    print()

    print(f"Loading model into vLLM engine...", flush=True)
    t0_load = time.perf_counter()
    llm = LLM(
        model=args.model,
        trust_remote_code=True,
        enforce_eager=True,
    )
    load_time = time.perf_counter() - t0_load
    print(f"Model loaded in {load_time:.2f}s ({load_time * 1000:.0f} ms)")

    sampling_params = SamplingParams(
        temperature=0.0,
        max_tokens=args.max_tokens,
    )

    prompt = (
        "You are an autonomous FreeBSD system administrator agent. "
        "Analyze the system state, disk partitions, kernel sysctl parameters, "
        "and active jail network interfaces, then provide a detailed diagnostic report."
    )

    # Warmup
    _ = llm.generate([prompt], SamplingParams(temperature=0.0, max_tokens=16))

    print("Running vLLM generation benchmark...", flush=True)
    t0_gen = time.perf_counter()
    outputs = llm.generate([prompt], sampling_params)
    gen_time = time.perf_counter() - t0_gen

    generated_text = outputs[0].outputs[0].text
    generated_tokens = len(outputs[0].outputs[0].token_ids)
    tps = generated_tokens / gen_time if gen_time > 0 else 0.0

    print()
    print("=" * 64)
    print(f" {'Metric':<25} | {'Result':<32}")
    print("-" * 64)
    print(f" {'Engine':<25} | {'vLLM (py312-vllm)':<32}")
    print(f" {'Model':<25} | {args.model:<32}")
    print(f" {'Load Time':<25} | {load_time * 1000:>10.0f} ms")
    print(f" {'Generated Tokens':<25} | {generated_tokens:>10d} tokens")
    print(f" {'Elapsed Time':<25} | {gen_time:>10.3f} s")
    print(f" {'Generation Throughput':<25} | {tps:>10.2f} tokens / sec")
    print("=" * 64)


if __name__ == "__main__":
    main()
