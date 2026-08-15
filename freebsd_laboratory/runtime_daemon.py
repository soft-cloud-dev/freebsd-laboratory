from __future__ import annotations

import argparse
import errno
import grp
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import socketserver
import stat
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from .network import IPv4LeasePool
from .runtime_client import DEFAULT_RUNTIME_SOCKET


RUNTIME_PREFIX = "freebsd-lab-"
RUNTIME_NAME_RE = re.compile(r"^freebsd-lab-[a-z0-9]{1,16}$")
MAX_REQUEST_BYTES = 64 * 1024


@dataclass(frozen=True)
class RuntimeConfig:
    socket_path: str = DEFAULT_RUNTIME_SOCKET
    socket_group: str = "freebsdlab"
    registry_dir: str = "/var/db/freebsd-laboratory/runtimes"
    lease_dir: str = "/var/db/freebsd-laboratory/network-leases"
    network_cidr: str = "172.31.254.0/24"
    host_address: str = "172.31.254.1"
    address_start: str = "172.31.254.10"
    address_end: str = "172.31.254.199"
    bridge_name: str = "labbridge0"
    jail_template_snapshot: str = "zroot/jails/templates/freebsd-python@clean"
    jail_dataset_parent: str = "zroot/jails/containers"
    jail_mount_root: str = "/usr/local/jails/containers"
    jail_interface_name: str = "vnet0"
    jail_sshd_service: str = "/usr/sbin/service"
    vm_command: str = "/usr/local/sbin/vm"
    vm_template: str = "freebsd-lab"
    vm_image: str = "freebsd-python.raw"
    vm_switch: str = "freebsdlab"
    vm_interface: str = "vtnet0"
    ssh_public_key: str = "/usr/local/etc/freebsd-laboratory/id_ed25519.pub"


class RuntimeManager:
    """Root-owned lifecycle manager with a deliberately small command surface."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.registry_dir = Path(config.registry_dir)
        self.registry_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pool = IPv4LeasePool(
            config.network_cidr,
            config.address_start,
            config.address_end,
            config.lease_dir,
        )
        self.prefix_len = ipaddress.ip_network(config.network_cidr, strict=False).prefixlen
        if ipaddress.ip_address(config.host_address) not in ipaddress.ip_network(
            config.network_cidr, strict=False
        ):
            raise ValueError("host_address must be contained in network_cidr")

    @staticmethod
    def validate_name(name: str) -> str:
        if not isinstance(name, str) or not RUNTIME_NAME_RE.fullmatch(name):
            raise ValueError("Invalid FreeBSD Laboratory runtime name")
        return name

    @staticmethod
    def validate_owner_pid(owner_pid: int) -> int:
        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 1:
            raise ValueError("owner_pid must be a positive host process id")
        return owner_pid

    @staticmethod
    def _run(
        command: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = 60,
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

    def _registry_path(self, name: str) -> Path:
        return self.registry_dir / f"{name}.json"

    def _load_registry(self, name: str) -> dict[str, Any] | None:
        path = self._registry_path(name)
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Invalid runtime registry record: {path}") from error
        if not isinstance(document, dict):
            raise RuntimeError(f"Invalid runtime registry record: {path}")
        return document

    def _write_registry(self, record: dict[str, Any]) -> None:
        name = self.validate_name(str(record["name"]))
        target = self._registry_path(name)
        self.registry_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{name}.",
            dir=self.registry_dir,
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def _delete_registry(self, name: str) -> None:
        self._registry_path(name).unlink(missing_ok=True)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError as error:
            if error.errno == errno.ESRCH:
                return False
            if error.errno == errno.EPERM:
                return True
            raise
        return True

    def _ensure_bridge(self) -> None:
        result = self._run(["ifconfig", self.config.bridge_name], check=False)
        if result.returncode != 0:
            self._run(["ifconfig", "bridge", "create", "name", self.config.bridge_name])
            result = self._run(["ifconfig", self.config.bridge_name])

        expected = f"inet {self.config.host_address} "
        if expected not in result.stdout:
            self._run(
                [
                    "ifconfig",
                    self.config.bridge_name,
                    "inet",
                    f"{self.config.host_address}/{self.prefix_len}",
                    "up",
                ]
            )
        else:
            self._run(["ifconfig", self.config.bridge_name, "up"])

    def _ensure_vm_switch(self) -> None:
        result = self._run(
            [self.config.vm_command, "switch", "info", self.config.vm_switch],
            check=False,
        )
        if result.returncode == 0:
            return
        self._run(
            [
                self.config.vm_command,
                "switch",
                "create",
                "-t",
                "manual",
                "-b",
                self.config.bridge_name,
                self.config.vm_switch,
            ]
        )

    def _allocate(self, name: str) -> str:
        address = self.pool.allocate(name)
        if address == self.config.host_address:
            self.pool.release(address, name)
            raise RuntimeError("Address pool contains the host bridge address")
        return address

    def create_jail(self, name: str, owner_pid: int) -> dict[str, Any]:
        name = self.validate_name(name)
        owner_pid = self.validate_owner_pid(owner_pid)
        if self._load_registry(name) is not None:
            raise RuntimeError(f"Runtime already registered: {name}")

        self._ensure_bridge()
        dataset = f"{self.config.jail_dataset_parent.rstrip('/')}/{name}"
        jail_root = str((Path(self.config.jail_mount_root) / name).resolve())
        address = self._allocate(name)
        record: dict[str, Any] = {
            "schema": "softcloud.runtime/v1",
            "name": name,
            "type": "jail",
            "owner_pid": owner_pid,
            "guest_ip": address,
            "dataset": dataset,
            "jail_root": jail_root,
            "bridge": self.config.bridge_name,
            "epair_host": None,
            "epair_guest": None,
            "jail_created": False,
            "dataset_created": False,
        }
        self._write_registry(record)

        try:
            self._run(
                [
                    "zfs",
                    "clone",
                    "-o",
                    f"mountpoint={jail_root}",
                    self.config.jail_template_snapshot,
                    dataset,
                ]
            )
            record["dataset_created"] = True
            self._write_registry(record)

            epair_result = self._run(["ifconfig", "epair", "create"])
            epair_host = epair_result.stdout.strip().splitlines()[-1].strip()
            if not epair_host.endswith("a"):
                raise RuntimeError(f"Unexpected epair interface name: {epair_host}")
            epair_guest = f"{epair_host[:-1]}b"
            record["epair_host"] = epair_host
            record["epair_guest"] = epair_guest
            self._write_registry(record)

            self._run(["ifconfig", self.config.bridge_name, "addm", epair_host])
            self._run(["ifconfig", self.config.bridge_name, "private", epair_host])
            self._run(["ifconfig", epair_host, "up"])

            self._run(
                [
                    "jail",
                    "-c",
                    f"name={name}",
                    f"path={jail_root}",
                    f"host.hostname={name}",
                    "persist",
                    "exec.clean",
                    "mount.devfs",
                    "vnet",
                    f"vnet.interface={epair_guest}",
                    "ip4=disable",
                    "ip6=disable",
                ]
            )
            record["jail_created"] = True
            self._write_registry(record)

            self._run(
                [
                    "jexec",
                    name,
                    "ifconfig",
                    epair_guest,
                    "name",
                    self.config.jail_interface_name,
                ]
            )
            self._run(
                [
                    "jexec",
                    name,
                    "ifconfig",
                    self.config.jail_interface_name,
                    "inet",
                    f"{address}/{self.prefix_len}",
                    "up",
                ]
            )
            self._run(
                [
                    "jexec",
                    name,
                    self.config.jail_sshd_service,
                    "sshd",
                    "onestart",
                ]
            )
            return {
                "name": name,
                "type": "jail",
                "guest_ip": address,
                "network_cidr": self.config.network_cidr,
                "bridge": self.config.bridge_name,
                "interface": self.config.jail_interface_name,
            }
        except Exception:
            self.destroy(name)
            raise

    def create_bhyve(self, name: str, owner_pid: int) -> dict[str, Any]:
        name = self.validate_name(name)
        owner_pid = self.validate_owner_pid(owner_pid)
        if self._load_registry(name) is not None:
            raise RuntimeError(f"Runtime already registered: {name}")
        if not Path(self.config.ssh_public_key).is_file():
            raise RuntimeError(f"Runtime SSH public key is unavailable: {self.config.ssh_public_key}")

        self._ensure_bridge()
        self._ensure_vm_switch()
        if self._run([self.config.vm_command, "info", name], check=False).returncode == 0:
            raise RuntimeError(f"Refusing to replace existing vm-bhyve guest: {name}")

        address = self._allocate(name)
        record: dict[str, Any] = {
            "schema": "softcloud.runtime/v1",
            "name": name,
            "type": "bhyve",
            "owner_pid": owner_pid,
            "guest_ip": address,
            "bridge": self.config.bridge_name,
            "vm_created": False,
        }
        self._write_registry(record)

        try:
            netconfig = ";".join(
                [
                    f"interface={self.config.vm_interface}",
                    f"ip={address}/{self.prefix_len}",
                    f"hostname={name}",
                ]
            )
            self._run(
                [
                    self.config.vm_command,
                    "create",
                    "-t",
                    self.config.vm_template,
                    "-i",
                    self.config.vm_image,
                    "-C",
                    "-k",
                    self.config.ssh_public_key,
                    "-n",
                    netconfig,
                    name,
                ]
            )
            record["vm_created"] = True
            self._write_registry(record)
            self._run([self.config.vm_command, "start", name])
            return {
                "name": name,
                "type": "bhyve",
                "guest_ip": address,
                "network_cidr": self.config.network_cidr,
                "bridge": self.config.bridge_name,
                "interface": self.config.vm_interface,
            }
        except Exception:
            self.destroy(name)
            raise

    def _jail_exists(self, name: str) -> bool:
        return self._run(["jls", "-j", name, "name"], check=False).returncode == 0

    def _vm_exists(self, name: str) -> bool:
        return self._run([self.config.vm_command, "info", name], check=False).returncode == 0

    def _dataset_exists(self, dataset: str) -> bool:
        return self._run(["zfs", "list", "-H", "-o", "name", dataset], check=False).returncode == 0

    def destroy(self, name: str) -> dict[str, Any]:
        name = self.validate_name(name)
        record = self._load_registry(name) or {}
        runtime_type = record.get("type")
        removed: list[str] = []

        if runtime_type == "bhyve" or self._vm_exists(name):
            self._run([self.config.vm_command, "poweroff", "-f", name], check=False, timeout=20)
            result = self._run(
                [self.config.vm_command, "destroy", "-f", name], check=False, timeout=60
            )
            if result.returncode == 0:
                removed.append("bhyve")

        if runtime_type == "jail" or self._jail_exists(name):
            result = self._run(["jail", "-r", name], check=False, timeout=30)
            if result.returncode == 0:
                removed.append("jail")

        epair_host = record.get("epair_host")
        if isinstance(epair_host, str) and epair_host:
            result = self._run(["ifconfig", epair_host, "destroy"], check=False)
            if result.returncode == 0:
                removed.append(epair_host)

        dataset = record.get("dataset")
        if not isinstance(dataset, str) or not dataset:
            dataset = f"{self.config.jail_dataset_parent.rstrip('/')}/{name}"
        if self._dataset_exists(dataset):
            result = self._run(["zfs", "destroy", "-r", dataset], check=False, timeout=60)
            if result.returncode == 0:
                removed.append(dataset)

        guest_ip = record.get("guest_ip")
        if isinstance(guest_ip, str) and guest_ip:
            self.pool.release(guest_ip, name)
        self._delete_registry(name)
        return {"name": name, "removed": removed}

    def _registered(self) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for path in sorted(self.registry_dir.glob(f"{RUNTIME_PREFIX}*.json")):
            name = path.stem
            if not RUNTIME_NAME_RE.fullmatch(name):
                continue
            record = self._load_registry(name)
            if record is not None:
                records[name] = record
        return records

    def _discover_jails(self) -> set[str]:
        result = self._run(["jls", "-n", "name"], check=False)
        names: set[str] = set()
        if result.returncode != 0:
            return names
        for match in re.finditer(r"(?:^|\s)name=(?:\"([^\"]+)\"|(\S+))", result.stdout):
            name = match.group(1) or match.group(2)
            if name and RUNTIME_NAME_RE.fullmatch(name):
                names.add(name)
        return names

    def _discover_vms(self) -> set[str]:
        result = self._run([self.config.vm_command, "list"], check=False)
        if result.returncode != 0:
            return set()
        names: set[str] = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            if not fields:
                continue
            name = fields[0]
            if RUNTIME_NAME_RE.fullmatch(name):
                names.add(name)
        return names

    def _discover_datasets(self) -> set[str]:
        result = self._run(
            ["zfs", "list", "-H", "-o", "name", "-r", self.config.jail_dataset_parent],
            check=False,
        )
        if result.returncode != 0:
            return set()
        prefix = f"{self.config.jail_dataset_parent.rstrip('/')}/{RUNTIME_PREFIX}"
        return {line.strip() for line in result.stdout.splitlines() if line.strip().startswith(prefix)}

    def _bridge_epairs(self) -> set[str]:
        result = self._run(["ifconfig", self.config.bridge_name], check=False)
        if result.returncode != 0:
            return set()
        members: set[str] = set()
        for match in re.finditer(r"member:\s+(epair\d+a)\b", result.stdout):
            members.add(match.group(1))
        return members

    def gc(self, *, stale_only: bool = True) -> dict[str, Any]:
        registered = self._registered()
        retained: set[str] = set()
        cleaned: list[str] = []

        for name, record in registered.items():
            owner_pid = record.get("owner_pid")
            alive = isinstance(owner_pid, int) and self._pid_alive(owner_pid)
            if stale_only and alive:
                retained.add(name)
                continue
            self.destroy(name)
            cleaned.append(name)

        registered_after = self._registered()
        retained.update(registered_after)

        discovered = self._discover_jails() | self._discover_vms()
        discovered.update(dataset.rsplit("/", 1)[-1] for dataset in self._discover_datasets())
        for name in sorted(discovered - retained):
            self.destroy(name)
            cleaned.append(name)

        referenced_epairs = {
            str(record.get("epair_host"))
            for record in self._registered().values()
            if record.get("epair_host")
        }
        removed_epairs: list[str] = []
        for interface in sorted(self._bridge_epairs() - referenced_epairs):
            result = self._run(["ifconfig", interface, "destroy"], check=False)
            if result.returncode == 0:
                removed_epairs.append(interface)

        active_owners = set(self._registered())
        released_addresses = self.pool.clear_orphans(active_owners)
        return {
            "cleaned": sorted(set(cleaned)),
            "retained": sorted(active_owners),
            "removed_epairs": removed_epairs,
            "released_addresses": released_addresses,
            "stale_only": stale_only,
        }


class RuntimeRequestHandler(socketserver.StreamRequestHandler):
    manager: RuntimeManager

    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            self._reply({"ok": False, "error": "request exceeded size limit"})
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            result = self._dispatch(request)
            self._reply({"ok": True, "result": result})
        except Exception as error:
            self._reply({"ok": False, "error": str(error)})

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "ping":
            return {"service": "freebsd-laboratory-runtime", "version": 1}
        if action == "create-jail":
            return self.manager.create_jail(
                str(request.get("name", "")),
                request.get("owner_pid"),
            )
        if action == "create-bhyve":
            return self.manager.create_bhyve(
                str(request.get("name", "")),
                request.get("owner_pid"),
            )
        if action == "destroy":
            return self.manager.destroy(str(request.get("name", "")))
        if action == "gc":
            stale_only = request.get("stale_only", True)
            if not isinstance(stale_only, bool):
                raise ValueError("stale_only must be boolean")
            return self.manager.gc(stale_only=stale_only)
        raise ValueError("unsupported runtime action")

    def _reply(self, response: dict[str, Any]) -> None:
        self.wfile.write(
            json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        self.wfile.flush()


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


def _configure_socket(path: Path, group_name: str) -> None:
    path.chmod(0o660)
    try:
        group = grp.getgrnam(group_name)
    except KeyError as error:
        raise RuntimeError(f"Runtime socket group does not exist: {group_name}") from error
    os.chown(path, 0, group.gr_gid)


def _prepare_socket_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not path.exists() and not path.is_symlink():
        return
    mode = path.lstat().st_mode
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"Refusing to replace non-socket path: {path}")
    path.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FreeBSD Laboratory privileged runtime daemon")
    parser.add_argument("--socket", default=DEFAULT_RUNTIME_SOCKET)
    parser.add_argument("--group", default="freebsdlab")
    parser.add_argument("--registry-dir", default="/var/db/freebsd-laboratory/runtimes")
    parser.add_argument("--lease-dir", default="/var/db/freebsd-laboratory/network-leases")
    parser.add_argument("--network", default="172.31.254.0/24")
    parser.add_argument("--host-address", default="172.31.254.1")
    parser.add_argument("--address-start", default="172.31.254.10")
    parser.add_argument("--address-end", default="172.31.254.199")
    parser.add_argument("--bridge", default="labbridge0")
    parser.add_argument("--jail-template", default="zroot/jails/templates/freebsd-python@clean")
    parser.add_argument("--jail-dataset-parent", default="zroot/jails/containers")
    parser.add_argument("--jail-mount-root", default="/usr/local/jails/containers")
    parser.add_argument("--vm-command", default="/usr/local/sbin/vm")
    parser.add_argument("--vm-template", default="freebsd-lab")
    parser.add_argument("--vm-image", default="freebsd-python.raw")
    parser.add_argument("--vm-switch", default="freebsdlab")
    parser.add_argument(
        "--ssh-public-key",
        default="/usr/local/etc/freebsd-laboratory/id_ed25519.pub",
    )
    parser.add_argument("--no-reconcile", action="store_true")
    return parser


def main() -> None:
    if platform.system() != "FreeBSD":
        raise SystemExit("freebsd-lab-runtime-daemon requires FreeBSD")
    if os.geteuid() != 0:
        raise SystemExit("freebsd-lab-runtime-daemon must run as root")

    args = build_parser().parse_args()
    config = RuntimeConfig(
        socket_path=args.socket,
        socket_group=args.group,
        registry_dir=args.registry_dir,
        lease_dir=args.lease_dir,
        network_cidr=args.network,
        host_address=args.host_address,
        address_start=args.address_start,
        address_end=args.address_end,
        bridge_name=args.bridge,
        jail_template_snapshot=args.jail_template,
        jail_dataset_parent=args.jail_dataset_parent,
        jail_mount_root=args.jail_mount_root,
        vm_command=args.vm_command,
        vm_template=args.vm_template,
        vm_image=args.vm_image,
        vm_switch=args.vm_switch,
        ssh_public_key=args.ssh_public_key,
    )
    manager = RuntimeManager(config)
    if not args.no_reconcile:
        manager.gc(stale_only=True)

    socket_path = Path(config.socket_path)
    _prepare_socket_path(socket_path)

    handler_type = type("BoundRuntimeRequestHandler", (RuntimeRequestHandler,), {"manager": manager})
    with ThreadingUnixServer(str(socket_path), handler_type) as server:
        _configure_socket(socket_path, config.socket_group)
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
