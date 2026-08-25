from __future__ import annotations

import json
import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest

from freebsd_laboratory.remote_kernel import (
    LocalPortLeasePool,
    SSHTransport,
    connection_ports,
    restore_connection_file,
    rewrite_connection_file,
)


PORTS = {
    "shell_port": 51001,
    "iopub_port": 51002,
    "stdin_port": 51003,
    "control_port": 51004,
    "hb_port": 51005,
}


class Parent:
    def __init__(self, connection_file: Path) -> None:
        self.connection_file = str(connection_file)
        self.ip = "0.0.0.0"
        for field_name, port in PORTS.items():
            setattr(self, field_name, port)


def write_connection_file(path: Path) -> None:
    document = {
        "transport": "tcp",
        "ip": "0.0.0.0",
        "key": "test",
        "signature_scheme": "hmac-sha256",
        **PORTS,
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def find_free_range(count: int) -> tuple[int, int]:
    for base in range(20000, 59000, max(count, 32)):
        sockets: list[socket.socket] = []
        available = True
        try:
            for port in range(base, base + count):
                reserved_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    reserved_socket.bind(("127.0.0.1", port))
                except OSError:
                    reserved_socket.close()
                    available = False
                    break
                sockets.append(reserved_socket)
        finally:
            for reserved_socket in sockets:
                reserved_socket.close()
        if available:
            return base, base + count - 1
    raise RuntimeError("Unable to find a free TCP range for test")


def make_pool(start: int, end: int, directory: Path) -> LocalPortLeasePool:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".lock").touch(mode=0o600, exist_ok=True)
    return LocalPortLeasePool(start, end, directory)


def test_connection_ports_requires_complete_unique_tcp_ports() -> None:
    assert connection_ports(PORTS) == tuple(PORTS.values())

    invalid = dict(PORTS)
    invalid["hb_port"] = invalid["shell_port"]
    with pytest.raises(ValueError):
        connection_ports(invalid)


def test_connection_file_is_rewritten_to_leased_loopback_ports(tmp_path: Path) -> None:
    path = tmp_path / "kernel.json"
    write_connection_file(path)
    parent = Parent(path)
    leased_ports = (52001, 52002, 52003, 52004, 52005)

    host_path, original_ip, original_ports, tunnel_ports = rewrite_connection_file(
        parent,
        ports=leased_ports,
    )

    document = json.loads(host_path.read_text(encoding="utf-8"))
    assert document["ip"] == "127.0.0.1"
    assert parent.ip == "127.0.0.1"
    assert original_ip == "0.0.0.0"
    assert original_ports == tuple(PORTS.values())
    assert tunnel_ports == leased_ports
    for field_name, port in zip(PORTS, leased_ports, strict=True):
        assert document[field_name] == port
        assert getattr(parent, field_name) == port

    restore_connection_file(parent, original_ip, original_ports)
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored["ip"] == "0.0.0.0"
    assert parent.ip == "0.0.0.0"
    for field_name, port in PORTS.items():
        assert restored[field_name] == port
        assert getattr(parent, field_name) == port


def test_port_pool_prevents_concurrent_session_collisions(tmp_path: Path) -> None:
    session_count = 12
    ports_per_session = 5
    start, end = find_free_range(128)
    pool = make_pool(start, end, tmp_path / "leases")
    barrier = Barrier(session_count)

    def allocate(index: int):
        barrier.wait()
        return pool.allocate(f"session-{index}", os.getpid(), ports_per_session)

    with ThreadPoolExecutor(max_workers=session_count) as executor:
        reservations = list(executor.map(allocate, range(session_count)))

    all_ports = [port for reservation in reservations for port in reservation.ports]
    assert len(all_ports) == session_count * ports_per_session
    assert len(set(all_ports)) == len(all_ports)

    for reservation in reservations:
        reservation.release_reservations()

    extra = pool.allocate("session-extra", os.getpid(), ports_per_session)
    assert set(extra.ports).isdisjoint(all_ports)

    extra.release()
    for reservation in reservations:
        reservation.release()


def test_port_pool_skips_ports_already_owned_by_host_process(tmp_path: Path) -> None:
    start, end = find_free_range(32)
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", start))
    occupied.listen(1)
    try:
        reservation = make_pool(start, end, tmp_path / "leases").allocate(
            "session-a",
            os.getpid(),
            5,
        )
        assert start not in reservation.ports
        reservation.release()
    finally:
        occupied.close()


def test_port_pool_requires_host_provisioned_lock(tmp_path: Path) -> None:
    start, end = find_free_range(16)
    directory = tmp_path / "leases"
    directory.mkdir()
    pool = LocalPortLeasePool(start, end, directory)

    with pytest.raises(RuntimeError, match="lock must be provisioned"):
        pool.allocate("session-a", os.getpid(), 5)


def test_port_pool_keeps_lease_files_private(tmp_path: Path) -> None:
    start, end = find_free_range(16)
    pool = make_pool(start, end, tmp_path / "leases")
    lock_path = pool.directory / ".lock"
    lock_mode_before = lock_path.stat().st_mode & 0o777
    reservation = pool.allocate("session-a", os.getpid(), 5)
    try:
        assert lock_path.stat().st_mode & 0o777 == lock_mode_before
        for port in reservation.ports:
            lease_paths = list(pool.directory.glob(f"{port}.*.lease"))
            assert len(lease_paths) == 1
            assert lease_paths[0].stat().st_mode & 0o777 == 0o600
            assert lease_paths[0].read_bytes() == b""
    finally:
        reservation.release()


def test_ssh_transport_forwards_all_kernel_ports_with_keepalives(tmp_path: Path) -> None:
    transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key="/tmp/lab-key",
        known_hosts_file=tmp_path / "known_hosts",
    )

    command = transport.command("exec python3 -m ipykernel_launcher", forward_ports=PORTS.values())
    rendered = " ".join(command)

    assert "-F" in command
    assert "/dev/null" in command
    assert "GlobalKnownHostsFile=/dev/null" in command
    assert "ConnectTimeout=5" in command
    assert "ConnectionAttempts=3" in command
    assert "ServerAliveInterval=15" in command
    assert "ServerAliveCountMax=4" in command
    assert "TCPKeepAlive=yes" in command
    assert "ExitOnForwardFailure=yes" in command
    assert command.count("-L") == 5
    for port in PORTS.values():
        assert f"127.0.0.1:{port}:127.0.0.1:{port}" in command
    assert "172.31.254.10" in rendered


def test_ssh_transport_retries_timed_out_readiness_probe(tmp_path: Path) -> None:
    transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key="/tmp/lab-key",
        known_hosts_file=tmp_path / "known_hosts",
    )
    ready = subprocess.CompletedProcess(["ssh"], 0, "", "")

    with (
        patch.object(
            SSHTransport,
            "_run",
            side_effect=[RuntimeError("Command timed out: ssh"), ready],
        ) as run,
        patch("freebsd_laboratory.ssh_transport.time.sleep"),
    ):
        transport.wait_until_ready(30)

    assert run.call_count == 2
    first_timeout = run.call_args_list[0].kwargs["timeout"]
    assert 16 <= first_timeout <= 17


def test_ssh_transport_accepts_default_device_config(tmp_path: Path) -> None:
    private_key = tmp_path / "id_ed25519"
    private_key.write_text("test", encoding="utf-8")
    transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key=str(private_key),
        known_hosts_file=tmp_path / "known_hosts",
        ssh_command="/bin/sh",
        scp_command="/bin/sh",
    )

    with patch.object(Path, "exists", side_effect=AssertionError("must not stat /dev/null")):
        transport.assert_available()


def test_ssh_transport_rejects_directory_as_config(tmp_path: Path) -> None:
    private_key = tmp_path / "id_ed25519"
    private_key.write_text("test", encoding="utf-8")
    config_dir = tmp_path / "ssh-config"
    config_dir.mkdir()
    transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key=str(private_key),
        known_hosts_file=tmp_path / "known_hosts",
        ssh_command="/bin/sh",
        scp_command="/bin/sh",
        config_file=str(config_dir),
    )

    with pytest.raises(RuntimeError, match="configuration path is unavailable"):
        transport.assert_available()


def test_ssh_transport_rejects_invalid_forward_port(tmp_path: Path) -> None:
    transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key="/tmp/lab-key",
        known_hosts_file=tmp_path / "known_hosts",
    )
    with pytest.raises(ValueError):
        transport.command("true", forward_ports=(0,))
