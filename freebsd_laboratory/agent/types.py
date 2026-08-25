from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BoundedOutput:
    """Result of bounded stream capture."""

    head: bytes
    tail: bytes
    total_bytes: int
    truncated: bool

    def as_text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        """Return combined head and tail decoded as text."""
        if not self.tail:
            return self.head.decode(encoding, errors=errors)
        head_text = self.head.decode(encoding, errors=errors)
        tail_text = self.tail.decode(encoding, errors=errors)
        return f"{head_text}\n... [TRUNCATED {self.total_bytes} bytes total] ...\n{tail_text}"


@dataclass(frozen=True)
class Observation:
    """Result of executing an action in the runtime. Transient controller state."""

    step: int
    command: str
    exit_status: int
    stdout: BoundedOutput
    stderr: BoundedOutput
    duration_ms: int

    def __post_init__(self) -> None:
        if isinstance(self.step, bool) or not isinstance(self.step, int):
            raise TypeError("step must be an integer")
        if isinstance(self.exit_status, bool) or not isinstance(self.exit_status, int):
            raise TypeError("exit_status must be an integer")
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int):
            raise TypeError("duration_ms must be an integer")


@dataclass(frozen=True)
class Command:
    """Model proposes a single shell action."""

    command: str


@dataclass(frozen=True)
class FinalAnswer:
    """Model declares the task complete."""

    answer: str


Action = Command | FinalAnswer


@dataclass(frozen=True)
class RuntimeHandle:
    """Opaque handle to a running runtime. Never exposed to the model."""

    runtime_name: str
    guest_ip: str
    runtime_type: str
    private_key: Path
    known_hosts_file: Path
