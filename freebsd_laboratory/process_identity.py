from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass


PS_COMMAND = shutil.which("ps") or "/bin/ps"


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    uid: int
    started_at: str
    digest: str


def _pid_exists(pid: int) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def query_process_identity(pid: int) -> ProcessIdentity | None:
    """Return a stable UID/start-time fingerprint for a live process.

    PID existence alone is not a durable ownership check because PIDs can be
    reused.  FreeBSD and the portable CI hosts both expose the real process
    start time through ps(1), so the digest survives daemon restarts while
    changing when the numeric PID is recycled.
    """

    if not _pid_exists(pid):
        return None

    try:
        result = subprocess.run(
            [PS_COMMAND, "-o", "uid=", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None
    normalized = " ".join(result.stdout.split())
    uid_text, separator, started_at = normalized.partition(" ")
    if not separator or not started_at:
        return None
    try:
        uid = int(uid_text)
    except ValueError:
        return None

    digest = hashlib.sha256(f"{uid}:{started_at}".encode("utf-8")).hexdigest()
    return ProcessIdentity(pid=pid, uid=uid, started_at=started_at, digest=digest)


def process_matches(pid: int, uid: int, digest: str) -> bool:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid < 0
        or not isinstance(digest, str)
        or not digest
    ):
        return False
    identity = query_process_identity(pid)
    return identity is not None and identity.uid == uid and identity.digest == digest
