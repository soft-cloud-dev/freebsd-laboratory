from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from freebsd_laboratory.runtime_daemon import RuntimeConfig, RuntimeManager, RuntimeOwner


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


def owner(pid: int = 321, uid: int = 1000, digest: str = "a" * 64) -> RuntimeOwner:
    return RuntimeOwner(
        pid=pid,
        uid=uid,
        gid=uid,
        started_at="Mon Jan 1 00:00:00 2026",
        process_digest=digest,
    )


def registered_jail(name: str, runtime_owner: RuntimeOwner) -> dict[str, object]:
    return {
        "schema": "softcloud.runtime/v1",
        "name": name,
        "type": "jail",
        **runtime_owner.registry_fields(),
        "dataset": f"zroot/jails/containers/{name}",
        "guest_ip": "172.31.254.10",
    }


def test_same_owner_registered_runtime_is_reconciled_before_recreate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager(tmp_path)
    name = "freebsd-lab-restart"
    runtime_owner = owner()
    manager._write_registry(registered_jail(name, runtime_owner))
    calls: list[tuple[str, int | None, bool]] = []

    def fake_destroy(
        runtime_name: str,
        *,
        requester_uid: int | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        calls.append((runtime_name, requester_uid, force))
        manager._delete_registry(runtime_name)
        return {"name": runtime_name, "removed": []}

    monkeypatch.setattr(manager, "destroy", fake_destroy)

    manager._reconcile_registered_before_create(name, runtime_owner)

    assert calls == [(name, runtime_owner.uid, False)]
    assert manager._load_registry(name) is None


def test_different_owner_registered_runtime_is_not_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager(tmp_path)
    name = "freebsd-lab-owned"
    manager._write_registry(registered_jail(name, owner(pid=400, uid=2000, digest="b" * 64)))

    def unexpected_destroy(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("foreign runtime must not be destroyed")

    monkeypatch.setattr(manager, "destroy", unexpected_destroy)

    with pytest.raises(RuntimeError, match="Runtime already registered"):
        manager._reconcile_registered_before_create(name, owner())

    assert manager._load_registry(name) is not None


def test_orphaned_dataset_is_removed_before_jail_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager(tmp_path)
    name = "freebsd-lab-orphan"
    dataset = manager._jail_dataset(name)
    state = {"dataset": True}
    calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(manager, "_jail_exists", lambda runtime_name: False)
    monkeypatch.setattr(manager, "_dataset_exists", lambda dataset_name: state["dataset"])

    def fake_destroy(
        runtime_name: str,
        *,
        requester_uid: int | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        calls.append((runtime_name, force))
        state["dataset"] = False
        return {"name": runtime_name, "removed": [dataset]}

    monkeypatch.setattr(manager, "destroy", fake_destroy)

    manager._reconcile_orphaned_jail_before_create(name, dataset)

    assert calls == [(name, True)]
    assert state["dataset"] is False


def test_destroy_preserves_registry_when_dataset_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager(tmp_path)
    name = "freebsd-lab-stuck"
    runtime_owner = owner()
    manager._write_registry(registered_jail(name, runtime_owner))

    monkeypatch.setattr(manager, "_vm_exists", lambda runtime_name: False)
    monkeypatch.setattr(manager, "_jail_exists", lambda runtime_name: False)
    monkeypatch.setattr(manager, "_interface_exists", lambda interface: False)
    monkeypatch.setattr(manager, "_dataset_exists", lambda dataset: True)
    monkeypatch.setattr(
        manager,
        "_run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, "", "dataset is busy"),
    )

    with pytest.raises(RuntimeError, match="Runtime cleanup incomplete"):
        manager.destroy(name, requester_uid=runtime_owner.uid)

    assert manager._load_registry(name) is not None


def test_destroy_deletes_registry_after_dataset_is_observed_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager(tmp_path)
    name = "freebsd-lab-clean"
    runtime_owner = owner()
    manager._write_registry(registered_jail(name, runtime_owner))
    state = {"dataset": True}

    monkeypatch.setattr(manager, "_vm_exists", lambda runtime_name: False)
    monkeypatch.setattr(manager, "_jail_exists", lambda runtime_name: False)
    monkeypatch.setattr(manager, "_interface_exists", lambda interface: False)
    monkeypatch.setattr(manager, "_dataset_exists", lambda dataset: state["dataset"])

    def fake_run(
        command: list[str],
        *,
        check: bool = True,
        timeout: float | None = 60,
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["zfs", "destroy", "-r"]:
            state["dataset"] = False
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(manager, "_run", fake_run)

    result = manager.destroy(name, requester_uid=runtime_owner.uid)

    assert manager._load_registry(name) is None
    assert state["dataset"] is False
    assert manager._jail_dataset(name) in result["removed"]
