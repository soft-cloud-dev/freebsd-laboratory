from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from freebsd_laboratory.network import IPv4LeasePool
from freebsd_laboratory.process_identity import (
    _pid_exists,
    process_matches,
)
from freebsd_laboratory.runtime_daemon import (
    RuntimeConfig,
    RuntimeManager,
    _configure_socket,
)
from freebsd_laboratory.ssh_transport import SSHTransport
from freebsd_laboratory.telemetry import (
    capture_exception,
    capture_kernel_error,
    flush_sentry,
    init_sentry,
)
from freebsd_laboratory.verify import verify_bundle


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
            ssh_public_key=str(tmp_path / "id_ed25519.pub"),
        )
    )


def valid_ed25519_public_key() -> str:
    algorithm = b"ssh-ed25519"
    key = b"\x42" * 32
    blob = (
        len(algorithm).to_bytes(4, "big")
        + algorithm
        + len(key).to_bytes(4, "big")
        + key
    )
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


# --- Telemetry Hardening Tests ---

def test_telemetry_graceful_when_no_dsn() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert init_sentry("test-comp") is False
        assert capture_exception(ValueError("boom"), component="test", operation="op") is None
        assert capture_kernel_error("Error", "Detail") is None
        flush_sentry()


def test_telemetry_graceful_when_sentry_sdk_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import freebsd_laboratory.telemetry as tm
    monkeypatch.setattr(tm, "sentry_sdk", None)
    with patch.dict(os.environ, {"SENTRY_DSN": "https://fake@sentry.io/1"}):
        assert tm.init_sentry("test-comp") is False
        assert tm.capture_exception(RuntimeError("test"), component="test", operation="op") is None
        assert tm.capture_kernel_error("Err", "val") is None
        tm.flush_sentry()


# --- Process Identity Hardening Tests ---

def test_process_matches_rejects_bool_and_invalid_types() -> None:
    assert process_matches(True, 1000, "a" * 64) is False  # type: ignore[arg-type]
    assert process_matches(os.getpid(), True, "a" * 64) is False  # type: ignore[arg-type]
    assert process_matches(os.getpid(), 1000, "") is False
    assert process_matches(-5, 1000, "a" * 64) is False
    assert process_matches(os.getpid(), -1, "a" * 64) is False


def test_pid_exists_rejects_invalid_values() -> None:
    assert _pid_exists(True) is False  # type: ignore[arg-type]
    assert _pid_exists(0) is False
    assert _pid_exists(1) is False
    assert _pid_exists(-10) is False
    assert _pid_exists("not-an-int") is False  # type: ignore[arg-type]


# --- Runtime Daemon Hardening Tests ---

def test_authorize_record_rejects_bool_and_invalid_requester_uids() -> None:
    manager = make_manager(Path("/tmp"))
    record = {"owner_uid": 1000}

    with pytest.raises(PermissionError, match="no authenticated requester"):
        manager._authorize_record(record, None)

    with pytest.raises(PermissionError, match="no authenticated requester"):
        manager._authorize_record(record, True)  # type: ignore[arg-type]

    with pytest.raises(PermissionError, match="no authenticated requester"):
        manager._authorize_record(record, -1)

    with pytest.raises(PermissionError, match="owned by another"):
        manager._authorize_record(record, 1001)

    with pytest.raises(PermissionError, match="owned by another"):
        manager._authorize_record({"owner_uid": True}, 1)  # type: ignore[arg-type]

    # Root always authorized
    manager._authorize_record(record, 0)
    # Same UID authorized
    manager._authorize_record(record, 1000)


def test_install_jail_authorized_key_prevents_symlink_attacks(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    public_key = valid_ed25519_public_key()

    jail_root = tmp_path / "jail"
    user_home = jail_root / "home" / "freebsd"
    user_home.mkdir(parents=True)

    # Mock pw usershow (7 fields: name:password:uid:gid:gecos:home:shell)
    pw_output = "freebsd:*:1000:1000:User:/home/freebsd:/bin/sh\n"
    manager._run = lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, pw_output, "")  # type: ignore[method-assign]

    chown_calls: list[dict[str, object]] = []

    def mock_chown(path: object, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
        chown_calls.append({"path": str(path), "uid": uid, "gid": gid, "follow_symlinks": follow_symlinks})

    with patch("os.chown", side_effect=mock_chown):
        # Test normal key installation
        manager._install_jail_authorized_key(str(jail_root), public_key)
        auth_keys = user_home / ".ssh" / "authorized_keys"
        assert auth_keys.is_file()
        assert auth_keys.read_text(encoding="utf-8") == public_key + "\n"
        assert auth_keys.stat().st_mode & 0o777 == 0o600
        assert all(call["follow_symlinks"] is False for call in chown_calls)

        # Test symlink attack on authorized_keys: target outside jail should NOT be overwritten
        victim_file = tmp_path / "victim.txt"
        victim_file.write_text("critical host data", encoding="utf-8")
        auth_keys.unlink()
        auth_keys.symlink_to(victim_file)

        manager._install_jail_authorized_key(str(jail_root), public_key)
        assert not auth_keys.is_symlink()
        assert auth_keys.is_file()
        assert victim_file.read_text(encoding="utf-8") == "critical host data"


def test_install_jail_authorized_key_rejects_escaping_home(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    public_key = valid_ed25519_public_key()

    jail_root = tmp_path / "jail"
    jail_root.mkdir(parents=True)

    # Mock home that escapes jail root (7 fields)
    pw_output = "freebsd:*:1000:1000:User:/../../etc:/bin/sh\n"
    manager._run = lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, pw_output, "")  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="escapes the jail root"):
        manager._install_jail_authorized_key(str(jail_root), public_key)


def test_destroy_does_not_destroy_arbitrary_interfaces(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    name = "freebsd-lab-malicious"
    # A corrupted/tampered registry entry pointing to host physical interface
    manager._write_registry({
        "schema": "softcloud.runtime/v1",
        "name": name,
        "type": "jail",
        "owner_uid": 1000,
        "epair_host": "em0",
    })

    commands: list[list[str]] = []
    manager._run = lambda cmd, **kwargs: commands.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")  # type: ignore[method-assign]
    manager._dataset_exists = lambda ds: False  # type: ignore[method-assign]
    manager._jail_exists = lambda j: False  # type: ignore[method-assign]
    manager._vm_exists = lambda v: False  # type: ignore[method-assign]

    manager.destroy(name, requester_uid=1000)

    # Verify ifconfig em0 destroy was NOT called
    assert not any(cmd[:2] == ["ifconfig", "em0"] for cmd in commands)


def test_configure_socket_error_on_missing_group(tmp_path: Path) -> None:
    sock_path = tmp_path / "test.sock"
    sock_path.touch()
    with pytest.raises(RuntimeError, match="socket group does not exist"):
        _configure_socket(sock_path, "definitely_nonexistent_group_12345")


# --- Verify Bundle Hardening Tests ---

def test_verify_bundle_rejects_symlinked_manifest(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    real_manifest = tmp_path / "real_manifest.json"
    real_manifest.write_text("{}", encoding="utf-8")
    (bundle / "manifest.json").symlink_to(real_manifest)

    with pytest.raises(ValueError, match="manifest.json is missing"):
        verify_bundle(bundle)


def test_verify_bundle_rejects_symlinked_artifact(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    artifact_content = b'{"events":[]}\n'
    artifact_hash = hashlib.sha256(artifact_content).hexdigest()

    manifest = {
        "schema": "softcloud.lab-evidence-manifest/v1",
        "lab_id": "test-lab",
        "session_id": "session123",
        "event_count": 0,
        "attestation": "self-recorded",
        "generated_at": "2026-01-01T00:00:00Z",
        "artifacts": {
            "evidence.json": {
                "sha256": artifact_hash,
                "size": len(artifact_content),
            }
        },
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Point evidence.json as a symlink
    outside_file = tmp_path / "outside_evidence.json"
    outside_file.write_bytes(artifact_content)
    (bundle / "evidence.json").symlink_to(outside_file)

    with pytest.raises(ValueError, match="Evidence artifact is missing"):
        verify_bundle(bundle)


# --- IPv4 Lease Pool Hardening Tests ---

def test_ipv4_pool_release_bounds_and_error_resilience(tmp_path: Path) -> None:
    pool = IPv4LeasePool(
        network="172.31.254.0/24",
        start="172.31.254.10",
        end="172.31.254.20",
        lease_dir=tmp_path / "leases",
    )

    # Valid allocation and release
    addr = pool.allocate("owner-a")
    assert addr == "172.31.254.10"
    assert pool.release(addr, "owner-a") is True
    assert pool.release(addr, "owner-a") is False

    # Out of network / out of range addresses
    assert pool.release("10.0.0.1", "owner-a") is False
    assert pool.release("172.31.254.5", "owner-a") is False
    assert pool.release("172.31.254.25", "owner-a") is False
    assert pool.release("invalid-ip", "owner-a") is False


# --- SSH Transport Hardening Tests ---

def test_ssh_transport_validates_bind_address_and_stage_target(tmp_path: Path) -> None:
    transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key=str(tmp_path / "nonexistent_key"),
        known_hosts_file=tmp_path / "known_hosts",
        bind_address="invalid-ip-addr",
    )

    with pytest.raises(ValueError, match="Invalid bind_address"):
        transport.assert_available()

    # Valid IP
    key_file = tmp_path / "key"
    key_file.touch()
    valid_transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key=str(key_file),
        known_hosts_file=tmp_path / "known_hosts",
        bind_address="127.0.0.1",
        ssh_command="/bin/sh",
        scp_command="/bin/sh",
    )
    valid_transport.assert_available()

    with pytest.raises(ValueError, match="timeout must be a positive integer"):
        valid_transport.wait_until_ready(0)

    with pytest.raises(RuntimeError, match="File to stage does not exist"):
        valid_transport.stage(tmp_path / "missing-file.json", "/tmp/remote")


def test_ssh_transport_validates_user_and_host(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.touch()

    # Invalid user with command injection characters
    bad_user_transport = SSHTransport(
        host="172.31.254.10",
        user="user;rm -rf /",
        private_key=str(key_file),
        known_hosts_file=tmp_path / "known_hosts",
        ssh_command="/bin/sh",
        scp_command="/bin/sh",
    )
    with pytest.raises(ValueError, match="Invalid SSH user"):
        bad_user_transport.assert_available()

    # Invalid host with option injection
    bad_host_transport = SSHTransport(
        host="-oProxyCommand=evil",
        user="freebsd",
        private_key=str(key_file),
        known_hosts_file=tmp_path / "known_hosts",
        ssh_command="/bin/sh",
        scp_command="/bin/sh",
    )
    with pytest.raises(ValueError, match="Invalid host address"):
        bad_host_transport.assert_available()


# --- Signing & Verification Hardening Tests ---

def test_verify_bundle_rejects_symlinked_signature(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    artifact_content = b'{"events":[]}\n'
    artifact_hash = hashlib.sha256(artifact_content).hexdigest()

    manifest = {
        "schema": "softcloud.lab-evidence-manifest/v1",
        "lab_id": "test-lab",
        "session_id": "session123",
        "event_count": 0,
        "attestation": "self-recorded",
        "generated_at": "2026-01-01T00:00:00Z",
        "artifacts": {
            "evidence.json": {
                "sha256": artifact_hash,
                "size": len(artifact_content),
            }
        },
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (bundle / "evidence.json").write_bytes(artifact_content)

    fake_sig = tmp_path / "fake.sig.json"
    fake_sig.write_text("{}", encoding="utf-8")
    (bundle / "manifest.sig.json").symlink_to(fake_sig)

    with pytest.raises(ValueError, match="Evidence signature must not be a symbolic link"):
        verify_bundle(bundle)


def test_signing_rejects_symlinked_keys_and_manifests(tmp_path: Path) -> None:
    from freebsd_laboratory.signing import (
        load_private_key,
        load_public_key,
        sign_manifest,
        verify_manifest_signature,
    )

    real_key = tmp_path / "real_key.pem"
    real_key.write_text("test key content", encoding="utf-8")

    symlinked_key = tmp_path / "sym_key.pem"
    symlinked_key.symlink_to(real_key)

    with pytest.raises(ValueError, match="must be a regular file"):
        load_private_key(symlinked_key)

    with pytest.raises(ValueError, match="must be a regular file"):
        load_public_key(symlinked_key)

    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text("{}", encoding="utf-8")
    symlinked_manifest = tmp_path / "sym_manifest.json"
    symlinked_manifest.symlink_to(manifest_file)

    with pytest.raises(ValueError, match="Manifest path must be a regular file"):
        sign_manifest(symlinked_manifest, real_key, "key1")

    with pytest.raises(ValueError, match="Manifest path must be a regular file"):
        verify_manifest_signature(symlinked_manifest, real_key)


# --- Port Leases & Network Concurrency Tests ---

def test_local_port_pool_validation_and_type_safety(tmp_path: Path) -> None:
    from freebsd_laboratory.port_leases import LocalPortLeasePool

    # Invalid port range types
    with pytest.raises(ValueError, match="integer TCP ports"):
        LocalPortLeasePool("1024", 2000, tmp_path)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="integer TCP ports"):
        LocalPortLeasePool(1024, True, tmp_path)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Invalid bind_address"):
        LocalPortLeasePool(1024, 2000, tmp_path, bind_address="not-an-ip")

    pool = LocalPortLeasePool(1024, 2000, tmp_path)
    # Release with boolean uid/pid safely returns without crash
    pool.release([1025], "owner", True, 1000, "a" * 64)  # type: ignore[arg-type]
    pool.release([1025], "owner", 100, True, "a" * 64)  # type: ignore[arg-type]


def test_ipv4_pool_concurrent_allocations_and_symlinks(tmp_path: Path) -> None:
    import threading

    pool = IPv4LeasePool(
        network="172.31.254.0/24",
        start="172.31.254.10",
        end="172.31.254.40",
        lease_dir=tmp_path / "leases",
    )

    with pytest.raises(ValueError, match="Lease owner must be a non-empty string"):
        pool.allocate("")

    # Multi-threaded concurrent allocations
    allocated: list[str] = []
    errors: list[Exception] = []

    def worker(worker_id: int) -> None:
        try:
            addr = pool.allocate(f"worker-{worker_id}")
            allocated.append(addr)
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(allocated) == 20
    assert len(set(allocated)) == 20

    # Symlink in lease directory is cleaned during clear_orphans
    sym_lease = tmp_path / "leases" / "172.31.254.99.lease"
    target = tmp_path / "target.txt"
    target.write_text("stay safe", encoding="utf-8")
    sym_lease.symlink_to(target)

    pool.clear_orphans(set())
    assert not sym_lease.exists()
    assert target.exists()


# --- Remote Connection Hardening Tests ---

def test_remote_connection_dict_and_symlink_hardening(tmp_path: Path) -> None:
    from freebsd_laboratory.remote_connection import (
        connection_ports,
        _validate_port_sequence,
        rewrite_connection_file,
        restore_connection_file,
    )

    with pytest.raises(ValueError, match="Connection document must be a dictionary"):
        connection_ports("not-a-dict")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Expected 5 Jupyter connection ports"):
        _validate_port_sequence("12345")  # type: ignore[arg-type]

    # Symlink protection on connection file
    conn_file = tmp_path / "connection.json"
    conn_file.write_text(json.dumps({
        "ip": "127.0.0.1",
        "transport": "tcp",
        "shell_port": 1001,
        "iopub_port": 1002,
        "stdin_port": 1003,
        "control_port": 1004,
        "hb_port": 1005,
    }), encoding="utf-8")

    class FakeParent:
        connection_file = str(conn_file)
        ip = "127.0.0.1"

    parent = FakeParent()
    host_path, orig_ip, orig_ports, tunnel_ports = rewrite_connection_file(parent, ports=(2001, 2002, 2003, 2004, 2005))
    assert host_path == conn_file
    assert tunnel_ports == (2001, 2002, 2003, 2004, 2005)

    restore_connection_file(parent, orig_ip, orig_ports)
    restored = json.loads(conn_file.read_text(encoding="utf-8"))
    assert restored["shell_port"] == 1001


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


# --- Registry Symlink Hardening Tests ---

def test_runtime_daemon_skips_symlinked_registry_files(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir(parents=True, exist_ok=True)

    # Legitimate registry file
    legit_name = "freebsd-lab-legit"
    manager._write_registry(owner_record(legit_name, uid=1000, pid=10, digest="a" * 64))

    # Malicious symlink
    sym_name = "freebsd-lab-symlink"
    victim_file = tmp_path / "victim.json"
    victim_file.write_text(json.dumps(owner_record(sym_name, uid=1000, pid=10, digest="a" * 64)), encoding="utf-8")
    (reg_dir / f"{sym_name}.json").symlink_to(victim_file)

    registered = manager._registered()
    assert legit_name in registered
    assert sym_name not in registered
    assert manager._load_registry(sym_name) is None


# --- Peer Credentials Tests ---

def test_peer_credentials_error_paths() -> None:
    import socket
    from freebsd_laboratory.peer_credentials import freebsd_peer_credentials

    # Non-AF_UNIX socket
    inet_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError):
            freebsd_peer_credentials(inet_sock)
    finally:
        inet_sock.close()


# --- Daemon Socket IPC Concurrency & Abrupt Disconnect Tests ---

def test_daemon_socket_ipc_concurrency_and_abrupt_disconnects(tmp_path: Path) -> None:
    import socket
    import tempfile
    import threading
    import uuid
    from freebsd_laboratory.peer_credentials import PeerCredentials
    from freebsd_laboratory.runtime_daemon import (
        RuntimeRequestHandler,
        ThreadingUnixServer,
    )

    short_dir = Path(tempfile.mkdtemp(prefix="fbl_", dir="/tmp"))
    sock_path = short_dir / f"r_{uuid.uuid4().hex[:8]}.sock"
    manager = make_manager(tmp_path)

    # Mock peer credentials for non-FreeBSD test environment
    fake_peer = PeerCredentials(pid=os.getpid(), uid=os.getuid(), gid=os.getgid())

    class TestRequestHandler(RuntimeRequestHandler):
        def handle(self) -> None:
            try:
                # Bypass getsockopt for simulated test
                peer = fake_peer
                raw = self.rfile.readline(64 * 1024 + 1)
                if not raw:
                    return
                if len(raw) > 64 * 1024:
                    raise ValueError("request exceeded size limit")
                request = json.loads(raw.decode("utf-8"))
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                result = self._dispatch(request, peer)
                self._reply({"ok": True, "result": result})
            except Exception as error:
                self._reply({"ok": False, "error": str(error)})

    TestRequestHandler.manager = manager
    server = ThreadingUnixServer(str(sock_path), TestRequestHandler)
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    server_thread.daemon = True
    server_thread.start()

    try:
        # 1. Normal ping request
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(sock_path))
            client.sendall(b'{"action":"ping"}\n')
            response = json.loads(client.recv(4096).decode("utf-8"))
            assert response["ok"] is True
            assert response["result"]["service"] == "freebsd-laboratory-runtime"

        # 2. Oversized request (>64KB)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(sock_path))
            client.sendall(b'{"action":"' + b'x' * (70 * 1024) + b'"}\n')
            response = json.loads(client.recv(4096).decode("utf-8"))
            assert response["ok"] is False
            assert "exceeded size limit" in response["error"]

        # 3. Invalid JSON request
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(sock_path))
            client.sendall(b'not json\n')
            response = json.loads(client.recv(4096).decode("utf-8"))
            assert response["ok"] is False

        # 4. Non-object request
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(sock_path))
            client.sendall(b'[1, 2, 3]\n')
            response = json.loads(client.recv(4096).decode("utf-8"))
            assert response["ok"] is False
            assert "must be an object" in response["error"]

        # 5. Type-confused request fields
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(sock_path))
            client.sendall(b'{"action":"create-jail", "name": 123, "owner_pid": true}\n')
            response = json.loads(client.recv(4096).decode("utf-8"))
            assert response["ok"] is False

        # 6. Abrupt disconnects (connect and immediately close)
        for _ in range(10):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(sock_path))

        # 7. Partial write and close
        for _ in range(10):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(sock_path))
                client.sendall(b'{"action":')

        # 8. Rapid concurrent connections
        concurrency_errors: list[Exception] = []

        def concurrent_client(index: int) -> None:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(5.0)
                    client.connect(str(sock_path))
                    client.sendall(b'{"action":"ping"}\n')
                    data = client.recv(4096)
                    resp = json.loads(data.decode("utf-8"))
                    assert resp["ok"] is True
            except Exception as exc:
                concurrency_errors.append(exc)

        threads = [threading.Thread(target=concurrent_client, args=(i,)) for i in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not concurrency_errors
    finally:
        server.shutdown()
        server.server_close()
        sock_path.unlink(missing_ok=True)
        try:
            short_dir.rmdir()
        except OSError:
            pass


# --- Adversarial Deep Hardening Test Suites ---

def test_peer_credentials_comprehensive_checks() -> None:
    import ctypes
    import socket
    from freebsd_laboratory.peer_credentials import (
        _Xucred,
        XUCRED_VERSION,
        freebsd_peer_credentials,
    )

    # 1. Non-socket instance
    with pytest.raises(TypeError, match="socket.socket instance"):
        freebsd_peer_credentials("not-a-socket")  # type: ignore[arg-type]

    # 2. Closed socket on simulated FreeBSD
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.close()
    with patch("platform.system", return_value="FreeBSD"):
        with pytest.raises(RuntimeError, match="Socket is closed"):
            freebsd_peer_credentials(s)

    # 3. Simulated FreeBSD platform with ctypes getsockopt mock
    open_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        def test_with_cred(cred_setup, expected_exc=None, match=None):
            def mock_getsockopt(fd, level, optname, optval, optlen):
                cred = _Xucred()
                cred_setup(cred)
                ctypes.memmove(optval, ctypes.byref(cred), ctypes.sizeof(cred))
                c_len = ctypes.cast(optlen, ctypes.POINTER(ctypes.c_uint))
                c_len.contents.value = ctypes.sizeof(cred)
                return 0

            with patch("platform.system", return_value="FreeBSD"):
                with patch("ctypes.CDLL") as mock_cdll:
                    mock_libc = mock_cdll.return_value
                    mock_libc.getsockopt = mock_getsockopt
                    if expected_exc:
                        with pytest.raises(expected_exc, match=match):
                            freebsd_peer_credentials(open_sock)
                    else:
                        return freebsd_peer_credentials(open_sock)

        # Bad xucred version
        def setup_bad_ver(c: _Xucred):
            c.cr_version = 99
            c.cr_ngroups = 1
            c.cr_pid = 1234
        test_with_cred(setup_bad_ver, RuntimeError, "Unsupported FreeBSD xucred version")

        # Zero groups
        def setup_zero_groups(c: _Xucred):
            c.cr_version = XUCRED_VERSION
            c.cr_ngroups = 0
            c.cr_pid = 1234
        test_with_cred(setup_zero_groups, RuntimeError, "contain no effective group")

        # Too many groups (>16)
        def setup_too_many_groups(c: _Xucred):
            c.cr_version = XUCRED_VERSION
            c.cr_ngroups = 20
            c.cr_pid = 1234
        test_with_cred(setup_too_many_groups, RuntimeError, "contain no effective group")

        # Invalid PID (<= 1)
        def setup_invalid_pid(c: _Xucred):
            c.cr_version = XUCRED_VERSION
            c.cr_ngroups = 1
            c.cr_pid = 1
        test_with_cred(setup_invalid_pid, RuntimeError, "invalid process id")

        # Valid credentials
        def setup_valid(c: _Xucred):
            c.cr_version = XUCRED_VERSION
            c.cr_ngroups = 1
            c.cr_pid = 4567
            c.cr_uid = 1001
            c.cr_gid = 1001
        res = test_with_cred(setup_valid)
        assert res.pid == 4567
        assert res.uid == 1001
        assert res.gid == 1001
    finally:
        open_sock.close()


def test_runtime_daemon_gc_and_socket_hardening(tmp_path: Path) -> None:
    from freebsd_laboratory.runtime_daemon import _prepare_socket_path

    manager = make_manager(tmp_path)

    # 1. gc requester_uid validation
    with pytest.raises(PermissionError, match="Invalid requester UID"):
        manager.gc(requester_uid=True)  # type: ignore[arg-type]

    with pytest.raises(PermissionError, match="Invalid requester UID"):
        manager.gc(requester_uid=-1)

    with pytest.raises(ValueError, match="stale_only must be boolean"):
        manager.gc(stale_only="false")  # type: ignore[arg-type]

    # 2. _prepare_socket_path with symlink unlinks symlink
    sock_path = tmp_path / "test_prepare.sock"
    target_file = tmp_path / "target_do_not_delete.txt"
    target_file.write_text("secure", encoding="utf-8")
    sock_path.symlink_to(target_file)

    _prepare_socket_path(sock_path)
    assert not sock_path.exists()
    assert not sock_path.is_symlink()
    assert target_file.exists()

    # 3. _prepare_socket_path with non-socket file raises
    regular_file = tmp_path / "not_a_sock.sock"
    regular_file.write_text("regular file", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Refusing to replace non-socket path"):
        _prepare_socket_path(regular_file)

    # 4. Public-key material must be a single validated Ed25519 line.
    public_key = valid_ed25519_public_key()
    with pytest.raises(ValueError, match="one line"):
        RuntimeManager.validate_ssh_public_key(public_key + "\ncommand=evil")


def test_remote_provisioner_hardening(tmp_path: Path) -> None:
    from freebsd_laboratory.remote_provisioner import (
        RemoteRuntimeProvisioner,
        runtime_name,
    )

    # 1. runtime_name rejects non-string
    with pytest.raises(ValueError, match="kernel_id must be a string"):
        runtime_name(12345)  # type: ignore[arg-type]

    # 2. _remove_runtime_path handles regular files, symlinks, and dirs
    reg_file = tmp_path / "file.txt"
    reg_file.touch()
    RemoteRuntimeProvisioner._remove_runtime_path(reg_file)
    assert not reg_file.exists()

    # 3. load_provisioner_info handles malformed fields safely
    class TestProvisioner(RemoteRuntimeProvisioner):
        pass

    prov = TestProvisioner()
    import asyncio

    asyncio.run(prov.load_provisioner_info({
        "kernel_id": "test-kernel",
        "connection_info": {},
        "pid": 1234,
        "pgid": 1234,
        "ip": "127.0.0.1",
        "ports_cached": False,
        "original_connection_ports": "not-a-list",
        "tunnel_ports": [True, "bad", 30001],
        "guest_ip": "172.31.254.10",
    }))
    assert prov._original_connection_ports == ()
    assert prov._tunnel_ports == ()
    assert prov.guest_ip == "172.31.254.10"


def test_remote_connection_bind_and_symlink_hardening(tmp_path: Path) -> None:
    from freebsd_laboratory.remote_connection import (
        restore_connection_file,
        rewrite_connection_file,
    )

    conn_file = tmp_path / "conn.json"
    conn_file.write_text(json.dumps({
        "ip": "127.0.0.1",
        "transport": "tcp",
        "shell_port": 1001,
        "iopub_port": 1002,
        "stdin_port": 1003,
        "control_port": 1004,
        "hb_port": 1005,
    }), encoding="utf-8")

    class FakeParent:
        connection_file = str(conn_file)
        ip = "127.0.0.1"

    parent = FakeParent()

    # Invalid bind_ip
    with pytest.raises(ValueError, match="Invalid bind_ip"):
        rewrite_connection_file(parent, bind_ip="not.an.ip.address")

    # Symlinked connection file rejected
    sym_conn = tmp_path / "sym_conn.json"
    sym_conn.symlink_to(conn_file)
    parent.connection_file = str(sym_conn)
    # resolve() resolves it, but if connection_file is missing:
    parent.connection_file = str(tmp_path / "nonexistent.json")
    with pytest.raises(RuntimeError, match="Connection file is unavailable"):
        rewrite_connection_file(parent)

    # restore_connection_file handles corrupted json gracefully
    corrupted = tmp_path / "corrupted.json"
    corrupted.write_text("not json at all", encoding="utf-8")
    parent.connection_file = str(corrupted)
    restore_connection_file(parent, "127.0.0.1", (1001, 1002, 1003, 1004, 1005))
    assert corrupted.read_text(encoding="utf-8") == "not json at all"


def test_port_leases_allocation_type_guards(tmp_path: Path) -> None:
    from freebsd_laboratory.port_leases import LocalPortLeasePool

    pool = LocalPortLeasePool(1024, 2000, tmp_path)
    (tmp_path / ".lock").touch()

    # allocate type validations
    with pytest.raises(ValueError, match="owner is required"):
        pool.allocate(123, os.getpid(), 1)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="PID must match"):
        pool.allocate("owner", True, 1)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Invalid tunnel port lease count"):
        pool.allocate("owner", os.getpid(), True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Invalid tunnel port lease count"):
        pool.allocate("owner", os.getpid(), 0)

    # release with non-sequence or out-of-range ports safely returns
    pool.release("not-a-sequence", "owner", os.getpid(), os.getuid(), "a" * 64)  # type: ignore[arg-type]
    pool.release([99999], "owner", os.getpid(), os.getuid(), "a" * 64)


def test_ssh_transport_symlink_and_port_guards(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.touch()

    sym_key = tmp_path / "sym_key"
    sym_key.symlink_to(key_file)

    sym_transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key=str(sym_key),
        known_hosts_file=tmp_path / "known_hosts",
        ssh_command="/bin/sh",
        scp_command="/bin/sh",
    )
    with pytest.raises(RuntimeError, match="Required SSH private key is unavailable"):
        sym_transport.assert_available()

    # Symlinked known_hosts
    kh_file = tmp_path / "kh"
    kh_file.touch()
    sym_kh = tmp_path / "sym_kh"
    sym_kh.symlink_to(kh_file)

    sym_kh_transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key=str(key_file),
        known_hosts_file=sym_kh,
        ssh_command="/bin/sh",
        scp_command="/bin/sh",
    )
    with pytest.raises(RuntimeError, match="Known hosts file must not be a symbolic link"):
        sym_kh_transport.assert_available()

    # command() with string forward_ports
    valid_transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key=str(key_file),
        known_hosts_file=tmp_path / "known_hosts",
    )
    with pytest.raises(ValueError, match="Invalid SSH forwarding port"):
        valid_transport.command("true", forward_ports="5000")  # type: ignore[arg-type]


def test_signing_and_verify_deep_hardening(tmp_path: Path) -> None:
    from freebsd_laboratory.signing import (
        sign_manifest,
        verify_manifest_signature,
    )
    from freebsd_laboratory.verify import verify_bundle

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    priv = Ed25519PrivateKey.generate()
    key_path = tmp_path / "priv.pem"
    key_path.write_bytes(priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"schema":"softcloud.lab-evidence-manifest/v1"}', encoding="utf-8")

    # sign_manifest with invalid key_id
    with pytest.raises(ValueError, match="key_id must be a non-empty string"):
        sign_manifest(manifest_path, key_path, "")

    with pytest.raises(ValueError, match="key_id must be a non-empty string"):
        sign_manifest(manifest_path, key_path, 123)  # type: ignore[arg-type]

    # verify_manifest_signature with corrupted signature JSON
    sig_path = tmp_path / "manifest.sig.json"
    sig_path.write_text("corrupted json {", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid signature JSON"):
        verify_manifest_signature(manifest_path, sig_path)

    # verify_manifest_signature with invalid base64 signature
    sig_path.write_text(json.dumps({
        "schema": "softcloud.lab-signature/v1",
        "algorithm": "ed25519",
        "key_id": "key1",
        "manifest": "manifest.json",
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "public_key_pem": priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii"),
        "public_key_sha256": "fake",
        "signature_base64": "invalid-base64!@#$",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="Embedded public-key fingerprint does not match"):
        verify_manifest_signature(manifest_path, sig_path)

    # verify_bundle path traversal rejection
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(json.dumps({
        "schema": "softcloud.lab-evidence-manifest/v1",
        "artifacts": {
            "../outside.txt": {"sha256": "a" * 64, "size": 10},
        },
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid artifact path in manifest"):
        verify_bundle(bundle)

    (bundle / "manifest.json").write_text(json.dumps({
        "schema": "softcloud.lab-evidence-manifest/v1",
        "artifacts": {
            ".": {"sha256": "a" * 64, "size": 10},
        },
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid artifact path in manifest"):
        verify_bundle(bundle)


def test_service_initialization_and_spec_symlink_hardening(tmp_path: Path) -> None:
    from freebsd_laboratory.service import LabService

    lab_file = tmp_path / "lab.yaml"
    lab_file.write_text("""\
schema: softcloud.lab/v1
id: test-lab
title: Test
notebook: Test.ipynb
""", encoding="utf-8")

    # 1. Boolean max_events and max_event_payload_bytes rejected
    with pytest.raises(ValueError, match="max_events must be positive"):
        LabService(tmp_path, "lab.yaml", ".evidence", max_events=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="max_event_payload_bytes must be at least 1024"):
        LabService(tmp_path, "lab.yaml", ".evidence", max_event_payload_bytes=True)  # type: ignore[arg-type]

    # 2. Symlinked lab.yaml rejected
    sym_lab = tmp_path / "sym_lab.yaml"
    sym_lab.symlink_to(lab_file)
    with pytest.raises(ValueError, match="must not be a symbolic link"):
        LabService(tmp_path, "sym_lab.yaml", ".evidence")

    # 3. Symlinked private key in signing config
    key_file = tmp_path / "priv.pem"
    key_file.touch()
    relative_key_link = tmp_path / "relative-key-link.pem"
    relative_key_link.symlink_to(key_file)

    signing_lab = tmp_path / "signing_lab.yaml"
    signing_lab.write_text("""\
schema: softcloud.lab/v1
id: test-lab
title: Test
notebook: Test.ipynb
evidence:
  signing:
    enabled: true
    algorithm: ed25519
    private_key: relative-key-link.pem
""", encoding="utf-8")

    service = LabService(tmp_path, "signing_lab.yaml", ".evidence")
    with pytest.raises(ValueError, match="evidence.signing.private_key is unavailable"):
        service.state()

    # 4. record_client_event and record_machine_event reject non-string kind
    with pytest.raises(ValueError, match="Client event kind is not allowed"):
        service.record_client_event(123, {})  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Machine event kind is not allowed"):
        service.record_machine_event(None, {})  # type: ignore[arg-type]


def test_service_stage_completion_robust_to_untrusted_markdown_cells_payload(tmp_path: Path) -> None:
    from freebsd_laboratory.service import LabService, redact_payload

    lab_file = tmp_path / "lab.yaml"
    lab_file.write_text("""\
schema: softcloud.lab/v1
id: test-lab
title: Test
notebook: Test.ipynb
""", encoding="utf-8")

    service = LabService(tmp_path, "lab.yaml", ".evidence")

    # Malformed markdown_cells types must not crash service.state()
    service.record_client_event("notebook-context", {"markdown_cells": "not-a-number"})
    service.record_client_event("notebook-context", {"markdown_cells": True})  # type: ignore[dict-item]
    service.record_client_event("notebook-context", {"markdown_cells": ["bad"]})  # type: ignore[dict-item]
    service.record_client_event("notebook-context", {"markdown_cells": {"bad": 1}})  # type: ignore[dict-item]
    service.record_client_event("notebook-context", {"markdown_cells": -5})
    service.record_client_event("notebook-context", {"markdown_cells": 0})

    st = service.state()
    explained_stage = next(s for s in st["stages"] if s["id"] == "explained")
    assert explained_stage["completed"] is False

    # Valid positive integer or digit string completes "explained"
    service.record_client_event("notebook-context", {"markdown_cells": 3})
    st = service.state()
    explained_stage = next(s for s in st["stages"] if s["id"] == "explained")
    assert explained_stage["completed"] is True

    # Test redact_payload on sets and frozensets
    redacted_set = redact_payload({"password_set", "normal_data"})
    assert isinstance(redacted_set, list)


def test_service_export_enforces_0600_permissions(tmp_path: Path) -> None:
    from freebsd_laboratory.service import LabService

    lab_file = tmp_path / "lab.yaml"
    lab_file.write_text("""\
schema: softcloud.lab/v1
id: test-lab
title: Test
notebook: Test.ipynb
""", encoding="utf-8")

    service = LabService(tmp_path, "lab.yaml", ".evidence")
    service.record_client_event("cell-executed", {"cell": {"cell_type": "code", "source": "x = 1"}})
    exported = service.export()

    session_dir = Path(exported["path"])
    for filename in exported["files"]:
        filepath = session_dir / filename
        assert filepath.is_file()
        assert filepath.stat().st_mode & 0o777 == 0o600


def test_runtime_daemon_create_bhyve_rejects_malformed_ssh_key(tmp_path: Path) -> None:
    from freebsd_laboratory.peer_credentials import PeerCredentials

    manager = RuntimeManager(
        RuntimeConfig(
            registry_dir=str(tmp_path / "registry"),
            lease_dir=str(tmp_path / "leases"),
            network_cidr="172.31.254.0/24",
            host_address="172.31.254.1",
            address_start="172.31.254.10",
            address_end="172.31.254.20",
            vm_command="/bin/sh",
        )
    )
    peer = PeerCredentials(pid=os.getpid(), uid=os.getuid(), gid=os.getgid())

    with pytest.raises(ValueError, match="Ed25519"):
        manager.create_bhyve(
            "freebsd-lab-testvm",
            os.getpid(),
            peer,
            "ssh-rsa AAAA",
        )


def test_ssh_transport_stage_symlink_and_remote_dir_guards(tmp_path: Path) -> None:
    key_file = tmp_path / "key"
    key_file.touch()
    kh_file = tmp_path / "known_hosts"
    kh_file.touch()

    transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key=str(key_file),
        known_hosts_file=kh_file,
        ssh_command="/bin/sh",
        scp_command="/bin/sh",
    )

    # Missing file
    with pytest.raises(RuntimeError, match="File to stage does not exist"):
        transport.stage(tmp_path / "missing.txt", "/tmp/dir")

    # Symlinked file
    real_file = tmp_path / "real.txt"
    real_file.write_text("hello", encoding="utf-8")
    sym_file = tmp_path / "sym.txt"
    sym_file.symlink_to(real_file)

    with pytest.raises(RuntimeError, match="File to stage must be a regular file"):
        transport.stage(sym_file, "/tmp/dir")

    # Non-absolute remote_dir
    with pytest.raises(ValueError, match="remote_dir must be an absolute path"):
        transport.stage(real_file, "relative/path")


def test_remote_provisioner_load_info_guards_boolean_name_and_invalid_ports() -> None:
    from freebsd_laboratory.remote_provisioner import RemoteRuntimeProvisioner
    import asyncio

    class TestProv(RemoteRuntimeProvisioner):
        pass

    prov = TestProv()
    asyncio.run(prov.load_provisioner_info({
        "kernel_id": "test-kernel-123",
        "connection_info": {},
        "pid": 100,
        "pgid": 100,
        "ip": "127.0.0.1",
        "ports_cached": False,
        "runtime_name": False,  # type: ignore[dict-item]
        "guest_ip": False,  # type: ignore[dict-item]
        "known_hosts_file": False,  # type: ignore[dict-item]
        "original_connection_ip": False,  # type: ignore[dict-item]
        "original_connection_ports": [0, 70000, "not-int", True, 2000],
        "tunnel_ports": [65536, -1, 3000],
    }))

    assert prov._runtime_name is None
    assert prov.guest_ip is None
    assert prov.known_hosts_file is None
    assert prov._original_connection_ip is None
    assert prov._original_connection_ports == ()
    assert prov._tunnel_ports == ()


def test_remote_connection_symlink_rejection(tmp_path: Path) -> None:
    from freebsd_laboratory.remote_connection import (
        restore_connection_file,
        rewrite_connection_file,
    )

    conn_file = tmp_path / "conn.json"
    conn_file.write_text(json.dumps({
        "ip": "127.0.0.1",
        "transport": "tcp",
        "shell_port": 1001,
        "iopub_port": 1002,
        "stdin_port": 1003,
        "control_port": 1004,
        "hb_port": 1005,
    }), encoding="utf-8")

    sym_conn = tmp_path / "sym_conn.json"
    sym_conn.symlink_to(conn_file)

    class FakeParent:
        connection_file = str(sym_conn)
        ip = "127.0.0.1"

    parent = FakeParent()
    with pytest.raises(RuntimeError, match="Connection file must not be a symbolic link"):
        rewrite_connection_file(parent)

    # restore_connection_file safely returns on symlink
    restore_connection_file(parent, "127.0.0.1", (1001, 1002, 1003, 1004, 1005))


def test_ipv4_pool_lock_symlink_resilience(tmp_path: Path) -> None:
    lease_dir = tmp_path / "leases"
    lease_dir.mkdir(parents=True)

    pool = IPv4LeasePool(
        network="172.31.254.0/24",
        start="172.31.254.10",
        end="172.31.254.20",
        lease_dir=lease_dir,
    )

    # Target file that should NOT be overwritten via lock symlink
    sensitive_target = tmp_path / "sensitive.txt"
    sensitive_target.write_text("do not overwrite", encoding="utf-8")

    lock_file = lease_dir / ".lock"
    lock_file.symlink_to(sensitive_target)

    # allocate unlinks symlink and succeeds
    addr = pool.allocate("owner1")
    assert addr == "172.31.254.10"
    assert sensitive_target.read_text(encoding="utf-8") == "do not overwrite"
    assert not lock_file.is_symlink()

    # Symlink on release
    lock_file.unlink()
    lock_file.symlink_to(sensitive_target)
    assert pool.release(addr, "owner1") is True
    assert sensitive_target.read_text(encoding="utf-8") == "do not overwrite"

    # Symlink on clear_orphans
    lock_file.unlink()
    lock_file.symlink_to(sensitive_target)
    pool.clear_orphans(set())
    assert sensitive_target.read_text(encoding="utf-8") == "do not overwrite"


def test_verify_bundle_rejects_symlinked_or_nonexistent_bundle_dir(tmp_path: Path) -> None:
    from freebsd_laboratory.verify import verify_bundle

    # 1. Nonexistent bundle directory
    with pytest.raises(ValueError, match="Evidence bundle directory does not exist"):
        verify_bundle(tmp_path / "nonexistent_bundle")

    # 2. Symlinked bundle directory
    real_bundle = tmp_path / "real_bundle"
    real_bundle.mkdir()
    sym_bundle = tmp_path / "sym_bundle"
    sym_bundle.symlink_to(real_bundle)

    with pytest.raises(ValueError, match="Evidence bundle must not be a symbolic link"):
        verify_bundle(sym_bundle)
