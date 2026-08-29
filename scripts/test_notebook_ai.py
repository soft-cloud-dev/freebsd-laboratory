#!/usr/bin/env python3
"""Test in-notebook AI inference and IPython magics."""

from freebsd_laboratory import ai

print("=== Available Models in Notebook ===")
for m in ai.list_models():
    print(f"  • {m['name']} ({m['size_mb']} MB)")

print("\n=== Testing ai.ask() [Default: Gemma 4 E2B Q4_K_M] ===")
response = ai.ask("What is a FreeBSD VNET jail in 2 concise sentences?")
text = getattr(response, "data", str(response))
print(text)
