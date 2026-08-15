from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class EvidenceEvent:
    sequence: int
    timestamp: str
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    stdout_sha256: str
    stderr_sha256: str


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class EvidenceRecorder:
    def __init__(self) -> None:
        self.events: list[EvidenceEvent] = []

    def run(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.events.append(
            EvidenceEvent(
                sequence=len(self.events) + 1,
                timestamp=datetime.now(timezone.utc).isoformat(),
                command=list(command),
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                stdout_sha256=sha256(result.stdout),
                stderr_sha256=sha256(result.stderr),
            )
        )
        return result

    def export(self, path: str | Path) -> None:
        document = {
            "schema": "softcloud.lab-evidence/v1",
            "runtime": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "events": [asdict(event) for event in self.events],
        }
        Path(path).write_text(json.dumps(document, indent=2) + "\n")
