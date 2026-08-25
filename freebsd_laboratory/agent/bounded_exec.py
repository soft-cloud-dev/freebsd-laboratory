from __future__ import annotations

import subprocess
import threading
from typing import IO, Sequence

from .types import BoundedOutput

DEFAULT_HEAD_LIMIT = 4096
DEFAULT_TAIL_LIMIT = 4096
READ_CHUNK = 4096


class _StreamDrainer:
    """Drain a pipe concurrently into bounded head + rolling tail buffers."""

    def __init__(self, stream: IO[bytes] | None, head_limit: int, tail_limit: int) -> None:
        self.stream = stream
        self.head_limit = head_limit
        self.tail_limit = tail_limit
        self.head = bytearray()
        self.tail = bytearray()
        self.total_bytes = 0

    def run(self) -> None:
        if self.stream is None:
            return
        try:
            while True:
                chunk = self.stream.read(READ_CHUNK)
                if not chunk:
                    break
                self.total_bytes += len(chunk)
                if len(self.head) < self.head_limit:
                    needed = self.head_limit - len(self.head)
                    take = min(len(chunk), needed)
                    self.head.extend(chunk[:take])
                    remaining = chunk[take:]
                else:
                    remaining = chunk

                if remaining and self.tail_limit > 0:
                    self.tail.extend(remaining)
                    if len(self.tail) > self.tail_limit:
                        self.tail = self.tail[-self.tail_limit :]
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.stream.close()
            except (OSError, ValueError):
                pass

    def result(self) -> BoundedOutput:
        truncated = self.total_bytes > (self.head_limit + self.tail_limit)
        return BoundedOutput(
            head=bytes(self.head),
            tail=bytes(self.tail),
            total_bytes=self.total_bytes,
            truncated=truncated,
        )


def bounded_exec(
    command: Sequence[str],
    *,
    timeout: float | None = 30.0,
    head_limit: int = DEFAULT_HEAD_LIMIT,
    tail_limit: int = DEFAULT_TAIL_LIMIT,
) -> tuple[int, BoundedOutput, BoundedOutput]:
    """Execute a subprocess command with bounded output capture and concurrent pipe draining.

    stdin is always DEVNULL. No PTY is allocated.
    Returns (exit_status, stdout_output, stderr_output).
    """
    if isinstance(head_limit, bool) or not isinstance(head_limit, int) or head_limit < 0:
        raise ValueError("head_limit must be a non-negative integer")
    if isinstance(tail_limit, bool) or not isinstance(tail_limit, int) or tail_limit < 0:
        raise ValueError("tail_limit must be a non-negative integer")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number")

    proc = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout_drainer = _StreamDrainer(proc.stdout, head_limit, tail_limit)
    stderr_drainer = _StreamDrainer(proc.stderr, head_limit, tail_limit)

    stdout_thread = threading.Thread(target=stdout_drainer.run, daemon=True)
    stderr_thread = threading.Thread(target=stderr_drainer.run, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()

    stdout_thread.join(timeout=5.0)
    stderr_thread.join(timeout=5.0)

    exit_status = -1 if timed_out else proc.returncode
    return exit_status, stdout_drainer.result(), stderr_drainer.result()
