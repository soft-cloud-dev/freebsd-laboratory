from __future__ import annotations

import asyncio
import os
import platform
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from jupyter_client.provisioning import LocalProvisioner
from traitlets import Int, Unicode

from .remote_kernel import (
    SSHTransport,
    remote_kernel_command,
    restore_connection_file,
    rewrite_connection_file,
)
from .runtime_client import DEFAULT_RUNTIME_SOCKET, RuntimeClient


def runtime_name(kernel_id: str) -> str:
    compact = re.sub(r"[^a-zA-Z0-9]", "", kernel_id).lower()
    if not compact:
        raise ValueError("kernel_id does not contain a usable identifier")
    return f"freebsd-lab-{compact[:16]}"


def jail_path_for_host_path(jail_root: Path, host_path: Path) -> Path:
    """Compatibility helper retained for evidence/tests from the direct-jexec prototype."""
    if not host_path.is_absolute():
        raise ValueError("Connection file path must be absolute")
    root = jail_root.resolve()
    target = (root / str(host_path).lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Mirrored path escapes jail root")
    return target


class FreeBSDJailProvisioner(LocalProvisioner):
    """Run ipykernel inside an ephemeral, VNET-isolated FreeBSD jail.

    Privileged lifecycle work is delegated to freebsd-lab-runtime-daemon over a
    Unix-domain socket. The Jupyter process reaches the jail only through SSH;
    all Jupyter TCP channels are loopback-bound and forwarded through that SSH
    session, so the guest exposes no ZMQ ports on the laboratory bridge.
    """

    runtime_socket: str = Unicode(DEFAULT_RUNTIME_SOCKET).tag(config=True)
    ssh_user: str = Unicode("freebsd").tag(config=True)
    ssh_private_key: str = Unicode(
        "/usr/local/etc/freebsd-laboratory/id_ed25519"
    ).tag(config=True)
    ssh_command: str = Unicode("/usr/bin/ssh").tag(config=True)
    scp_command: str = Unicode("/usr/bin/scp").tag(config=True)
    remote_connection_dir: str = Unicode("/tmp/freebsd-laboratory").tag(config=True)
    runtime_dir: str = Unicode("~/.cache/freebsd-laboratory/runtime").tag(config=True)
    startup_timeout: int = Int(30, min=5).tag(config=True)
    ssh_connect_timeout: int = Int(5, min=1).tag(config=True)
    ssh_connection_attempts: int = Int(3, min=1).tag(config=True)
    ssh_server_alive_interval: int = Int(15, min=1).tag(config=True)
    ssh_server_alive_count_max: int = Int(4, min=1).tag(config=True)

    jail_name: str | None = None
    guest_ip: str | None = None
    known_hosts_file: Path | None = None
    _runtime_created = False
    _original_connection_ip: str | None = None
    _tunnel_ports: tuple[int, ...] = ()

    @staticmethod
    def _assert_supported_host() -> None:
        if platform.system() != "FreeBSD":
            raise RuntimeError("FreeBSD jail provisioner requires a FreeBSD host")

    def _client(self) -> RuntimeClient:
        return RuntimeClient(self.runtime_socket, timeout=max(30.0, float(self.startup_timeout)))

    def _runtime_path(self) -> Path:
        if self.jail_name is None:
            raise RuntimeError("Jail name is not initialized")
        return Path(self.runtime_dir).expanduser() / self.jail_name

    def _transport(self) -> SSHTransport:
        if self.guest_ip is None or self.known_hosts_file is None:
            raise RuntimeError("Jail SSH transport is not initialized")
        return SSHTransport(
            host=self.guest_ip,
            user=self.ssh_user,
            private_key=str(Path(self.ssh_private_key).expanduser()),
            known_hosts_file=self.known_hosts_file,
            ssh_command=self.ssh_command,
            scp_command=self.scp_command,
            connect_timeout=self.ssh_connect_timeout,
            connection_attempts=self.ssh_connection_attempts,
            server_alive_interval=self.ssh_server_alive_interval,
            server_alive_count_max=self.ssh_server_alive_count_max,
        )

    async def _create_runtime(self) -> None:
        self.jail_name = runtime_name(str(self.kernel_id))
        runtime_path = self._runtime_path()
        runtime_path.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.known_hosts_file = runtime_path / "known_hosts"
        self.known_hosts_file.touch(mode=0o600)

        result = await asyncio.to_thread(
            self._client().create_jail,
            self.jail_name,
            os.getpid(),
        )
        guest_ip = result.get("guest_ip")
        if not isinstance(guest_ip, str) or not guest_ip:
            raise RuntimeError("Runtime daemon did not return the jail address")
        self.guest_ip = guest_ip
        self._runtime_created = True

    async def _destroy_runtime(self) -> None:
        name = self.jail_name
        if self._runtime_created and name:
            try:
                await asyncio.to_thread(self._client().destroy, name)
            finally:
                self._runtime_created = False
        if name:
            shutil.rmtree(Path(self.runtime_dir).expanduser() / name, ignore_errors=True)
        self.jail_name = None
        self.guest_ip = None
        self.known_hosts_file = None
        self._tunnel_ports = ()

    async def pre_launch(self, **kwargs: Any) -> dict[str, Any]:
        self._assert_supported_host()
        prepared = await super().pre_launch(**kwargs)
        try:
            await self._create_runtime()
            transport = self._transport()
            transport.assert_available()
            await asyncio.to_thread(transport.wait_until_ready, self.startup_timeout)

            if self.parent is None:
                raise RuntimeError("Kernel manager is unavailable")
            host_connection, original_ip, tunnel_ports = rewrite_connection_file(self.parent)
            self._original_connection_ip = original_ip
            self._tunnel_ports = tunnel_ports
            remote_connection = await asyncio.to_thread(
                transport.stage,
                host_connection,
                self.remote_connection_dir,
            )
            kernel_command = remote_kernel_command(
                list(prepared["cmd"]),
                host_connection,
                remote_connection,
            )
            prepared["cmd"] = transport.command(
                "exec " + " ".join(shlex.quote(value) for value in kernel_command),
                forward_ports=tunnel_ports,
            )
            prepared.pop("cwd", None)
            return prepared
        except Exception:
            if self.parent is not None:
                restore_connection_file(self.parent, self._original_connection_ip)
            self._original_connection_ip = None
            self._tunnel_ports = ()
            await self._destroy_runtime()
            raise

    async def cleanup(self, restart: bool = False) -> None:
        try:
            await super().cleanup(restart=restart)
        finally:
            if self.parent is not None:
                restore_connection_file(self.parent, self._original_connection_ip)
            self._original_connection_ip = None
            self._tunnel_ports = ()
            await self._destroy_runtime()

    async def get_provisioner_info(self) -> dict[str, Any]:
        info = await super().get_provisioner_info()
        info.update(
            {
                "jail_name": self.jail_name,
                "guest_ip": self.guest_ip,
                "known_hosts_file": str(self.known_hosts_file) if self.known_hosts_file else None,
                "runtime_created": self._runtime_created,
                "original_connection_ip": self._original_connection_ip,
                "tunnel_ports": list(self._tunnel_ports),
            }
        )
        return info

    async def load_provisioner_info(self, provisioner_info: dict[str, Any]) -> None:
        await super().load_provisioner_info(provisioner_info)
        self.jail_name = provisioner_info.get("jail_name")
        self.guest_ip = provisioner_info.get("guest_ip")
        known_hosts_file = provisioner_info.get("known_hosts_file")
        self.known_hosts_file = Path(known_hosts_file) if known_hosts_file else None
        self._runtime_created = bool(provisioner_info.get("runtime_created"))
        self._original_connection_ip = provisioner_info.get("original_connection_ip")
        tunnel_ports = provisioner_info.get("tunnel_ports", [])
        self._tunnel_ports = tuple(int(value) for value in tunnel_ports)
