from __future__ import annotations

import asyncio
import os
import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, ClassVar

from jupyter_client.provisioning import LocalProvisioner
from traitlets import Int, Unicode

from .remote_kernel import (
    CONNECTION_PORT_FIELDS,
    LocalPortLeasePool,
    LocalPortReservation,
    SSHTransport,
    connection_ports,
    create_runtime_ssh_key,
    release_jupyter_cached_ports,
    remote_kernel_command,
    restore_connection_file,
    rewrite_connection_file,
)
from .runtime_client import DEFAULT_RUNTIME_SOCKET, RuntimeClient, RuntimeControlError


def runtime_name(kernel_id: str) -> str:
    if not isinstance(kernel_id, str):
        raise ValueError("kernel_id must be a string")
    compact = re.sub(r"[^a-zA-Z0-9]", "", kernel_id).lower()
    if not compact:
        raise ValueError("kernel_id does not contain a usable identifier")
    return f"freebsd-lab-{compact[:16]}"


def persisted_connection_ports(value: object) -> tuple[int, ...]:
    """Accept only a complete, valid persisted Jupyter port tuple."""

    if not isinstance(value, (list, tuple)):
        return ()
    try:
        document = dict(zip(CONNECTION_PORT_FIELDS, value, strict=True))
        return connection_ports(document)
    except ValueError:
        return ()


class RemoteRuntimeProvisioner(LocalProvisioner):
    """Shared SSH transport and lifecycle implementation for remote runtimes."""

    runtime_label: ClassVar[str] = "runtime"
    provisioner_name_key: ClassVar[str] = "runtime_name"
    default_startup_timeout: ClassVar[int] = 30

    runtime_socket: str = Unicode(DEFAULT_RUNTIME_SOCKET).tag(config=True)
    ssh_user: str = Unicode("freebsd").tag(config=True)
    ssh_keygen_command: str = Unicode("/usr/bin/ssh-keygen").tag(config=True)
    ssh_command: str = Unicode("/usr/bin/ssh").tag(config=True)
    scp_command: str = Unicode("/usr/bin/scp").tag(config=True)
    remote_connection_dir: str = Unicode("/tmp/freebsd-laboratory").tag(config=True)
    runtime_dir: str = Unicode("~/.cache/freebsd-laboratory/runtime").tag(config=True)
    startup_timeout: int = Int(30, min=5).tag(config=True)
    ssh_connect_timeout: int = Int(5, min=1).tag(config=True)
    ssh_connection_attempts: int = Int(3, min=1).tag(config=True)
    ssh_server_alive_interval: int = Int(15, min=1).tag(config=True)
    ssh_server_alive_count_max: int = Int(4, min=1).tag(config=True)
    tunnel_lease_dir: str = Unicode(
        "/var/run/freebsd-laboratory/tunnel-port-leases"
    ).tag(config=True)
    tunnel_port_start: int = Int(30000, min=1024, max=65535).tag(config=True)
    tunnel_port_end: int = Int(44999, min=1024, max=65535).tag(config=True)

    _runtime_name: str | None = None
    guest_ip: str | None = None
    known_hosts_file: Path | None = None
    _ssh_private_key: Path | None = None
    _runtime_created = False
    _original_connection_ip: str | None = None
    _original_connection_ports: tuple[int, ...] = ()
    _tunnel_ports: tuple[int, ...] = ()
    _tunnel_reservation: LocalPortReservation | None = None

    @staticmethod
    def _assert_supported_host() -> None:
        if platform.system() != "FreeBSD":
            raise RuntimeError("FreeBSD remote runtime provisioner requires a FreeBSD host")

    def _client(self) -> RuntimeClient:
        return RuntimeClient(
            self.runtime_socket,
            timeout=max(30.0, float(self.startup_timeout)),
        )

    def _request_create(
        self,
        name: str,
        owner_pid: int,
        ssh_public_key: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _runtime_path(self) -> Path:
        if self._runtime_name is None:
            raise RuntimeError(f"{self.runtime_label} name is not initialized")
        return Path(self.runtime_dir).expanduser() / self._runtime_name

    def _create_runtime_key(self, runtime_path: Path) -> str:
        private_key, material = create_runtime_ssh_key(
            runtime_path,
            ssh_keygen_command=self.ssh_keygen_command,
        )
        self._ssh_private_key = private_key
        return material

    def _transport(self) -> SSHTransport:
        if (
            self.guest_ip is None
            or self.known_hosts_file is None
            or self._ssh_private_key is None
        ):
            raise RuntimeError(f"{self.runtime_label} SSH transport is not initialized")
        return SSHTransport(
            host=self.guest_ip,
            user=self.ssh_user,
            private_key=str(self._ssh_private_key),
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

    @staticmethod
    def _remove_runtime_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)

    async def _create_runtime(self) -> None:
        self._runtime_name = runtime_name(str(self.kernel_id))
        runtime_path = self._runtime_path()
        self._remove_runtime_path(runtime_path)
        runtime_path.mkdir(parents=True, exist_ok=False, mode=0o700)
        runtime_path.chmod(0o700)
        self.known_hosts_file = runtime_path / "known_hosts"
        self.known_hosts_file.touch(mode=0o600)
        self.known_hosts_file.chmod(0o600)
        ssh_public_key = self._create_runtime_key(runtime_path)

        result = await asyncio.to_thread(
            self._request_create,
            self._runtime_name,
            os.getpid(),
            ssh_public_key,
        )
        self._runtime_created = True
        guest_ip = result.get("guest_ip")
        if not isinstance(guest_ip, str) or not guest_ip:
            raise RuntimeError(
                f"Runtime daemon did not return the {self.runtime_label} address"
            )
        self.guest_ip = guest_ip

    async def _destroy_runtime(self) -> None:
        self._release_tunnel_ports()
        name = self._runtime_name
        try:
            if self._runtime_created and name:
                try:
                    await asyncio.to_thread(self._client().destroy, name)
                except RuntimeControlError as error:
                    self.log.warning(
                        "Unable to destroy %s %s through runtime daemon; "
                        "stale-owner reconciliation will retry: %s",
                        self.runtime_label,
                        name,
                        error,
                    )
        finally:
            self._runtime_created = False
            if name:
                self._remove_runtime_path(
                    Path(self.runtime_dir).expanduser() / name
                )
            self._runtime_name = None
            self.guest_ip = None
            self.known_hosts_file = None
            self._ssh_private_key = None

    async def pre_launch(self, **kwargs: Any) -> dict[str, Any]:
        self._assert_supported_host()
        prepared = await super().pre_launch(**kwargs)
        try:
            await self._create_runtime()
            transport = self._transport()
            transport.assert_available()

            if self.parent is None or self._runtime_name is None:
                raise RuntimeError(
                    f"Kernel manager or {self.runtime_label} name is unavailable"
                )

            async def _prepare_connection() -> tuple[Path, tuple[int, ...]]:
                reservation = await asyncio.to_thread(
                    self._port_pool().allocate,
                    self._runtime_name,
                    os.getpid(),
                    len(CONNECTION_PORT_FIELDS),
                )
                self._tunnel_reservation = reservation

                host_conn, original_ip, original_ports, tunnel_ports = (
                    rewrite_connection_file(
                        self.parent,
                        ports=reservation.ports,
                    )
                )
                self._original_connection_ip = original_ip
                self._original_connection_ports = original_ports
                self._tunnel_ports = tunnel_ports
                release_jupyter_cached_ports(self, original_ports)
                self.connection_info = self.parent.get_connection_info()
                return host_conn, tunnel_ports

            _, (host_connection, tunnel_ports) = await asyncio.gather(
                asyncio.to_thread(transport.wait_until_ready, self.startup_timeout),
                _prepare_connection(),
            )

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
                self.provisioner_name_key: self._runtime_name,
                "guest_ip": self.guest_ip,
                "known_hosts_file": (
                    str(self.known_hosts_file) if self.known_hosts_file else None
                ),
                "runtime_created": self._runtime_created,
                "original_connection_ip": self._original_connection_ip,
                "original_connection_ports": list(self._original_connection_ports),
                "tunnel_ports": list(self._tunnel_ports),
            }
        )
        return info

    async def load_provisioner_info(self, provisioner_info: dict[str, Any]) -> None:
        await super().load_provisioner_info(provisioner_info)
        if not isinstance(provisioner_info, dict):
            return
        name = provisioner_info.get(self.provisioner_name_key)
        self._runtime_name = str(name) if isinstance(name, str) and name else None
        guest_ip = provisioner_info.get("guest_ip")
        self.guest_ip = str(guest_ip) if isinstance(guest_ip, str) and guest_ip else None
        known_hosts_file = provisioner_info.get("known_hosts_file")
        self.known_hosts_file = (
            Path(known_hosts_file) if isinstance(known_hosts_file, str) and known_hosts_file else None
        )
        if self._runtime_name is not None:
            candidate = Path(self.runtime_dir).expanduser() / self._runtime_name / "id_ed25519"
            self._ssh_private_key = (
                candidate if candidate.is_file() and not candidate.is_symlink() else None
            )
        else:
            self._ssh_private_key = None
        self._runtime_created = bool(provisioner_info.get("runtime_created"))
        original_ip = provisioner_info.get("original_connection_ip")
        self._original_connection_ip = str(original_ip) if isinstance(original_ip, str) and original_ip else None
        self._original_connection_ports = persisted_connection_ports(
            provisioner_info.get("original_connection_ports", [])
        )
        self._tunnel_ports = persisted_connection_ports(
            provisioner_info.get("tunnel_ports", [])
        )
