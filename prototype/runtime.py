from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class RuntimeIdentity:
    system: str
    release: str
    machine: str


class Executor(Protocol):
    def identity(self) -> RuntimeIdentity: ...
    def execute(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


class LocalExecutor:
    """Development executor. Production FreeBSD execution plugs in here."""

    def identity(self) -> RuntimeIdentity:
        return RuntimeIdentity(platform.system(), platform.release(), platform.machine())

    def execute(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False)


class FreeBSDExecutor(LocalExecutor):
    def identity(self) -> RuntimeIdentity:
        identity = super().identity()
        if identity.system != "FreeBSD":
            raise RuntimeError(
                f"FreeBSD runtime required; executor is running on {identity.system}"
            )
        return identity
