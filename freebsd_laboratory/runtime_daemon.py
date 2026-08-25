from __future__ import annotations

import argparse
import base64
import binascii
import grp
import ipaddress
import json
import os
import platform
import re
import shlex
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Sequence

from .network import IPv4LeasePool
from .peer_credentials import PeerCredentials, freebsd_peer_credentials
from .process_identity import process_matches, query_process_identity
from .runtime_client import DEFAULT_RUNTIME_SOCKET


RUNTIME_PREFIX = "freebsd-lab-"
RUNTIME_NAME_RE = re.compile(r"^freebsd-lab-[a-z0-9]{1,16}$")
MAX_REQUEST_BYTES = 64 * 1024


def _serialized_lifecycle(method: Any) -> Any:
    @wraps(method)
    def wrapped(self: "RuntimeManager", *args: Any, **kwargs: Any) -> Any:
        with self._lifecycle_lock:
            return method(self, *args, **kwargs)

    return wrapped


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
    jail_ssh_user: str = "freebsd"
    jail_sshd_service: str = "/usr/sbin/service"
    vm_command: str = "/usr/local/sbin/vm"
    vm_template: str = "freebsd-lab"
    vm_memdisk_template: str = "freebsd-lab-memdisk"
    vm_image: str = "freebsd-python.raw"
    vm_switch: str = "freebsdlab"
    vm_interface: str = "vtnet0"
    vm_disk_backend: str = "zvol-clone"
    vm_zvol_snapshot: str = "zroot/vm/.zvol/freebsd-python@ready"
    vm_zvol_parent: str = "zroot/vm/.zvol"
    vm_dataset_parent: str = "zroot/vm"
    vm_disk_size: str = "8G"
    vm_memdisk_type: str = "swap"
    ssh_public_key: str = "/usr/local/etc/freebsd-laboratory/id_ed25519.pub"


@dataclass(frozen=True)
class RuntimeOwner:
    pid: int
    uid: int
    gid: int
    started_at: str
    process_digest: str

    def registry_fields(self) -> dict[str, Any]:
        return {
            "owner_pid": self.pid,
            "owner_uid": self.uid,
            "owner_gid": self.gid,
            "owner_started_at": self.started_at,
            "owner_process_digest": self.process_digest,
        }


@dataclass(frozen=True)
class BhyveGuestProfile:
    template: str
    memdisk_template: str
    image: str
    zvol_snapshot: str
    interface: str
    guest_os: str = "freebsd"


BHYVE_PROFILES: dict[str, BhyveGuestProfile] = {
    "freebsd-python": BhyveGuestProfile(
        template="freebsd-lab",
        memdisk_template="freebsd-lab-memdisk",
        image="freebsd-python.raw",
        zvol_snapshot="zroot/vm/.zvol/freebsd-python@ready",
        interface="vtnet0",
        guest_os="freebsd",
    ),
    "linux-python": BhyveGuestProfile(
        template="linux-lab",
        memdisk_template="linux-lab-memdisk",
        image="linux-python.raw",
        zvol_snapshot="zroot/vm/.zvol/linux-python@ready",
        interface="eth0",
        guest_os="linux",
    ),
}


class RuntimeManager:
    """Root-owned lifecycle manager with an authenticated, constrained API."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self._lifecycle_lock = threading.RLock()
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
    def validate_ssh_public_key(value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise ValueError("Runtime SSH public key is invalid")
        if "\n" in value or "\r" in value:
            raise ValueError("Runtime SSH public key must be one line")
        fields = value.strip().split(maxsplit=2)
        if len(fields) < 2 or fields[0] != "ssh-ed25519":
            raise ValueError("Runtime SSH public key must be Ed25519")
        try:
            blob = base64.b64decode(fields[1], validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Runtime SSH public key has invalid base64") from error
        if (
            len(blob) != 51
            or blob[:4] != (11).to_bytes(4, "big")
            or blob[4:15] != b"ssh-ed25519"
            or blob[15:19] != (32).to_bytes(4, "big")
        ):
            raise ValueError("Runtime SSH public key has invalid Ed25519 encoding")
        return f"ssh-ed25519 {fields[1]}"

    @staticmethod
    def _run(
        command: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = 60,
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
            if check:
                raise RuntimeError(
                    f"Unable to execute {shlex.join(normalized)}: {error}"
                ) from error
            return subprocess.CompletedProcess(normalized, 127, "", str(error))
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise RuntimeError(f"{shlex.join(normalized)}: {detail}")
        return result

    @staticmethod
    def _owner_from_peer(peer: PeerCredentials, requested_pid: int) -> RuntimeOwner:
        owner_pid = RuntimeManager.validate_owner_pid(requested_pid)
        if owner_pid != peer.pid:
            raise PermissionError(
                f"owner_pid {owner_pid} does not match authenticated peer pid {peer.pid}"
            )
        identity = query_process_identity(peer.pid)
        if identity is None:
            raise PermissionError("Unable to fingerprint authenticated peer process")
        if identity.uid != peer.uid:
            raise PermissionError(
                f"Authenticated peer uid {peer.uid} does not own pid {peer.pid}"
            )
        return RuntimeOwner(
            pid=peer.pid,
            uid=peer.uid,
            gid=peer.gid,
            started_at=identity.started_at,
            process_digest=identity.digest,
        )

    @staticmethod
    def _owner_alive(record: dict[str, Any]) -> bool:
        owner_pid = record.get("owner_pid")
        owner_uid = record.get("owner_uid")
        process_digest = record.get("owner_process_digest")
        if (
            isinstance(owner_pid, bool)
            or not isinstance(owner_pid, int)
            or owner_pid <= 1
            or isinstance(owner_uid, bool)
            or not isinstance(owner_uid, int)
            or not isinstance(process_digest, str)
            or not process_digest
        ):
            return False
        return process_matches(owner_pid, owner_uid, process_digest)

    @staticmethod
    def _authorize_record(record: dict[str, Any], requester_uid: int | None) -> None:
        if (
            requester_uid is None
            or isinstance(requester_uid, bool)
            or not isinstance(requester_uid, int)
            or requester_uid < 0
        ):
            raise PermissionError("Runtime operation has no authenticated requester")
        if requester_uid == 0:
            return
        owner_uid = record.get("owner_uid")
        if (
            isinstance(owner_uid, bool)
            or not isinstance(owner_uid, int)
            or owner_uid != requester_uid
        ):
            raise PermissionError("Runtime is owned by another Unix user")

    @staticmethod
    def _record_owned_by(record: dict[str, Any], owner: RuntimeOwner) -> bool:
        return (
            record.get("owner_pid") == owner.pid
            and record.get("owner_uid") == owner.uid
            and record.get("owner_gid") == owner.gid
            and record.get("owner_process_digest") == owner.process_digest
        )

    def _registry_path(self, name: str) -> Path:
        return self.registry_dir / f"{name}.json"

    def _load_registry(self, name: str) -> dict[str, Any] | None:
        path = self._registry_path(name)
        if path.is_symlink() or not path.is_file():
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
            os.chmod(temporary, 0o600, follow_symlinks=False)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_runtime_public_key(self, name: str, public_key: str) -> Path:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{name}.ssh-key.",
            dir=self.registry_dir,
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(public_key)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600, follow_symlinks=False)
            return temporary
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _delete_registry(self, name: str) -> None:
        self._registry_path(name).unlink(missing_ok=True)

    def _vm_available(self) -> bool:
        command = Path(self.config.vm_command)
        return command.is_file() and os.access(command, os.X_OK)

    def _require_vm_backend(self) -> None:
        if not self._vm_available():
            raise RuntimeError(
                f"bhyve backend is unavailable: {self.config.vm_command}; install vm-bhyve"
            )

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
        self._require_vm_backend()
        result = self._run(
            [self.config.vm_command, "switch", "info", self.config.vm_switch],
            check=False,
        )
        if result.returncode != 0:
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
        # NOTE: Do NOT set "vm switch private ... on" here.
        # The PRIVATE bridge member flag blocks the FreeBSD host itself from
        # forwarding packets to VM tap ports, making SSH from host→guest
        # impossible (ping: sendto: Permission denied). VM-to-VM isolation
        # is enforced at L3 by the PF anchor (block in on labbridge0), so
        # PRIVATE is redundant and breaks the SSH transport.

    def _allocate(self, name: str) -> str:
        address = self.pool.allocate(name)
        if address == self.config.host_address:
            self.pool.release(address, name)
            raise RuntimeError("Address pool contains the host bridge address")
        return address

    def _jail_dataset(self, name: str) -> str:
        return f"{self.config.jail_dataset_parent.rstrip('/')}/{name}"

    def _reconcile_registered_before_create(self, name: str, owner: RuntimeOwner) -> None:
        record = self._load_registry(name)
        if record is None:
            return
        if not self._record_owned_by(record, owner):
            raise RuntimeError(f"Runtime already registered: {name}")
        self.destroy(name, requester_uid=owner.uid)
        if self._load_registry(name) is not None:
            raise RuntimeError(f"Runtime cleanup incomplete before recreate: {name}")

    def _reconcile_orphaned_jail_before_create(self, name: str, dataset: str) -> None:
        if self._load_registry(name) is not None:
            return
        if not self._jail_exists(name) and not self._dataset_exists(dataset):
            return
        self.destroy(name, force=True)
        remaining: list[str] = []
        if self._jail_exists(name):
            remaining.append(f"jail:{name}")
        if self._dataset_exists(dataset):
            remaining.append(f"dataset:{dataset}")
        if remaining:
            raise RuntimeError(
                f"Unable to reconcile orphaned runtime {name}: {', '.join(remaining)}"
            )

    def _configure_jail_loopback(self, name: str) -> None:
        self._run(
            ["jexec", name, "ifconfig", "lo0", "inet", "127.0.0.1/8", "up"]
        )

    def _install_jail_authorized_key(self, jail_root: str, public_key: str) -> None:
        user = self._run(
            [
                "pw",
                "-R",
                jail_root,
                "usershow",
                "-n",
                self.config.jail_ssh_user,
                "-7",
            ]
        ).stdout.strip()
        fields = user.split(":")
        if len(fields) != 7:
            raise RuntimeError(f"Unable to resolve jail SSH user: {self.config.jail_ssh_user}")
        uid = int(fields[2])
        gid = int(fields[3])
        home = fields[5]
        if not home.startswith("/") or home == "/":
            raise RuntimeError("Jail SSH user must have a dedicated absolute home directory")

        home_path = (Path(jail_root) / home.lstrip("/")).resolve()
        jail_root_path = Path(jail_root).resolve()
        if jail_root_path not in home_path.parents and home_path != jail_root_path:
            raise RuntimeError("Jail SSH home escapes the jail root")
        ssh_dir = home_path / ".ssh"
        if ssh_dir.is_symlink():
            ssh_dir.unlink()
        ssh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(ssh_dir, 0o700, follow_symlinks=False)
        os.chown(ssh_dir, uid, gid, follow_symlinks=False)
        authorized_keys = ssh_dir / "authorized_keys"
        if authorized_keys.is_symlink():
            authorized_keys.unlink()
        authorized_keys.write_text(public_key + "\n", encoding="utf-8")
        os.chmod(authorized_keys, 0o600, follow_symlinks=False)
        os.chown(authorized_keys, uid, gid, follow_symlinks=False)

    @_serialized_lifecycle
    def create_jail(
        self,
        name: str,
        owner_pid: int,
        peer: PeerCredentials,
        ssh_public_key: str,
    ) -> dict[str, Any]:
        name = self.validate_name(name)
        public_key = self.validate_ssh_public_key(ssh_public_key)
        owner = self._owner_from_peer(peer, owner_pid)
        self._reconcile_registered_before_create(name, owner)

        dataset = self._jail_dataset(name)
        self._reconcile_orphaned_jail_before_create(name, dataset)
        self._ensure_bridge()
        jail_root = str((Path(self.config.jail_mount_root) / name).resolve())
        address = self._allocate(name)
        self._run(["arp", "-d", address], check=False)
        record: dict[str, Any] = {
            "schema": "softcloud.runtime/v1",
            "name": name,
            "type": "jail",
            **owner.registry_fields(),
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
            self._install_jail_authorized_key(jail_root, public_key)

            epair_result = self._run(["ifconfig", "epair", "create"])
            epair_host = epair_result.stdout.strip().splitlines()[-1].strip()
            if not epair_host.endswith("a"):
                raise RuntimeError(f"Unexpected epair interface name: {epair_host}")
            epair_guest = f"{epair_host[:-1]}b"
            record["epair_host"] = epair_host
            record["epair_guest"] = epair_guest
            self._write_registry(record)

            self._run(["ifconfig", self.config.bridge_name, "addm", epair_host])
            # NOTE: Do NOT set private on the epair host interface.
            # Same reason as for bhyve taps: PRIVATE blocks the FreeBSD host
            # from sending packets to the epair, making SSH into jails
            # impossible. VM/jail-to-VM/jail isolation is enforced by PF.
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
            self._configure_jail_loopback(name)
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
            self.destroy(name, force=True)
            raise

    @_serialized_lifecycle
    def create_bhyve(
        self,
        name: str,
        owner_pid: int,
        peer: PeerCredentials,
        ssh_public_key: str,
        profile: str = "freebsd-python",
    ) -> dict[str, Any]:
        name = self.validate_name(name)
        public_key = self.validate_ssh_public_key(ssh_public_key)
        if isinstance(profile, bool) or not isinstance(profile, str) or profile not in BHYVE_PROFILES:
            raise ValueError(
                f"Unknown bhyve guest profile: {profile!r}. Supported profiles: {sorted(BHYVE_PROFILES.keys())}"
            )
        guest_profile = BHYVE_PROFILES[profile]
        owner = self._owner_from_peer(peer, owner_pid)
        self._require_vm_backend()
        self._reconcile_registered_before_create(name, owner)

        self._ensure_bridge()
        self._ensure_vm_switch()
        if self._run([self.config.vm_command, "info", name], check=False).returncode == 0:
            raise RuntimeError(f"Refusing to replace existing vm-bhyve guest: {name}")

        address = self._allocate(name)
        self._run(["arp", "-d", address], check=False)
        record: dict[str, Any] = {
            "schema": "softcloud.runtime/v1",
            "name": name,
            "type": "bhyve",
            "profile": profile,
            "guest_os": guest_profile.guest_os,
            **owner.registry_fields(),
            "guest_ip": address,
            "bridge": self.config.bridge_name,
            "disk_backend": self.config.vm_disk_backend,
            "md_unit": None,
            "dataset": None,
            "vm_created": False,
        }
        self._write_registry(record)
        temporary_public_key: Path | None = None

        try:
            temporary_public_key = self._write_runtime_public_key(name, public_key)
            netconfig = ";".join(
                [
                    f"interface={guest_profile.interface}",
                    f"ip={address}/{self.prefix_len}",
                    f"hostname={name}",
                ]
            )
            backend = self.config.vm_disk_backend
            if backend == "zvol-clone" and self._snapshot_exists(guest_profile.zvol_snapshot):
                vm_dataset = f"{self.config.vm_dataset_parent}/{name}"
                record["dataset"] = vm_dataset
                self._write_registry(record)
                self._run(
                    [
                        self.config.vm_command,
                        "create",
                        "-t",
                        guest_profile.template,
                        "-C",
                        "-k",
                        str(temporary_public_key),
                        "-n",
                        netconfig,
                        name,
                    ]
                )
                if self._dataset_exists(f"{vm_dataset}/disk0"):
                    self._run(["zfs", "destroy", "-f", f"{vm_dataset}/disk0"])
                self._run(["zfs", "clone", guest_profile.zvol_snapshot, f"{vm_dataset}/disk0"])

            elif backend == "memdisk":
                md_unit = self._create_memdisk(name, guest_profile.zvol_snapshot, guest_profile.image)
                record["md_unit"] = md_unit
                self._write_registry(record)
                self._run(
                    [
                        self.config.vm_command,
                        "create",
                        "-t",
                        guest_profile.memdisk_template,
                        "-C",
                        "-k",
                        str(temporary_public_key),
                        "-n",
                        netconfig,
                        name,
                    ]
                )
                self._configure_vm_memdisk(name, md_unit)
            else:
                self._run(
                    [
                        self.config.vm_command,
                        "create",
                        "-t",
                        guest_profile.template,
                        "-i",
                        guest_profile.image,
                        "-C",
                        "-k",
                        str(temporary_public_key),
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
                "profile": profile,
                "guest_os": guest_profile.guest_os,
                "guest_ip": address,
                "network_cidr": self.config.network_cidr,
                "bridge": self.config.bridge_name,
                "interface": guest_profile.interface,
            }
        except Exception:
            self.destroy(name, force=True)
            raise
        finally:
            if temporary_public_key is not None:
                temporary_public_key.unlink(missing_ok=True)

    def _snapshot_exists(self, snapshot: str) -> bool:
        if not snapshot or "@" not in snapshot:
            return False
        return (
            self._run(
                ["zfs", "list", "-H", "-o", "name", "-t", "snapshot", snapshot],
                check=False,
            ).returncode
            == 0
        )

    def _create_memdisk(
        self,
        name: str,
        snapshot: str | None = None,
        image: str | None = None,
    ) -> str:
        snapshot = snapshot or self.config.vm_zvol_snapshot
        image = image or self.config.vm_image
        result = self._run(
            ["mdconfig", "-a", "-t", self.config.vm_memdisk_type, "-s", self.config.vm_disk_size]
        )
        output = result.stdout.strip()
        match = re.search(r"(?:^|/dev/)?(md\d+)$", output)
        if not match:
            raise RuntimeError(f"Unexpected mdconfig output: {output}")
        md_unit = match.group(1)

        if self._snapshot_exists(snapshot):
            tmp_src = f"{self.config.vm_zvol_parent}/{name}-src"
            if self._dataset_exists(tmp_src):
                self._run(["zfs", "destroy", "-r", "-f", tmp_src], check=False)
            self._run(["zfs", "clone", snapshot, tmp_src])
            try:
                self._run(["dd", f"if=/dev/zvol/{tmp_src}", f"of=/dev/{md_unit}", "bs=1M", "status=none"])
            finally:
                self._run(["zfs", "destroy", "-r", "-f", tmp_src], check=False)
        elif Path(image).is_file():
            self._run(["dd", f"if={image}", f"of=/dev/{md_unit}", "bs=1M", "status=none"])
        return md_unit

    def _configure_vm_memdisk(self, name: str, md_unit: str) -> None:
        self._run([self.config.vm_command, "set", name, f"disk0_name=/dev/{md_unit}"], check=False)
        self._run([self.config.vm_command, "set", name, "disk0_dev=custom"], check=False)

    def _md_exists(self, md_unit: str) -> bool:
        if not isinstance(md_unit, str) or not re.fullmatch(r"md\d+", md_unit):
            return False
        result = self._run(["mdconfig", "-l", "-u", md_unit[2:]], check=False)
        return result.returncode == 0

    def _jail_exists(self, name: str) -> bool:
        return self._run(["jls", "-j", name, "name"], check=False).returncode == 0

    def _vm_exists(self, name: str) -> bool:
        if not self._vm_available():
            return False
        return self._run([self.config.vm_command, "info", name], check=False).returncode == 0

    def _dataset_exists(self, dataset: str) -> bool:
        return self._run(["zfs", "list", "-H", "-o", "name", dataset], check=False).returncode == 0

    def _interface_exists(self, interface: str) -> bool:
        return self._run(["ifconfig", interface], check=False).returncode == 0

    @_serialized_lifecycle
    def destroy(
        self,
        name: str,
        *,
        requester_uid: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        name = self.validate_name(name)
        record = self._load_registry(name) or {}
        if not force:
            self._authorize_record(record, requester_uid)
        runtime_type = record.get("type")
        removed: list[str] = []
        if runtime_type == "bhyve":
            self._require_vm_backend()
        if runtime_type == "bhyve" or self._vm_exists(name):
            self._run([self.config.vm_command, "poweroff", "-f", name], check=False, timeout=20)
            for _ in range(5):
                result = self._run(
                    [self.config.vm_command, "destroy", "-f", name], check=False, timeout=60
                )
                if result.returncode == 0:
                    removed.append("bhyve")
                    break
                time.sleep(0.2)

        md_unit = record.get("md_unit")
        if isinstance(md_unit, str) and re.fullmatch(r"md\d+", md_unit):
            unit_num = md_unit[2:]
            for _ in range(5):
                result = self._run(["mdconfig", "-d", "-u", unit_num], check=False)
                if result.returncode == 0:
                    removed.append(md_unit)
                    break
                time.sleep(0.2)

        vm_dataset = record.get("dataset")
        if isinstance(vm_dataset, str) and self._dataset_exists(vm_dataset):
            for _ in range(10):
                result = self._run(
                    ["zfs", "destroy", "-r", "-f", vm_dataset], check=False, timeout=60
                )
                if result.returncode == 0:
                    removed.append(vm_dataset)
                    break
                time.sleep(0.2)

        if runtime_type == "jail" or self._jail_exists(name):
            for _ in range(5):
                result = self._run(["jail", "-r", name], check=False, timeout=30)
                if result.returncode == 0:
                    removed.append("jail")
                    break
                time.sleep(0.2)

        epair_host = record.get("epair_host")
        if isinstance(epair_host, str) and re.fullmatch(r"epair\d+a", epair_host):
            for _ in range(5):
                result = self._run(["ifconfig", epair_host, "destroy"], check=False)
                if result.returncode == 0:
                    removed.append(epair_host)
                    break
                time.sleep(0.2)

        dataset = self._jail_dataset(name)
        if self._dataset_exists(dataset):
            for _ in range(10):
                result = self._run(
                    ["zfs", "destroy", "-r", "-f", dataset], check=False, timeout=60
                )
                if result.returncode == 0:
                    removed.append(dataset)
                    break
                time.sleep(0.2)

        remaining: list[str] = []
        if self._vm_exists(name):
            remaining.append("bhyve")
        if self._jail_exists(name):
            remaining.append("jail")
        if (
            isinstance(epair_host, str)
            and re.fullmatch(r"epair\d+a", epair_host)
            and self._interface_exists(epair_host)
        ):
            remaining.append(epair_host)
        if isinstance(md_unit, str) and re.fullmatch(r"md\d+", md_unit) and self._md_exists(md_unit):
            remaining.append(md_unit)
        if isinstance(vm_dataset, str) and self._dataset_exists(vm_dataset):
            remaining.append(vm_dataset)
        if self._dataset_exists(dataset):
            remaining.append(dataset)
        if remaining:
            raise RuntimeError(
                f"Runtime cleanup incomplete for {name}: {', '.join(remaining)}"
            )


        guest_ip = record.get("guest_ip")
        if isinstance(guest_ip, str) and guest_ip:
            self.pool.release(guest_ip, name)
            self._run(["arp", "-d", guest_ip], check=False)
        self._delete_registry(name)
        return {"name": name, "removed": removed}

    def _registered(self) -> dict[str, dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        for path in sorted(self.registry_dir.glob(f"{RUNTIME_PREFIX}*.json")):
            if path.is_symlink():
                continue
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
        if not self._vm_available():
            return set()
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

    @_serialized_lifecycle
    def gc(
        self,
        *,
        stale_only: bool = True,
        requester_uid: int = 0,
    ) -> dict[str, Any]:
        if (
            isinstance(requester_uid, bool)
            or not isinstance(requester_uid, int)
            or requester_uid < 0
        ):
            raise PermissionError("Invalid requester UID")
        if not isinstance(stale_only, bool):
            raise ValueError("stale_only must be boolean")
        registered = self._registered()
        retained: set[str] = set()
        cleaned: list[str] = []
        errors: dict[str, str] = {}

        for name, record in registered.items():
            if requester_uid != 0 and record.get("owner_uid") != requester_uid:
                retained.add(name)
                continue
            alive = self._owner_alive(record)
            if stale_only and alive:
                retained.add(name)
                continue
            try:
                self.destroy(
                    name,
                    requester_uid=requester_uid,
                    force=requester_uid == 0,
                )
                cleaned.append(name)
            except Exception as error:
                retained.add(name)
                errors[name] = str(error)

        registered_after = self._registered()
        retained.update(registered_after)

        if requester_uid == 0:
            discovered = self._discover_jails() | self._discover_vms()
            discovered.update(dataset.rsplit("/", 1)[-1] for dataset in self._discover_datasets())
            for name in sorted(discovered - retained):
                try:
                    self.destroy(name, force=True)
                    cleaned.append(name)
                except Exception as error:
                    errors[name] = str(error)

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
        else:
            removed_epairs = []

        active_owners = set(self._registered())
        released_addresses = (
            self.pool.clear_orphans(active_owners) if requester_uid == 0 else []
        )
        for address in released_addresses:
            self._run(["arp", "-d", address], check=False)
        return {
            "cleaned": sorted(set(cleaned)),
            "retained": sorted(active_owners),
            "removed_epairs": removed_epairs,
            "released_addresses": released_addresses,
            "errors": errors,
            "stale_only": stale_only,
        }


class RuntimeRequestHandler(socketserver.StreamRequestHandler):
    manager: RuntimeManager

    def handle(self) -> None:
        try:
            peer = freebsd_peer_credentials(self.request)
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw:
                return
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("request exceeded size limit")
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be an object")
            result = self._dispatch(request, peer)
            self._reply({"ok": True, "result": result})
        except Exception as error:
            self._reply({"ok": False, "error": str(error)})

    def _dispatch(
        self,
        request: dict[str, Any],
        peer: PeerCredentials,
    ) -> dict[str, Any]:
        action = request.get("action")
        if action == "ping":
            return {
                "service": "freebsd-laboratory-runtime",
                "version": 4,
                "capabilities": ["jail", "bhyve.freebsd", "bhyve.linux"],
                "bhyve_profiles": sorted(BHYVE_PROFILES.keys()),
            }
        if action == "create-jail":
            name = request.get("name")
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            owner_pid = request.get("owner_pid")
            if isinstance(owner_pid, bool) or not isinstance(owner_pid, int):
                raise ValueError("owner_pid must be an integer process id")
            ssh_public_key = request.get("ssh_public_key")
            if not isinstance(ssh_public_key, str):
                raise ValueError("ssh_public_key must be a string")
            return self.manager.create_jail(
                name,
                owner_pid,
                peer,
                ssh_public_key,
            )
        if action == "create-bhyve":
            name = request.get("name")
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            owner_pid = request.get("owner_pid")
            if isinstance(owner_pid, bool) or not isinstance(owner_pid, int):
                raise ValueError("owner_pid must be an integer process id")
            ssh_public_key = request.get("ssh_public_key")
            if not isinstance(ssh_public_key, str):
                raise ValueError("ssh_public_key must be a string")
            profile = request.get("profile", "freebsd-python")
            if isinstance(profile, bool) or not isinstance(profile, str):
                raise ValueError("profile must be a string")
            return self.manager.create_bhyve(
                name,
                owner_pid,
                peer,
                ssh_public_key,
                profile=profile,
            )
        if action == "destroy":
            name = request.get("name")
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            return self.manager.destroy(
                name,
                requester_uid=peer.uid,
            )
        if action == "gc":
            stale_only = request.get("stale_only", True)
            if not isinstance(stale_only, bool):
                raise ValueError("stale_only must be boolean")
            return self.manager.gc(stale_only=stale_only, requester_uid=peer.uid)
        raise ValueError("unsupported runtime action")

    def _reply(self, response: dict[str, Any]) -> None:
        try:
            self.wfile.write(
                json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            )
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 32


def _configure_socket(path: Path, group_name: str) -> None:
    """Grant the configured Jupyter group access to the root daemon socket.

    Mode 0660 is intentional: the unprivileged Jupyter service must connect as
    a member of the configured group. Filesystem access only permits a
    connection; RuntimeRequestHandler authorizes every request using kernel
    supplied LOCAL_PEERCRED before any privileged action is performed.
    """

    try:
        group = grp.getgrnam(group_name)
    except KeyError as error:
        raise RuntimeError(f"Runtime socket group does not exist: {group_name}") from error
    os.chown(path, 0, group.gr_gid, follow_symlinks=False)
    os.chmod(path, 0o660, follow_symlinks=False)


def _prepare_socket_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink():
        path.unlink()
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
    parser.add_argument("--jail-ssh-user", default="freebsd")
    parser.add_argument("--vm-command", default="/usr/local/sbin/vm")
    parser.add_argument("--vm-template", default="freebsd-lab")
    parser.add_argument("--vm-memdisk-template", default="freebsd-lab-memdisk")
    parser.add_argument("--vm-image", default="freebsd-python.raw")
    parser.add_argument("--vm-switch", default="freebsdlab")
    parser.add_argument(
        "--vm-disk-backend",
        default="zvol-clone",
        choices=["zvol-clone", "memdisk", "legacy"],
    )
    parser.add_argument(
        "--vm-zvol-snapshot",
        default="zroot/vm/.zvol/freebsd-python@ready",
    )
    parser.add_argument("--vm-zvol-parent", default="zroot/vm/.zvol")
    parser.add_argument("--vm-dataset-parent", default="zroot/vm")
    parser.add_argument("--vm-disk-size", default="8G")
    parser.add_argument(
        "--vm-memdisk-type",
        default="swap",
        choices=["swap", "malloc"],
    )
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
        jail_ssh_user=args.jail_ssh_user,
        vm_command=args.vm_command,
        vm_template=args.vm_template,
        vm_memdisk_template=args.vm_memdisk_template,
        vm_image=args.vm_image,
        vm_switch=args.vm_switch,
        vm_disk_backend=args.vm_disk_backend,
        vm_zvol_snapshot=args.vm_zvol_snapshot,
        vm_zvol_parent=args.vm_zvol_parent,
        vm_dataset_parent=args.vm_dataset_parent,
        vm_disk_size=args.vm_disk_size,
        vm_memdisk_type=args.vm_memdisk_type,
        ssh_public_key=args.ssh_public_key,
    )
    manager = RuntimeManager(config)
    socket_path = Path(config.socket_path)
    _prepare_socket_path(socket_path)

    handler_type = type("BoundRuntimeRequestHandler", (RuntimeRequestHandler,), {"manager": manager})
    with ThreadingUnixServer(str(socket_path), handler_type) as server:
        _configure_socket(socket_path, config.socket_group)
        if not args.no_reconcile:
            result = manager.gc(stale_only=True, requester_uid=0)
            for name, detail in result.get("errors", {}).items():
                print(
                    f"freebsd-lab-runtime-daemon: reconciliation failed for {name}: {detail}",
                    file=sys.stderr,
                )
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
