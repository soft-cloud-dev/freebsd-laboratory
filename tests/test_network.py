from __future__ import annotations

import os
from pathlib import Path

import pytest

from freebsd_laboratory.network import IPv4LeasePool


def make_pool(tmp_path: Path) -> IPv4LeasePool:
    return IPv4LeasePool(
        "172.31.254.0/24",
        "172.31.254.10",
        "172.31.254.20",
        tmp_path / "leases",
    )


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is unavailable")
def test_allocate_refuses_symlink_lock(tmp_path: Path) -> None:
    pool = make_pool(tmp_path)
    pool.lease_dir.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    (pool.lease_dir / ".lock").symlink_to(target)

    with pytest.raises(OSError):
        pool.allocate("freebsd-lab-test")

    assert target.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is unavailable")
def test_release_refuses_symlink_lock(tmp_path: Path) -> None:
    pool = make_pool(tmp_path)
    address = pool.allocate("freebsd-lab-test")
    lock_path = pool.lease_dir / ".lock"
    lock_path.unlink()
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    lock_path.symlink_to(target)

    with pytest.raises(OSError):
        pool.release(address, "freebsd-lab-test")

    assert target.read_text(encoding="utf-8") == "unchanged"
    assert pool._lease_path(pool.start).exists()
