from __future__ import annotations

from pathlib import Path

import pytest

import freebsd_laboratory.runtime_daemon as runtime_daemon
from freebsd_laboratory.peer_credentials import PeerCredentials
from freebsd_laboratory.process_identity import ProcessIdentity
from freebsd_laboratory.runtime_daemon import RuntimeConfig, RuntimeManager


class PortableRuntimeManager(RuntimeManager):
    def __init__(self, config: RuntimeConfig) -> None:
        super().__init__(config)
        self.destroyed: list[str] = []

    def destroy(
        self,
        name: str,
        *,
        requester_uid: int | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        self.validate_name(name)
        record = self._load_registry(name) or {}
        if not force:
            self._authorize_record(record, requester_uid)
        self.destroyed.append(name)
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


def make_manager(tmp_path: Path, *, vm_command: str = "/missing/vm") -> PortableRuntimeManager:
    return PortableRuntimeManager(
        RuntimeConfig(
            registry_dir=str(tmp_path / "registry"),
            lease_dir=str(tmp_path / "leases"),
            network_cidr="172.31.254.0/24",
            host_address="172.31.254.1",
            address_start="172.31.254.10",
            address_end="172.31.254.20",
            vm_command=vm_command,
        )
    )


def owner_record(
    name: str,
    *,
    uid: int,
    pid: int,
    digest: str,
    guest_ip: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema": "softcloud.runtime/v1",
        "name": name,
        "type": "jail",
        "owner_pid": pid,
        "owner_uid": uid,
        "owner_gid": uid,
        "owner_started_at": "Mon Jan 1 00:00:00 2026",
        "owner_process_digest": digest,
    }
    if guest_ip is not None:
        record["guest_ip"] = guest_ip
    return record


def test_runtime_names_are_strictly_constrained() -> None:
    assert RuntimeManager.validate_name("freebsd-lab-abc123") == "freebsd-lab-abc123"
    with pytest.raises(ValueError):
        RuntimeManager.validate_name("../../etc/passwd")
    with pytest.raises(ValueError):
        RuntimeManager.validate_name("other-runtime")


def test_create_owner_is_bound_to_authenticated_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = PeerCredentials(pid=321, uid=1000, gid=1000)
    identity = ProcessIdentity(
        pid=321,
        uid=1000,
        started_at="Mon Jan 1 00:00:00 2026",
        digest="a" * 64,
    )
    monkeypatch.setattr(runtime_daemon, "query_process_identity", lambda pid: identity)

    owner = RuntimeManager._owner_from_peer(peer, 321)

    assert owner.registry_fields()["owner_uid"] == 1000
    assert owner.registry_fields()["owner_process_digest"] == "a" * 64
    with pytest.raises(PermissionError, match="does not match authenticated peer"):
        RuntimeManager._owner_from_peer(peer, 999)


def test_create_rejects_pid_not_owned_by_peer_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = PeerCredentials(pid=321, uid=1000, gid=1000)
    identity = ProcessIdentity(
        pid=321,
        uid=2000,
        started_at="Mon Jan 1 00:00:00 2026",
        digest="a" * 64,
    )
    monkeypatch.setattr(runtime_daemon, "query_process_identity", lambda pid: identity)

    with pytest.raises(PermissionError, match="does not own pid"):
        RuntimeManager._owner_from_peer(peer, 321)


def test_destroy_requires_owner_uid_or_root(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    name = "freebsd-lab-owned"
    manager._write_registry(owner_record(name, uid=1000, pid=10, digest="a" * 64))

    with pytest.raises(PermissionError, match="another Unix user"):
        manager.destroy(name, requester_uid=1001)

    assert manager._load_registry(name) is not None
    manager.destroy(name, requester_uid=0)
    assert manager._load_registry(name) is None


def test_gc_uses_process_fingerprint_not_bare_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager(tmp_path)
    live = "freebsd-lab-live"
    reused = "freebsd-lab-reused"
    manager._write_registry(owner_record(live, uid=1000, pid=100, digest="a" * 64))
    manager._write_registry(owner_record(reused, uid=1000, pid=100, digest="b" * 64))
    monkeypatch.setattr(
        runtime_daemon,
        "process_matches",
        lambda pid, uid, digest: digest == "a" * 64,
    )

    result = manager.gc(stale_only=True, requester_uid=1000)

    assert live in result["retained"]
    assert reused in result["cleaned"]
    assert reused in manager.destroyed


def test_non_stale_gc_is_scoped_to_requester_uid(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    own = "freebsd-lab-own"
    other = "freebsd-lab-other"
    manager._write_registry(owner_record(own, uid=1000, pid=100, digest="a" * 64))
    manager._write_registry(owner_record(other, uid=1001, pid=101, digest="b" * 64))

    result = manager.gc(stale_only=False, requester_uid=1000)

    assert own in result["cleaned"]
    assert other in result["retained"]
    assert manager._load_registry(other) is not None


def test_jail_only_daemon_tolerates_missing_vm_bhyve(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    assert manager._discover_vms() == set()
    assert manager._vm_exists("freebsd-lab-test") is False
    with pytest.raises(RuntimeError, match="install vm-bhyve"):
        manager._require_vm_backend()


def test_missing_optional_command_is_nonfatal_when_check_is_false() -> None:
    result = RuntimeManager._run(["/definitely/missing-command"], check=False)

    assert result.returncode == 127
