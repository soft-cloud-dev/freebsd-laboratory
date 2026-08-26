#!/usr/bin/env python3
"""Benchmark vLLM inference engine on FreeBSD."""

from __future__ import annotations

import argparse
import sys
import time


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

    print(f"Loading model with vLLM: {args.model} ...")
    t0_load = time.perf_counter()
    llm = LLM(
        model=args.model,
        trust_remote_code=True,
        enforce_eager=True,
    )
    load_time = time.perf_counter() - t0_load
    print(f"Model loaded in {load_time:.2f}s")

    sampling_params = SamplingParams(
        temperature=0.0,
        max_tokens=args.max_tokens,
    )

    prompt = (
        "You are an autonomous FreeBSD system administrator agent. "
        "Analyze the system state, disk partitions, kernel sysctl parameters, "
        "and active jail network interfaces, then provide a detailed diagnostic report."
    )

    print("Running vLLM generation benchmark...")
    t0_gen = time.perf_counter()
    outputs = llm.generate([prompt], sampling_params)
    gen_time = time.perf_counter() - t0_gen

    generated_text = outputs[0].outputs[0].text
    generated_tokens = len(outputs[0].outputs[0].token_ids)
    tps = generated_tokens / gen_time if gen_time > 0 else 0.0

    print("=" * 60)
    print(f" vLLM Inference Benchmark Results")
    print("=" * 60)
    print(f" Model           : {args.model}")
    print(f" Generated tokens: {generated_tokens}")
    print(f" Elapsed time    : {gen_time:.3f} s")
    print(f" Generation Speed: {tps:.2f} tokens / sec")
    print("=" * 60)


if __name__ == "__main__":
    main()
