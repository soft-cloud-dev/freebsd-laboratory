from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path

from ..runtime_client import DEFAULT_RUNTIME_SOCKET, RuntimeClient, RuntimeControlError
from ..ssh_transport import SSHTransport, create_runtime_ssh_key
from .bounded_exec import bounded_exec
from .types import BoundedOutput, Observation, RuntimeHandle


def generate_agent_runtime_name() -> str:
    """Generate a valid runtime name matching ^freebsd-lab-[a-z0-9]{1,16}$."""
    token = uuid.uuid4().hex[:12]
    return f"freebsd-lab-a{token}"


class AgentRuntime:
    """Manages the creation, command execution, and destruction of isolated runtimes for agents."""

    def __init__(
        self,
        mode: str = "bhyve",
        socket_path: str = DEFAULT_RUNTIME_SOCKET,
        ssh_user: str = "freebsd",
        ssh_command: str = "/usr/bin/ssh",
        ssh_connect_timeout: int = 5,
        startup_timeout: int = 90,
        command_timeout: int = 30,
        head_limit: int = 4096,
        tail_limit: int = 4096,
        runtime_dir: Path | str | None = None,
    ) -> None:
        if mode not in ("bhyve", "jail"):
            raise ValueError(f"Unsupported runtime mode: {mode!r} (must be 'bhyve' or 'jail')")
        if not isinstance(ssh_user, str) or not ssh_user:
            raise ValueError("ssh_user must be a non-empty string")
        if isinstance(startup_timeout, bool) or not isinstance(startup_timeout, int) or startup_timeout <= 0:
            raise ValueError("startup_timeout must be a positive integer")
        if isinstance(command_timeout, bool) or not isinstance(command_timeout, int) or command_timeout <= 0:
            raise ValueError("command_timeout must be a positive integer")

        self.mode = mode
        self.socket_path = socket_path
        self.ssh_user = ssh_user
        self.ssh_command = ssh_command
        self.ssh_connect_timeout = ssh_connect_timeout
        self.startup_timeout = startup_timeout
        self.command_timeout = command_timeout
        self.head_limit = head_limit
        self.tail_limit = tail_limit

        if runtime_dir is None:
            self.runtime_dir = Path("~/.cache/freebsd-laboratory/agent").expanduser()
        else:
            self.runtime_dir = Path(runtime_dir).expanduser()

    def _client(self) -> RuntimeClient:
        return RuntimeClient(
            self.socket_path,
            timeout=max(30.0, float(self.startup_timeout)),
        )

    def create(self) -> RuntimeHandle:
        runtime_name = generate_agent_runtime_name()
        if self.runtime_dir.is_symlink():
            raise RuntimeError(f"Runtime cache directory must not be a symlink: {self.runtime_dir}")
        self.runtime_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.runtime_dir, 0o700, follow_symlinks=False)

        instance_dir = self.runtime_dir / runtime_name
        if instance_dir.is_symlink() or instance_dir.is_file():
            instance_dir.unlink(missing_ok=True)
        elif instance_dir.is_dir():
            shutil.rmtree(instance_dir, ignore_errors=True)

        instance_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(instance_dir, 0o700, follow_symlinks=False)

        known_hosts_file = instance_dir / "known_hosts"
        known_hosts_file.touch(mode=0o600)
        os.chmod(known_hosts_file, 0o600, follow_symlinks=False)

        private_key_path, public_key_material = create_runtime_ssh_key(instance_dir)

        client = self._client()
        owner_pid = os.getpid()

        if self.mode == "bhyve":
            result = client.create_bhyve(
                name=runtime_name,
                owner_pid=owner_pid,
                ssh_public_key=public_key_material,
                profile="freebsd-python",
            )
        else:
            result = client.create_jail(
                name=runtime_name,
                owner_pid=owner_pid,
                ssh_public_key=public_key_material,
            )

        guest_ip = result.get("guest_ip")
        if not isinstance(guest_ip, str) or not guest_ip:
            raise RuntimeError(f"Runtime daemon did not return an IP address for {runtime_name}")

        transport = SSHTransport(
            host=guest_ip,
            user=self.ssh_user,
            private_key=str(private_key_path),
            known_hosts_file=known_hosts_file,
            ssh_command=self.ssh_command,
            connect_timeout=self.ssh_connect_timeout,
        )
        transport.assert_available()
        transport.wait_until_ready(self.startup_timeout)

        return RuntimeHandle(
            runtime_name=runtime_name,
            guest_ip=guest_ip,
            runtime_type=self.mode,
            private_key=private_key_path,
            known_hosts_file=known_hosts_file,
        )

    def execute(self, handle: RuntimeHandle, command: str) -> Observation:
        transport = SSHTransport(
            host=handle.guest_ip,
            user=self.ssh_user,
            private_key=str(handle.private_key),
            known_hosts_file=handle.known_hosts_file,
            ssh_command=self.ssh_command,
            connect_timeout=self.ssh_connect_timeout,
        )
        ssh_cmd = transport.command(command)

        start = time.monotonic()
        exit_status, stdout_out, stderr_out = bounded_exec(
            ssh_cmd,
            timeout=self.command_timeout,
            head_limit=self.head_limit,
            tail_limit=self.tail_limit,
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        return Observation(
            step=0,
            command=command,
            exit_status=exit_status,
            stdout=stdout_out,
            stderr=stderr_out,
            duration_ms=duration_ms,
        )

    def destroy(self, handle: RuntimeHandle) -> None:
        try:
            self._client().destroy(handle.runtime_name)
        except (RuntimeControlError, OSError):
            pass
        finally:
            instance_dir = self.runtime_dir / handle.runtime_name
            if instance_dir.is_symlink() or instance_dir.is_file():
                instance_dir.unlink(missing_ok=True)
            elif instance_dir.is_dir():
                shutil.rmtree(instance_dir, ignore_errors=True)
