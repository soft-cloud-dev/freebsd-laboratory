from __future__ import annotations

import json
from pathlib import Path

import pytest

from freebsd_laboratory.remote_kernel import (
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


def write_connection_file(path: Path) -> None:
    document = {
        "transport": "tcp",
        "ip": "0.0.0.0",
        "key": "test",
        "signature_scheme": "hmac-sha256",
        **PORTS,
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_connection_ports_requires_complete_unique_tcp_ports() -> None:
    assert connection_ports(PORTS) == tuple(PORTS.values())

    invalid = dict(PORTS)
    invalid["hb_port"] = invalid["shell_port"]
    with pytest.raises(ValueError):
        connection_ports(invalid)


def test_connection_file_is_loopback_bound_for_ssh_tunnel(tmp_path: Path) -> None:
    path = tmp_path / "kernel.json"
    write_connection_file(path)
    parent = Parent(path)

    host_path, original_ip, ports = rewrite_connection_file(parent)

    document = json.loads(host_path.read_text(encoding="utf-8"))
    assert document["ip"] == "127.0.0.1"
    assert parent.ip == "127.0.0.1"
    assert original_ip == "0.0.0.0"
    assert ports == tuple(PORTS.values())

    restore_connection_file(parent, original_ip)
    restored = json.loads(path.read_text(encoding="utf-8"))
    assert restored["ip"] == "0.0.0.0"
    assert parent.ip == "0.0.0.0"


def test_ssh_transport_forwards_all_kernel_ports_with_keepalives(tmp_path: Path) -> None:
    transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key="/tmp/lab-key",
        known_hosts_file=tmp_path / "known_hosts",
    )

    command = transport.command("exec python3 -m ipykernel_launcher", forward_ports=PORTS.values())
    rendered = " ".join(command)

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


def test_ssh_transport_rejects_invalid_forward_port(tmp_path: Path) -> None:
    transport = SSHTransport(
        host="172.31.254.10",
        user="freebsd",
        private_key="/tmp/lab-key",
        known_hosts_file=tmp_path / "known_hosts",
    )
    with pytest.raises(ValueError):
        transport.command("true", forward_ports=(0,))
