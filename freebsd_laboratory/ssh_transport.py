from __future__ import annotations

import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


def executable_exists(command: str) -> bool:
    if Path(command).is_absolute():
        return Path(command).is_file() and Path(command).stat().st_mode & 0o111 != 0
    return shutil.which(command) is not None


@dataclass
class SSHTransport:
    host: str
    user: str
    private_key: str
    known_hosts_file: Path
    ssh_command: str = "/usr/bin/ssh"
    scp_command: str = "/usr/bin/scp"
    config_file: str = "/dev/null"
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
        if not Path(self.config_file).is_file():
            raise RuntimeError(
                f"SSH client configuration path is unavailable: {self.config_file}"
            )

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def options(self) -> list[str]:
        return [
            "-F",
            self.config_file,
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
            "GlobalKnownHostsFile=/dev/null",
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
        normalized = list(command)
        try:
            result = subprocess.run(
                normalized,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"Command timed out: {shlex.join(normalized)}") from error
        except OSError as error:
            raise RuntimeError(
                f"Unable to execute {shlex.join(normalized)}: {error}"
            ) from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise RuntimeError(f"{shlex.join(normalized)}: {detail}")
        return result

    def wait_until_ready(self, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        last_detail = "SSH did not become ready"
        probe_timeout = max(5, self.connect_timeout + 2)
        while time.monotonic() < deadline:
            result = self._run(
                self.command("true"),
                check=False,
                timeout=probe_timeout,
            )
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
