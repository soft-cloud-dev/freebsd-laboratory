#!/usr/bin/env python3
"""End-to-end integration test for vLLM server backend."""

import http.server
import json
import threading
import time
import subprocess
import sys


class MockVLLMHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/v1/chat/completions" or self.path == "/chat/completions":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            data = json.loads(body.decode("utf-8"))
            messages = data.get("messages", [])

            # Simple reasoning logic for testing
            last_msg = messages[-1]["content"] if messages else ""
            if "GOAL: Check hostname" in str(messages):
                if "EXIT: 0" in last_msg:
                    answer = "FINAL: The hostname was verified successfully."
                else:
                    answer = "COMMAND: hostname"
            else:
                answer = "FINAL: Task executed via vLLM OpenAI API."

            resp = {
                "id": "cmpl-vllm-test",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": data.get("model", "qwen2.5:1.5b"),
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": answer,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 32,
                    "completion_tokens": 12,
                    "total_tokens": 44,
                },
            }
            resp_bytes = json.dumps(resp).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    server = http.server.HTTPServer(("127.0.0.1", 8000), MockVLLMHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print("✓ Local vLLM OpenAI server listening on http://127.0.0.1:8000/v1")

    # Run agent in bhyve mode against vLLM server
    cmd = [
        "/home/freebsd/freebsd-laboratory/.venv/bin/freebsd-lab-agent",
        "Check hostname",
        "--backend", "vllm_server",
        "--vllm-url", "http://127.0.0.1:8000/v1",
        "--mode", "bhyve",
        "--max-steps", "4",
    ]
    print(f"Executing: {' '.join(cmd)}")
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.perf_counter() - t0

    print("=" * 60)
    print(f" Exit status : {proc.returncode}")
    print(f" Output      :\n{proc.stdout.strip()}")
    if proc.stderr:
        print(f" Stderr      :\n{proc.stderr.strip()}")
    print(f" Latency     : {elapsed:.2f}s")
    print("=" * 60)

    server.shutdown()
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
