from __future__ import annotations

import asyncio
import ipaddress
import os
import platform
import shlex
import shutil
from pathlib import Path
from typing import Any

from jupyter_client.provisioning import LocalProvisioner
from traitlets import Int, Unicode

from .network import IPv4LeasePool
from .provisioner import runtime_name
from .remote_kernel import (
    CONNECTION_PORT_FIELDS,
    LocalPortLeasePool,
    LocalPortReservation,
    SSHTransport,
    remote_kernel_command,
    restore_connection_file,
    rewrite_connection_file,
)
from .runtime_client import DEFAULT_RUNTIME_SOCKET, RuntimeClient


def build_netconfig(
    interface: str,
    address: str,
    network_cidr: str,
    hostname: str,
    gateway4: str = "",
    nameservers: str = "",
) -> str:
    """Compatibility helper for validating vm-bhyve static network arguments."""
    network = ipaddress.ip_network(network_cidr, strict=False)
    ip = ipaddress.ip_address(address)
    if ip not in network:
        raise ValueError("Guest address is outside network_cidr")
    fields = [
        f"interface={interface}",
        f"ip={ip}/{network.prefixlen}",
        f"hostname={hostname}",
    ]
    if gateway4:
        fields.append(f"gateway4={gateway4}")
    if nameservers:
        fields.append(f"nameservers={nameservers}")
    return ";".join(fields)


class FreeBSDBhyveProvisioner(LocalProvisioner):
    """Run ipykernel inside an ephemeral bhyve VM on the private lab switch.

    The root-owned runtime daemon performs vm-bhyve lifecycle operations. The
    unprivileged Jupyter process reaches the guest only through SSH. Jupyter TCP
    channels are forwarded over the attached SSH process and remain loopback-only
    inside the VM.
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
    startup_timeout: int = Int(90, min=5).tag(config=True)
    ssh_connect_timeout: int = Int(5, min=1).tag(config=True)
    ssh_connection_attempts: int = Int(3, min=1).tag(config=True)
    ssh_server_alive_interval: int = Int(15, min=1).tag(config=True)
    ssh_server_alive_count_max: int = Int(4, min=1).tag(config=True)
    tunnel_lease_dir: str = Unicode(
        "/var/run/freebsd-laboratory/tunnel-port-leases"
    ).tag(config=True)
    tunnel_port_start: int = Int(30000, min=1024, max=65535).tag(config=True)
    tunnel_port_end: int = Int(44999, min=1024, max=65535).tag(config=True)

    vm_name: str | None = None
    guest_ip: str | None = None
    known_hosts_file: Path | None = None
    _runtime_created = False
    _original_connection_ip: str | None = None
    _original_connection_ports: tuple[int, ...] = ()
    _tunnel_ports: tuple[int, ...] = ()
    _tunnel_reservation: LocalPortReservation | None = None

    @staticmethod
    def _assert_supported_host() -> None:
        if platform.system() != "FreeBSD":
            raise RuntimeError("bhyve provisioner requires a FreeBSD host")

    def _client(self) -> RuntimeClient:
        return RuntimeClient(self.runtime_socket, timeout=max(30.0, float(self.startup_timeout)))

    def _runtime_path(self) -> Path:
        if self.vm_name is None:
            raise RuntimeError("VM name is not initialized")
        return Path(self.runtime_dir).expanduser() / self.vm_name

    def _transport(self) -> SSHTransport:
        if self.guest_ip is None or self.known_hosts_file is None:
            raise RuntimeError("bhyve SSH transport is not initialized")
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

    def _port_pool(self) -> LocalPortLeasePool:
        return LocalPortLeasePool(
            self.tunnel_port_start,
            self.tunnel_port_end,
            Path(self.tunnel_lease_dir).expanduser(),
        )

    def _release_tunnel_ports(self) -> None:
        reservation, self._tunnel_reservation = self._tunnel_reservation, None
        if reservation is not None:
            reservation.release()
        self._tunnel_ports = ()

    async def _create_runtime(self) -> None:
        self.vm_name = runtime_name(str(self.kernel_id))
        runtime_path = self._runtime_path()
        runtime_path.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.known_hosts_file = runtime_path / "known_hosts"
        self.known_hosts_file.touch(mode=0o600)

        result = await asyncio.to_thread(
            self._client().create_bhyve,
            self.vm_name,
            os.getpid(),
        )
        guest_ip = result.get("guest_ip")
        if not isinstance(guest_ip, str) or not guest_ip:
            raise RuntimeError("Runtime daemon did not return the bhyve address")
        self.guest_ip = guest_ip
        self._runtime_created = True

    async def _destroy_runtime(self) -> None:
        self._release_tunnel_ports()
        name = self.vm_name
        if self._runtime_created and name:
            try:
                await asyncio.to_thread(self._client().destroy, name)
            finally:
                self._runtime_created = False
        if name:
            shutil.rmtree(Path(self.runtime_dir).expanduser() / name, ignore_errors=True)
        self.vm_name = None
        self.guest_ip = None
        self.known_hosts_file = None

    async def pre_launch(self, **kwargs: Any) -> dict[str, Any]:
        self._assert_supported_host()
        prepared = await super().pre_launch(**kwargs)
        try:
            await self._create_runtime()
            transport = self._transport()
            transport.assert_available()
            await asyncio.to_thread(transport.wait_until_ready, self.startup_timeout)

            if self.parent is None or self.vm_name is None:
                raise RuntimeError("Kernel manager or VM name is unavailable")

            reservation = await asyncio.to_thread(
                self._port_pool().allocate,
                self.vm_name,
                os.getpid(),
                len(CONNECTION_PORT_FIELDS),
            )
            self._tunnel_reservation = reservation

            host_connection, original_ip, original_ports, tunnel_ports = rewrite_connection_file(
                self.parent,
                ports=reservation.ports,
            )
            self._original_connection_ip = original_ip
            self._original_connection_ports = original_ports
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
                restore_connection_file(
                    self.parent,
                    self._original_connection_ip,
                    self._original_connection_ports,
                )
            self._original_connection_ip = None
            self._original_connection_ports = ()
            await self._destroy_runtime()
            raise

    async def launch_kernel(self, cmd: list[str], **kwargs: Any) -> dict[str, Any]:
        """Hand the pre-bound local ports directly from reservation to OpenSSH."""
        reservation = self._tunnel_reservation
        if reservation is not None:
            reservation.release_reservations()
        try:
            return await super().launch_kernel(cmd, **kwargs)
        except Exception:
            if self.parent is not None:
                restore_connection_file(
                    self.parent,
                    self._original_connection_ip,
                    self._original_connection_ports,
                )
            self._original_connection_ip = None
            self._original_connection_ports = ()
            await self._destroy_runtime()
            raise

    async def cleanup(self, restart: bool = False) -> None:
        try:
            await super().cleanup(restart=restart)
        finally:
            if self.parent is not None:
                restore_connection_file(
                    self.parent,
                    self._original_connection_ip,
                    self._original_connection_ports,
                )
            self._original_connection_ip = None
            self._original_connection_ports = ()
            await self._destroy_runtime()

    async def get_provisioner_info(self) -> dict[str, Any]:
        info = await super().get_provisioner_info()
        info.update(
            {
                "vm_name": self.vm_name,
                "guest_ip": self.guest_ip,
                "known_hosts_file": str(self.known_hosts_file) if self.known_hosts_file else None,
                "runtime_created": self._runtime_created,
                "original_connection_ip": self._original_connection_ip,
                "original_connection_ports": list(self._original_connection_ports),
                "tunnel_ports": list(self._tunnel_ports),
            }
        )
        return info

    async def load_provisioner_info(self, provisioner_info: dict[str, Any]) -> None:
        await super().load_provisioner_info(provisioner_info)
        self.vm_name = provisioner_info.get("vm_name")
        self.guest_ip = provisioner_info.get("guest_ip")
        known_hosts_file = provisioner_info.get("known_hosts_file")
        self.known_hosts_file = Path(known_hosts_file) if known_hosts_file else None
        self._runtime_created = bool(provisioner_info.get("runtime_created"))
        self._original_connection_ip = provisioner_info.get("original_connection_ip")
        original_ports = provisioner_info.get("original_connection_ports", [])
        self._original_connection_ports = tuple(int(value) for value in original_ports)
        tunnel_ports = provisioner_info.get("tunnel_ports", [])
        self._tunnel_ports = tuple(int(value) for value in tunnel_ports)
