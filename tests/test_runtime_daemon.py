from __future__ import annotations

import base64
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def ed25519_public_key() -> str:
    algorithm = b"ssh-ed25519"
    key = b"\x42" * 32
    blob = (
        len(algorithm).to_bytes(4, "big")
        + algorithm
        + len(key).to_bytes(4, "big")
        + key
    )
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def test_runtime_names_are_strictly_constrained() -> None:
    assert RuntimeManager.validate_name("freebsd-lab-abc123") == "freebsd-lab-abc123"
    with pytest.raises(ValueError):
        RuntimeManager.validate_name("../../etc/passwd")
    with pytest.raises(ValueError):
        RuntimeManager.validate_name("other-runtime")


def test_runtime_public_key_accepts_only_ed25519_material() -> None:
    key = ed25519_public_key()

    assert RuntimeManager.validate_ssh_public_key(key + " user@host") == key
    with pytest.raises(ValueError, match="Ed25519"):
        RuntimeManager.validate_ssh_public_key("ssh-rsa AAAA")
    with pytest.raises(ValueError, match="one line"):
        RuntimeManager.validate_ssh_public_key(key + "\ncommand=evil")


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


def test_stale_gc_is_scoped_to_requester_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager(tmp_path)
    own = "freebsd-lab-own"
    other = "freebsd-lab-other"
    manager._write_registry(owner_record(own, uid=1000, pid=100, digest="a" * 64))
    manager._write_registry(owner_record(other, uid=1001, pid=101, digest="b" * 64))
    monkeypatch.setattr(runtime_daemon, "process_matches", lambda pid, uid, digest: False)

    result = manager.gc(stale_only=True, requester_uid=1000)

    assert own in result["cleaned"]
    assert other in result["retained"]
    assert manager._load_registry(other) is not None
    assert other not in manager.destroyed


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


def test_lifecycle_gc_calls_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager(tmp_path)
    first_entered = threading.Event()
    release_first = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def registered() -> dict[str, dict[str, object]]:
        nonlocal calls
        with calls_lock:
            calls += 1
            current = calls
        if current == 1:
            first_entered.set()
            release_first.wait(timeout=2)
        return {}

    monkeypatch.setattr(manager, "_registered", registered)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(manager.gc, stale_only=True, requester_uid=1000)
        assert first_entered.wait(timeout=1)
        second = executor.submit(manager.gc, stale_only=True, requester_uid=1000)
        time.sleep(0.05)
        with calls_lock:
            assert calls == 1
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)


def test_jail_only_daemon_tolerates_missing_vm_bhyve(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    assert manager._discover_vms() == set()
    assert manager._vm_exists("freebsd-lab-test") is False
    with pytest.raises(RuntimeError, match="install vm-bhyve"):
        manager._require_vm_backend()


def test_missing_optional_command_is_nonfatal_when_check_is_false() -> None:
    result = RuntimeManager._run(["/definitely/missing-command"], check=False)

    assert result.returncode == 127


def test_bhyve_config_defaults_include_speed_backends() -> None:
    config = RuntimeConfig()
    assert config.vm_disk_backend == "zvol-clone"
    assert config.vm_zvol_snapshot == "zroot/vm/.zvol/freebsd-python@ready"
    assert config.vm_zvol_parent == "zroot/vm/.zvol"
    assert config.vm_dataset_parent == "zroot/vm"
    assert config.vm_memdisk_template == "freebsd-lab-memdisk"
    assert config.vm_disk_size == "8G"
    assert config.vm_memdisk_type == "swap"


def test_build_parser_accepts_disk_backend_flags() -> None:
    parser = runtime_daemon.build_parser()
    args = parser.parse_args([
        "--vm-disk-backend", "memdisk",
        "--vm-zvol-snapshot", "zroot/custom@ready",
        "--vm-disk-size", "4G",
        "--vm-memdisk-type", "malloc",
    ])
    assert args.vm_disk_backend == "memdisk"
    assert args.vm_zvol_snapshot == "zroot/custom@ready"
    assert args.vm_disk_size == "4G"
    assert args.vm_memdisk_type == "malloc"


def test_create_bhyve_zvol_clone_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = make_manager(tmp_path, vm_command="/bin/true")
    name = "freebsd-lab-clone1"
    key = ed25519_public_key()
    peer = PeerCredentials(pid=100, uid=1000, gid=1000)
    identity = ProcessIdentity(
        pid=100,
        uid=1000,
        started_at="Mon Jan 1 00:00:00 2026",
        digest="a" * 64,
    )
    monkeypatch.setattr(runtime_daemon, "query_process_identity", lambda pid: identity)

    commands_run: list[list[str]] = []

    def fake_run(cmd: Sequence[str], *, check: bool = True, timeout: float | None = 60) -> subprocess.CompletedProcess[str]:
        cmd_list = list(cmd)
        commands_run.append(cmd_list)
        if "list" in cmd_list and "-t" in cmd_list and "snapshot" in cmd_list:
            return subprocess.CompletedProcess(cmd_list, 0, "zroot/vm/.zvol/freebsd-python@ready\n", "")
        if "info" in cmd_list:
            return subprocess.CompletedProcess(cmd_list, 1, "", "")
        return subprocess.CompletedProcess(cmd_list, 0, "", "")

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(manager, "_require_vm_backend", lambda: None)
    monkeypatch.setattr(manager, "_ensure_bridge", lambda: None)
    monkeypatch.setattr(manager, "_ensure_vm_switch", lambda: None)

    result = manager.create_bhyve(name, 100, peer, key)
    assert result["name"] == name
    assert result["type"] == "bhyve"

    record = manager._load_registry(name)
    assert record is not None
    assert record["disk_backend"] == "zvol-clone"
    assert record["dataset"] == f"zroot/vm/{name}"
    assert record["vm_created"] is True

    # Verify zfs clone and vm create without -i was invoked
    flattened = [" ".join(c) for c in commands_run]
    assert any("zfs clone zroot/vm/.zvol/freebsd-python@ready zroot/vm/freebsd-lab-clone1/disk0" in cmd for cmd in flattened)
    assert any("/bin/true create -t freebsd-lab -C" in cmd and "-i" not in cmd for cmd in flattened)


def test_create_bhyve_memdisk_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RuntimeConfig(
        registry_dir=str(tmp_path / "registry"),
        lease_dir=str(tmp_path / "leases"),
        network_cidr="172.31.254.0/24",
        host_address="172.31.254.1",
        address_start="172.31.254.10",
        address_end="172.31.254.20",
        vm_command="/bin/true",
        vm_disk_backend="memdisk",
    )
    manager = PortableRuntimeManager(config)
    name = "freebsd-lab-mem1"
    key = ed25519_public_key()
    peer = PeerCredentials(pid=100, uid=1000, gid=1000)
    identity = ProcessIdentity(
        pid=100,
        uid=1000,
        started_at="Mon Jan 1 00:00:00 2026",
        digest="a" * 64,
    )
    monkeypatch.setattr(runtime_daemon, "query_process_identity", lambda pid: identity)

    commands_run: list[list[str]] = []

    def fake_run(cmd: Sequence[str], *, check: bool = True, timeout: float | None = 60) -> subprocess.CompletedProcess[str]:
        cmd_list = list(cmd)
        commands_run.append(cmd_list)
        if "mdconfig" in cmd_list and "-a" in cmd_list:
            return subprocess.CompletedProcess(cmd_list, 0, "md3\n", "")
        if "info" in cmd_list:
            return subprocess.CompletedProcess(cmd_list, 1, "", "")
        if "list" in cmd_list and "snapshot" in cmd_list:
            return subprocess.CompletedProcess(cmd_list, 0, "zroot/vm/.zvol/freebsd-python@ready\n", "")
        return subprocess.CompletedProcess(cmd_list, 0, "", "")

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(manager, "_require_vm_backend", lambda: None)
    monkeypatch.setattr(manager, "_ensure_bridge", lambda: None)
    monkeypatch.setattr(manager, "_ensure_vm_switch", lambda: None)

    result = manager.create_bhyve(name, 100, peer, key)
    assert result["name"] == name
    assert result["type"] == "bhyve"

    record = manager._load_registry(name)
    assert record is not None
    assert record["disk_backend"] == "memdisk"
    assert record["md_unit"] == "md3"

    flattened = [" ".join(c) for c in commands_run]
    assert any("mdconfig -a -t swap -s 8G" in cmd for cmd in flattened)
    assert any("dd if=/dev/zvol/zroot/vm/.zvol/freebsd-lab-mem1-src of=/dev/md3" in cmd for cmd in flattened)
    assert any("/bin/true create -t freebsd-lab-memdisk -C" in cmd for cmd in flattened)
    assert any("/bin/true set freebsd-lab-mem1 disk0_name=/dev/md3" in cmd for cmd in flattened)


def test_destroy_cleans_up_md_unit_and_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RuntimeConfig(
        registry_dir=str(tmp_path / "registry"),
        lease_dir=str(tmp_path / "leases"),
        network_cidr="172.31.254.0/24",
        host_address="172.31.254.1",
        address_start="172.31.254.10",
        address_end="172.31.254.20",
        vm_command="/bin/true",
    )
    manager = RuntimeManager(config)
    name = "freebsd-lab-memcleanup"
    record = {
        "schema": "softcloud.runtime/v1",
        "name": name,
        "type": "bhyve",
        "owner_pid": 100,
        "owner_uid": 1000,
        "owner_gid": 1000,
        "owner_started_at": "Mon Jan 1 00:00:00 2026",
        "owner_process_digest": "a" * 64,
        "guest_ip": "172.31.254.10",
        "bridge": "labbridge0",
        "disk_backend": "memdisk",
        "md_unit": "md5",
        "dataset": f"zroot/vm/{name}",
        "vm_created": True,
    }
    manager._write_registry(record)

    commands_run: list[list[str]] = []

    def fake_run(cmd: Sequence[str], *, check: bool = True, timeout: float | None = 60) -> subprocess.CompletedProcess[str]:
        cmd_list = list(cmd)
        commands_run.append(cmd_list)
        return subprocess.CompletedProcess(cmd_list, 0, "", "")

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(manager, "_vm_available", lambda: True)
    monkeypatch.setattr(manager, "_vm_exists", lambda n: False)
    monkeypatch.setattr(manager, "_dataset_exists", lambda d: True)
    monkeypatch.setattr(manager, "_md_exists", lambda u: False)

    result = manager.destroy(name, requester_uid=1000)
    assert result["name"] == name
    assert "md5" in result["removed"]
    assert f"zroot/vm/{name}" in result["removed"]

    flattened = [" ".join(c) for c in commands_run]
    assert any("mdconfig -d -u 5" in cmd for cmd in flattened)
    assert any(f"zfs destroy -r -f zroot/vm/{name}" in cmd for cmd in flattened)

