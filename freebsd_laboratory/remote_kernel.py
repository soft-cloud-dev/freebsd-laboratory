from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


CONNECTION_PORT_FIELDS = (
    "shell_port",
    "iopub_port",
    "stdin_port",
    "control_port",
    "hb_port",
)


def executable_exists(command: str) -> bool:
    if os.path.isabs(command):
        return Path(command).is_file() and os.access(command, os.X_OK)
    return shutil.which(command) is not None


def remote_kernel_command(
    command: Sequence[str],
    host_connection_file: Path,
    remote_connection_file: str,
) -> list[str]:
    candidates = {
        str(host_connection_file),
        str(host_connection_file.resolve()),
        host_connection_file.name,
    }
    replaced = False
    result: list[str] = []
    for argument in command:
        if argument in candidates:
            result.append(remote_connection_file)
            replaced = True
        else:
            result.append(argument)
    if not replaced:
        raise RuntimeError("Kernel command does not contain the Jupyter connection file")
    return result


def connection_ports(document: dict[str, Any]) -> tuple[int, ...]:
    ports: list[int] = []
    for field in CONNECTION_PORT_FIELDS:
        value = document.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValueError(f"Invalid Jupyter connection port: {field}")
        ports.append(value)
    if len(set(ports)) != len(ports):
        raise ValueError("Jupyter connection ports must be unique")
    return tuple(ports)


def rewrite_connection_file(
    parent: Any,
    bind_ip: str = "127.0.0.1",
) -> tuple[Path, str, tuple[int, ...]]:
    """Bind the host and remote kernel documents to loopback for SSH forwarding.

    Jupyter keeps using its original random TCP ports, but those ports are bound
    locally by the SSH client and forwarded to the same loopback ports inside the
    jail/VM. The guest therefore exposes only SSH on the laboratory bridge.
    """

    connection_file = getattr(parent, "connection_file", None)
    if not connection_file:
        raise RuntimeError("Kernel manager connection file is unavailable")
    host_path = Path(connection_file).resolve()
    document = json.loads(host_path.read_text(encoding="utf-8"))
    if document.get("transport", "tcp") != "tcp":
        raise ValueError("SSH kernel transport requires Jupyter TCP connections")

    ports = connection_ports(document)
    original_ip = str(document.get("ip", getattr(parent, "ip", "")))
    document["ip"] = bind_ip
    setattr(parent, "ip", bind_ip)

    temporary = host_path.with_name(f".{host_path.name}.remote.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(host_path)
    return host_path, original_ip, ports


def restore_connection_file(parent: Any, original_ip: str | None) -> None:
    if original_ip is None:
        return
    setattr(parent, "ip", original_ip)
    connection_file = getattr(parent, "connection_file", None)
    if not connection_file:
        return
    path = Path(connection_file)
    if not path.is_file():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        document["ip"] = original_ip
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
    except (OSError, ValueError, TypeError):
        return


@dataclass
class SSHTransport:
    host: str
    user: str
    private_key: str
    known_hosts_file: Path
    ssh_command: str = "/usr/bin/ssh"
    scp_command: str = "/usr/bin/scp"
    connect_timeout: int = 5
    connection_attempts: int = 3
    server_alive_interval: int = 15
    server_alive_count_max: int = 4
    tcp_keep_alive: bool = True
    bind_address: str = "127.0.0.1"

    def assert_available(self) -> None:
        for command in (self.ssh_command, self.scp_command):
            if not executable_exists(command):
                raise RuntimeError(f"Required executable is unavailable: {command}")
        if not Path(self.private_key).is_file():
            raise RuntimeError(f"Required SSH private key is unavailable: {self.private_key}")

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def options(self) -> list[str]:
        return [
            "-i",
            self.private_key,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            f"ConnectionAttempts={self.connection_attempts}",
            "-o",
            f"ServerAliveInterval={self.server_alive_interval}",
            "-o",
            f"ServerAliveCountMax={self.server_alive_count_max}",
            "-o",
            f"TCPKeepAlive={'yes' if self.tcp_keep_alive else 'no'}",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.known_hosts_file}",
        ]

    def command(
        self,
        remote_command: str,
        *,
        forward_ports: Sequence[int] = (),
    ) -> list[str]:
        command = [self.ssh_command, *self.options(), "-T"]
        for port in sorted(set(forward_ports)):
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("Invalid SSH forwarding port")
            command.extend(
                [
                    "-L",
                    f"{self.bind_address}:{port}:127.0.0.1:{port}",
                ]
            )
        command.extend([self.target, remote_command])
        return command

    @staticmethod
    def _run(
        command: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                list(command),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Command timed out: {shlex.join(command)}") from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise RuntimeError(f"{shlex.join(command)}: {detail}")
        return result

    def wait_until_ready(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        last_detail = "SSH did not become ready"
        probe_timeout = max(5, self.connect_timeout + 2)
        while time.monotonic() < deadline:
            result = self._run(self.command("true"), check=False, timeout=probe_timeout)
            if result.returncode == 0:
                return
            last_detail = result.stderr.strip() or result.stdout.strip() or last_detail
            time.sleep(1)
        raise RuntimeError(f"Timed out waiting for {self.target} SSH: {last_detail}")

    def stage(self, host_path: Path, remote_dir: str) -> str:
        remote_path = f"{remote_dir.rstrip('/')}/{host_path.name}"
        self._run(
            self.command(f"install -d -m 700 {shlex.quote(remote_dir)}"),
            timeout=15,
        )
        self._run(
            [
                self.scp_command,
                *self.options(),
                str(host_path),
                f"{self.target}:{remote_path}",
            ],
            timeout=15,
        )
        return remote_path
