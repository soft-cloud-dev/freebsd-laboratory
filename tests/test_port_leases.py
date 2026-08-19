from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

import pytest

from freebsd_laboratory.port_leases import LocalPortLeasePool
from freebsd_laboratory.process_identity import query_process_identity


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def make_pool(port: int, directory: Path) -> LocalPortLeasePool:
    directory.mkdir()
    (directory / ".lock").touch(mode=0o600)
    return LocalPortLeasePool(port, port, directory)


def lease_name(port: int, owner: str, process_digest: str) -> str:
    owner_digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
    return (
        f"{port}.{os.getpid()}.{os.getuid()}.{process_digest}."
        f"{owner_digest}.lease"
    )


def test_recycled_pid_fingerprint_does_not_pin_stale_port(tmp_path: Path) -> None:
    port = free_port()
    owner = "session-a"
    pool = make_pool(port, tmp_path / "leases")
    stale = pool.directory / lease_name(port, owner, "0" * 64)
    stale.touch(mode=0o600)

    reservation = pool.allocate(owner, os.getpid(), 1)
    try:
        assert reservation.ports == (port,)
        assert not stale.exists()
        current = query_process_identity(os.getpid())
        assert current is not None
        assert current.digest in next(pool.directory.glob(f"{port}.*.lease")).name
    finally:
        reservation.release()


def test_live_process_fingerprint_keeps_lease_authoritative(tmp_path: Path) -> None:
    port = free_port()
    owner = "session-a"
    pool = make_pool(port, tmp_path / "leases")
    current = query_process_identity(os.getpid())
    assert current is not None
    live = pool.directory / lease_name(port, owner, current.digest)
    live.touch(mode=0o600)

    with pytest.raises(RuntimeError, match="Unable to reserve"):
        pool.allocate("session-b", os.getpid(), 1)


def test_lease_pid_must_be_the_calling_process(tmp_path: Path) -> None:
    port = free_port()
    pool = make_pool(port, tmp_path / "leases")

    with pytest.raises(ValueError, match="must match the calling process"):
        pool.allocate("session-a", os.getpid() + 1, 1)
