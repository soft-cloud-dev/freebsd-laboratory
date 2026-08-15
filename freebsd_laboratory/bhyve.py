from __future__ import annotations

import asyncio
import fcntl
import ipaddress
import json
import os
import platform
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from jupyter_client.provisioning import LocalProvisioner
from traitlets import Int, Unicode

from .provisioner import runtime_name


class IPv4LeasePool:
    """Small file-backed address allocator for concurrent ephemeral VMs."""

    def __init__(self, network: str, start: str, end: str, lease_dir: str | Path) -> None:
        self.network = ipaddress.ip_network(network, strict=False)
        if self.network.version != 4:
            raise ValueError("bhyve kernel transport currently requires an IPv4 network")
        self.start = ipaddress.ip_address(start)
        self.end = ipaddress.ip_address(end)
        if self.start not in self.network or self.end not in self.network:
            raise ValueError("Address pool must be contained in network_cidr")
        if int(self.start) > int(self.end):
            raise ValueError("address_start must not be greater than address_end")
        self.lease_dir = Path(lease_dir)

    def _lease_path(self, address: ipaddress.IPv4Address) -> Path:
        return self.lease_dir / f"{address}.lease"

    def allocate(self, owner: str) -> str:
        self.lease_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.lease_dir / ".lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                for value in range(int(self.start), int(self.end) + 1):
                    address = ipaddress.ip_address(value)
                    lease_path = self._lease_path(address)
                    try:
                        descriptor = os.open(
                            lease_path,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                        )
                    except FileExistsError:
                        continue
                    try:
                        os.write(descriptor, f"{owner}\n".encode("utf-8"))
                    finally:
                        os.close(descriptor)
                    return str(address)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        raise RuntimeError("No free IPv4 addresses remain in the bhyve laboratory pool")

    def release(self, address: str, owner: str) -> bool:
        parsed = ipaddress.ip_address(address)
        if parsed not in self.network:
            return False
        self.lease_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.lease_dir / ".lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                lease_path = self._lease_path(parsed)
                if not lease_path.exists():
                    return False
                recorded_owner = lease_path.read_text(encoding="utf-8").strip()
                if recorded_owner != owner:
                    return False
                lease_path.unlink()
                return True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def build_netconfig(
    interface: str,
    address: str,
    network_cidr: str,
    hostname: str,
    gateway4: str = "",
    nameservers: str = "",
) -> str:
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


class FreeBSDBhyveProvisioner(LocalProvisioner):
    """Launch a Jupyter kernel inside an ephemeral bhyve virtual machine.

    vm-bhyve is used as the lifecycle manager around the base-system bhyve
    hypervisor. Each kernel receives a private address from a host-side lease
    pool. The Jupyter connection file is rebound to that guest address, copied
    over SSH, and the kernel process is kept attached to a local SSH process so
    the standard LocalProvisioner lifecycle remains usable.
    """

    vm_command: str = Unicode(
        "/usr/local/sbin/vm",
        help="Path to the vm-bhyve command.",
    ).tag(config=True)
    vm_template: str = Unicode(
        "freebsd-lab",
        help="vm-bhyve template used for ephemeral laboratory VMs.",
    ).tag(config=True)
    vm_image: str = Unicode(
        "freebsd-python.raw",
        help="Prepared vm-bhyve image containing FreeBSD, SSH, Python and ipykernel.",
    ).tag(config=True)
    network_cidr: str = Unicode(
        "172.31.254.0/24",
        help="Private network used for Jupyter-to-guest kernel transport.",
    ).tag(config=True)
    address_start: str = Unicode("172.31.254.100").tag(config=True)
    address_end: str = Unicode("172.31.254.199").tag(config=True)
    network_interface: str = Unicode("vtnet0").tag(config=True)
    gateway4: str = Unicode("").tag(config=True)
    nameservers: str = Unicode("").tag(config=True)
    lease_dir: str = Unicode(
        "/var/run/freebsd-laboratory/bhyve-leases",
        help="File-backed lease directory used to prevent concurrent address reuse.",
    ).tag(config=True)
    runtime_dir: str = Unicode(
        "/var/run/freebsd-laboratory/bhyve",
        help="Host directory for per-VM SSH known-host state.",
    ).tag(config=True)
    ssh_user: str = Unicode("freebsd").tag(config=True)
    ssh_private_key: str = Unicode(
        "/usr/local/etc/freebsd-laboratory/id_ed25519"
    ).tag(config=True)
    ssh_public_key: str = Unicode(
        "/usr/local/etc/freebsd-laboratory/id_ed25519.pub"
    ).tag(config=True)
    ssh_command: str = Unicode("/usr/bin/ssh").tag(config=True)
    scp_command: str = Unicode("/usr/bin/scp").tag(config=True)
    remote_connection_dir: str = Unicode("/tmp/freebsd-laboratory").tag(config=True)
    user_data_file: str = Unicode(
        "",
        help="Optional cloud-init user-data file passed to vm create -u.",
    ).tag(config=True)
    startup_timeout: int = Int(90, min=5).tag(config=True)

    vm_name: str | None = None
    guest_ip: str | None = None
    known_hosts_file: Path | None = None
    _vm_created = False
    _lease_acquired = False
    _original_connection_ip: str | None = None

    @staticmethod
    def _executable_exists(command: str) -> bool:
        if os.path.isabs(command):
            return Path(command).is_file() and os.access(command, os.X_OK)
        return shutil.which(command) is not None

    def _assert_supported_host(self) -> None:
        if platform.system() != "FreeBSD":
            raise RuntimeError("bhyve provisioner requires a FreeBSD host")
        if os.geteuid() != 0:
            raise PermissionError("bhyve provisioner currently requires root privileges")
        for command in (self.vm_command, self.ssh_command, self.scp_command):
            if not self._executable_exists(command):
                raise RuntimeError(f"Required executable is unavailable: {command}")
        for key_path in (self.ssh_private_key, self.ssh_public_key):
            if not Path(key_path).is_file():
                raise RuntimeError(f"Required SSH key is unavailable: {key_path}")
        if self.user_data_file and not Path(self.user_data_file).is_file():
            raise RuntimeError(f"cloud-init user-data file is unavailable: {self.user_data_file}")

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

    def _lease_pool(self) -> IPv4LeasePool:
        return IPv4LeasePool(
            self.network_cidr,
            self.address_start,
            self.address_end,
            self.lease_dir,
        )

    def _ssh_options(self) -> list[str]:
        if self.known_hosts_file is None:
            raise RuntimeError("SSH known-hosts file is not initialized")
        return [
            "-i",
            self.ssh_private_key,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=3",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={self.known_hosts_file}",
        ]

    def _target(self) -> str:
        if self.guest_ip is None:
            raise RuntimeError("Guest address is not initialized")
        return f"{self.ssh_user}@{self.guest_ip}"

    def _create_runtime(self) -> None:
        self.vm_name = runtime_name(str(self.kernel_id))
        self._vm_created = False
        self._lease_acquired = False

        vm_runtime_dir = Path(self.runtime_dir) / self.vm_name
        vm_runtime_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.known_hosts_file = vm_runtime_dir / "known_hosts"
        self.known_hosts_file.touch(mode=0o600)

        try:
            if self._run([self.vm_command, "info", self.vm_name], check=False).returncode == 0:
                raise RuntimeError(f"Refusing to replace existing vm-bhyve guest: {self.vm_name}")

            self.guest_ip = self._lease_pool().allocate(self.vm_name)
            self._lease_acquired = True
            netconfig = build_netconfig(
                self.network_interface,
                self.guest_ip,
                self.network_cidr,
                self.vm_name,
                self.gateway4,
                self.nameservers,
            )

            create_command = [
                self.vm_command,
                "create",
                "-t",
                self.vm_template,
                "-i",
                self.vm_image,
                "-C",
                "-k",
                self.ssh_public_key,
                "-n",
                netconfig,
            ]
            if self.user_data_file:
                create_command.extend(["-u", self.user_data_file])
            create_command.append(self.vm_name)

            self._run(create_command)
            self._vm_created = True
            self._run([self.vm_command, "start", self.vm_name])
            self._wait_for_ssh()
        except Exception:
            self._destroy_runtime()
            raise

    def _wait_for_ssh(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_detail = "SSH did not become ready"
        while time.monotonic() < deadline:
            result = self._run(
                [self.ssh_command, *self._ssh_options(), self._target(), "true"],
                check=False,
                timeout=5,
            )
            if result.returncode == 0:
                return
            last_detail = result.stderr.strip() or result.stdout.strip() or last_detail
            time.sleep(1)
        raise RuntimeError(
            f"Timed out waiting for {self.vm_name} ({self.guest_ip}) SSH: {last_detail}"
        )

    def _rewrite_connection_file(self) -> tuple[Path, str]:
        if self.parent is None or not getattr(self.parent, "connection_file", None):
            raise RuntimeError("Kernel manager connection file is unavailable")
        if self.guest_ip is None:
            raise RuntimeError("Guest address is not initialized")

        host_path = Path(self.parent.connection_file).resolve()
        document = json.loads(host_path.read_text(encoding="utf-8"))
        self._original_connection_ip = str(document.get("ip", getattr(self.parent, "ip", "")))
        document["ip"] = self.guest_ip
        setattr(self.parent, "ip", self.guest_ip)

        temporary = host_path.with_name(f".{host_path.name}.bhyve.tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(host_path)

        remote_path = f"{self.remote_connection_dir.rstrip('/')}/{host_path.name}"
        return host_path, remote_path

    def _restore_connection_ip(self) -> None:
        if self._original_connection_ip is None or self.parent is None:
            return
        setattr(self.parent, "ip", self._original_connection_ip)
        connection_file = getattr(self.parent, "connection_file", None)
        if connection_file:
            path = Path(connection_file)
            if path.is_file():
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                    document["ip"] = self._original_connection_ip
                    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
                    path.chmod(0o600)
                except (OSError, ValueError, TypeError):
                    pass
        self._original_connection_ip = None

    def _stage_connection_file(self, host_path: Path, remote_path: str) -> None:
        self._run(
            [
                self.ssh_command,
                *self._ssh_options(),
                self._target(),
                f"install -d -m 700 {shlex.quote(self.remote_connection_dir)}",
            ]
        )
        self._run(
            [
                self.scp_command,
                *self._ssh_options(),
                str(host_path),
                f"{self._target()}:{remote_path}",
            ]
        )

    def _destroy_runtime(self) -> None:
        vm_name = self.vm_name
        guest_ip = self.guest_ip
        if self._vm_created and vm_name:
            self._run([self.vm_command, "poweroff", "-f", vm_name], check=False, timeout=15)
            self._run([self.vm_command, "destroy", "-f", vm_name], check=False, timeout=30)
        if self._lease_acquired and guest_ip and vm_name:
            self._lease_pool().release(guest_ip, vm_name)

        if vm_name:
            shutil.rmtree(Path(self.runtime_dir) / vm_name, ignore_errors=True)
        self._vm_created = False
        self._lease_acquired = False
        self.vm_name = None
        self.guest_ip = None
        self.known_hosts_file = None

    async def pre_launch(self, **kwargs: Any) -> dict[str, Any]:
        self._assert_supported_host()
        prepared = await super().pre_launch(**kwargs)
        try:
            await asyncio.to_thread(self._create_runtime)
            host_connection, remote_connection = self._rewrite_connection_file()
            await asyncio.to_thread(
                self._stage_connection_file,
                host_connection,
                remote_connection,
            )
            kernel_command = remote_kernel_command(
                list(prepared["cmd"]),
                host_connection,
                remote_connection,
            )
            prepared["cmd"] = [
                self.ssh_command,
                *self._ssh_options(),
                self._target(),
                shlex.join(kernel_command),
            ]
            prepared.pop("cwd", None)
            return prepared
        except Exception:
            self._restore_connection_ip()
            await asyncio.to_thread(self._destroy_runtime)
            raise

    async def cleanup(self, restart: bool = False) -> None:
        try:
            await super().cleanup(restart=restart)
        finally:
            await asyncio.to_thread(self._destroy_runtime)

    async def get_provisioner_info(self) -> dict[str, Any]:
        info = await super().get_provisioner_info()
        info.update(
            {
                "vm_name": self.vm_name,
                "guest_ip": self.guest_ip,
                "known_hosts_file": (
                    str(self.known_hosts_file) if self.known_hosts_file else None
                ),
                "vm_created": self._vm_created,
                "lease_acquired": self._lease_acquired,
            }
        )
        return info

    async def load_provisioner_info(self, provisioner_info: dict[str, Any]) -> None:
        await super().load_provisioner_info(provisioner_info)
        self.vm_name = provisioner_info.get("vm_name")
        self.guest_ip = provisioner_info.get("guest_ip")
        known_hosts_file = provisioner_info.get("known_hosts_file")
        self.known_hosts_file = Path(known_hosts_file) if known_hosts_file else None
        self._vm_created = bool(provisioner_info.get("vm_created"))
        self._lease_acquired = bool(provisioner_info.get("lease_acquired"))
