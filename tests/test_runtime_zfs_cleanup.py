from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from freebsd_laboratory.runtime_daemon import RuntimeConfig, RuntimeManager


def make_manager(tmp_path: Path) -> RuntimeManager:
    return RuntimeManager(
        RuntimeConfig(
            registry_dir=str(tmp_path / "registry"),
            lease_dir=str(tmp_path / "leases"),
            network_cidr="172.31.254.0/24",
            host_address="172.31.254.1",
            address_start="172.31.254.10",
            address_end="172.31.254.20",
            vm_command="/missing/vm",
        )
    )


def test_gc_force_unmounts_mounted_runtime_dataset_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager(tmp_path)
    name = "freebsd-lab-mounted"
    dataset = manager._jail_dataset(name)
    state = {"dataset": True}
    commands: list[list[str]] = []

    manager._write_registry(
        {
            "schema": "softcloud.runtime/v1",
            "name": name,
            "type": "jail",
            "owner_uid": 1000,
            "dataset": dataset,
        }
    )

    monkeypatch.setattr(manager, "_vm_exists", lambda runtime_name: False)
    monkeypatch.setattr(manager, "_jail_exists", lambda runtime_name: False)
    monkeypatch.setattr(manager, "_interface_exists", lambda interface: False)
    monkeypatch.setattr(manager, "_dataset_exists", lambda dataset_name: state["dataset"])
    monkeypatch.setattr(manager, "_discover_jails", lambda: set())
    monkeypatch.setattr(manager, "_discover_vms", lambda: set())
    monkeypatch.setattr(
        manager,
        "_discover_datasets",
        lambda: {dataset} if state["dataset"] else set(),
    )
    monkeypatch.setattr(manager, "_bridge_epairs", lambda: set())

    def fake_run(
        command: list[str],
        *,
        check: bool = True,
        timeout: float | None = 60,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:2] == ["zfs", "destroy"]:
            if "-f" not in command:
                return subprocess.CompletedProcess(command, 1, "", "dataset is busy")
            state["dataset"] = False
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(manager, "_run", fake_run)

    first = manager.gc(stale_only=False, requester_uid=0)
    second = manager.gc(stale_only=False, requester_uid=0)

    assert ["zfs", "destroy", "-r", "-f", dataset] in commands
    assert first["cleaned"] == [name]
    assert first["errors"] == {}
    assert second["cleaned"] == []
    assert second["errors"] == {}
