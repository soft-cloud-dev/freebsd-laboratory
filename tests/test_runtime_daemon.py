from __future__ import annotations

import os
from pathlib import Path

import pytest

from freebsd_laboratory.runtime_daemon import RuntimeConfig, RuntimeManager


class PortableRuntimeManager(RuntimeManager):
    def __init__(self, config: RuntimeConfig) -> None:
        super().__init__(config)
        self.destroyed: list[str] = []

    def destroy(self, name: str) -> dict[str, object]:
        self.validate_name(name)
        self.destroyed.append(name)
        record = self._load_registry(name) or {}
        guest_ip = record.get("guest_ip")
        if isinstance(guest_ip, str):
            self.pool.release(guest_ip, name)
        self._delete_registry(name)
        return {"name": name, "removed": []}

    def _discover_jails(self) -> set[str]:
        return set()

    def _discover_vms(self) -> set[str]:
        return set()

    def _discover_datasets(self) -> set[str]:
        return set()

    def _bridge_epairs(self) -> set[str]:
        return set()


def make_manager(tmp_path: Path) -> PortableRuntimeManager:
    return PortableRuntimeManager(
        RuntimeConfig(
            registry_dir=str(tmp_path / "registry"),
            lease_dir=str(tmp_path / "leases"),
            network_cidr="172.31.254.0/24",
            host_address="172.31.254.1",
            address_start="172.31.254.10",
            address_end="172.31.254.20",
        )
    )


def test_runtime_names_are_strictly_constrained() -> None:
    assert RuntimeManager.validate_name("freebsd-lab-abc123") == "freebsd-lab-abc123"
    with pytest.raises(ValueError):
        RuntimeManager.validate_name("../../etc/passwd")
    with pytest.raises(ValueError):
        RuntimeManager.validate_name("other-runtime")


def test_gc_keeps_live_owner_and_removes_stale_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = make_manager(tmp_path)
    live = "freebsd-lab-live"
    stale = "freebsd-lab-stale"
    live_ip = manager.pool.allocate(live)
    stale_ip = manager.pool.allocate(stale)
    manager._write_registry(
        {
            "schema": "softcloud.runtime/v1",
            "name": live,
            "type": "jail",
            "owner_pid": os.getpid(),
            "guest_ip": live_ip,
        }
    )
    manager._write_registry(
        {
            "schema": "softcloud.runtime/v1",
            "name": stale,
            "type": "bhyve",
            "owner_pid": 999999,
            "guest_ip": stale_ip,
        }
    )
    monkeypatch.setattr(manager, "_pid_alive", lambda pid: pid == os.getpid())

    result = manager.gc(stale_only=True)

    assert live in result["retained"]
    assert stale in result["cleaned"]
    assert stale in manager.destroyed
    assert manager._load_registry(live) is not None
    assert manager._load_registry(stale) is None
