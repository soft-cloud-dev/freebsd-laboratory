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


def rewrite_connection_file(parent: Any, guest_ip: str) -> tuple[Path, str]:
    connection_file = getattr(parent, "connection_file", None)
    if not connection_file:
        raise RuntimeError("Kernel manager connection file is unavailable")
    host_path = Path(connection_file).resolve()
    document = json.loads(host_path.read_text(encoding="utf-8"))
    original_ip = str(document.get("ip", getattr(parent, "ip", "")))
    document["ip"] = guest_ip
    setattr(parent, "ip", guest_ip)

    temporary = host_path.with_name(f".{host_path.name}.remote.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(host_path)
    return host_path, original_ip


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
    connect_timeout: int = 3

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
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.known_hosts_file}",
        ]

    def command(self, remote_command: str) -> list[str]:
        return [self.ssh_command, *self.options(), self.target, remote_command]

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
        while time.monotonic() < deadline:
            result = self._run(self.command("true"), check=False, timeout=5)
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
