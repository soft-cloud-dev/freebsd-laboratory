from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import BoundedOutput, Observation


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


@dataclass(frozen=True)
class AgentEvidenceEvent:
    event: str
    session_id: str
    runtime_id: str
    runtime_type: str
    step: int
    command_sha256: str
    exit_status: int
    stdout_sha256: str
    stdout_bytes: int
    stderr_sha256: str
    stderr_bytes: int
    duration_ms: int
    truncated: bool
    timestamp: str


def make_command_event(
    session_id: str,
    runtime_id: str,
    runtime_type: str,
    observation: Observation,
) -> AgentEvidenceEvent:
    cmd_bytes = observation.command.encode("utf-8")
    stdout_data = observation.stdout.head + observation.stdout.tail
    stderr_data = observation.stderr.head + observation.stderr.tail

    return AgentEvidenceEvent(
        event="agent-command-complete",
        session_id=session_id,
        runtime_id=runtime_id,
        runtime_type=runtime_type,
        step=observation.step,
        command_sha256=sha256_bytes(cmd_bytes),
        exit_status=observation.exit_status,
        stdout_sha256=sha256_bytes(stdout_data),
        stdout_bytes=observation.stdout.total_bytes,
        stderr_sha256=sha256_bytes(stderr_data),
        stderr_bytes=observation.stderr.total_bytes,
        duration_ms=observation.duration_ms,
        truncated=observation.stdout.truncated or observation.stderr.truncated,
        timestamp=utc_now(),
    )


class AgentEvidenceLog:
    """Single-writer durable append-only JSONL evidence log."""

    def __init__(self, log_path: Path | str, fsync: bool = True) -> None:
        self.log_path = Path(log_path).expanduser()
        if self.log_path.is_symlink():
            raise RuntimeError(f"Evidence log path must not be a symbolic link: {self.log_path}")

        parent_dir = self.log_path.parent
        if parent_dir.is_symlink():
            raise RuntimeError(f"Evidence parent directory must not be a symlink: {parent_dir}")
        parent_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(parent_dir, 0o700, follow_symlinks=False)

        self._fd = os.open(
            str(self.log_path),
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        os.chmod(self.log_path, 0o600, follow_symlinks=False)
        self._fsync = fsync

    def emit(self, event: AgentEvidenceEvent) -> None:
        line = canonical_json(asdict(event)) + b"\n"
        os.write(self._fd, line)
        if self._fsync:
            os.fsync(self._fd)

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
