from __future__ import annotations

import fcntl
import ipaddress
import os
from pathlib import Path


import threading

_IPV4_POOL_THREAD_LOCK = threading.Lock()


class IPv4LeasePool:
    """File-backed allocator shared by jail and bhyve runtimes."""

    def __init__(self, network: str, start: str, end: str, lease_dir: str | Path) -> None:
        self.network = ipaddress.ip_network(network, strict=False)
        if self.network.version != 4:
            raise ValueError("FreeBSD Laboratory private transport requires IPv4")
        self.start = ipaddress.ip_address(start)
        self.end = ipaddress.ip_address(end)
        if self.start not in self.network or self.end not in self.network:
            raise ValueError("Address pool must be contained in network_cidr")
        if int(self.start) > int(self.end):
            raise ValueError("address_start must not be greater than address_end")
        self.lease_dir = Path(lease_dir)

    def _lease_path(self, address: ipaddress.IPv4Address) -> Path:
        return self.lease_dir / f"{address}.lease"

    def _open_lock(self):
        lock_path = self.lease_dir / ".lock"
        if lock_path.is_symlink():
            lock_path.unlink(missing_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        return os.fdopen(descriptor, "a+", encoding="utf-8")

    def allocate(self, owner: str) -> str:
        if not isinstance(owner, str) or not owner:
            raise ValueError("Lease owner must be a non-empty string")
        self.lease_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _IPV4_POOL_THREAD_LOCK:
            with self._open_lock() as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    for value in range(int(self.start), int(self.end) + 1):
                        address = ipaddress.ip_address(value)
                        lease_path = self._lease_path(address)
                        flags = (
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | getattr(os, "O_NOFOLLOW", 0)
                        )
                        try:
                            descriptor = os.open(
                                lease_path,
                                flags,
                                0o600,
                            )
                        except FileExistsError:
                            continue
                        except OSError:
                            continue
                        try:
                            os.write(descriptor, f"{owner}\n".encode("utf-8"))
                        finally:
                            os.close(descriptor)
                        return str(address)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        raise RuntimeError("No free IPv4 addresses remain in the laboratory pool")

    def release(self, address: str, owner: str) -> bool:
        if not isinstance(owner, str) or not owner:
            return False
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        if parsed not in self.network or int(parsed) < int(self.start) or int(parsed) > int(self.end):
            return False
        self.lease_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _IPV4_POOL_THREAD_LOCK:
            with self._open_lock() as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    lease_path = self._lease_path(parsed)
                    if lease_path.is_symlink() or not lease_path.exists():
                        if lease_path.is_symlink():
                            lease_path.unlink(missing_ok=True)
                        return False
                    try:
                        recorded_owner = lease_path.read_text(encoding="utf-8").strip()
                    except OSError:
                        return False
                    if recorded_owner != owner:
                        return False
                    lease_path.unlink(missing_ok=True)
                    return True
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def clear_orphans(self, active_owners: set[str]) -> list[str]:
        if not self.lease_dir.exists():
            return []
        removed: list[str] = []
        with _IPV4_POOL_THREAD_LOCK:
            with self._open_lock() as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    for lease_path in self.lease_dir.glob("*.lease"):
                        if lease_path.is_symlink():
                            lease_path.unlink(missing_ok=True)
                            continue
                        try:
                            owner = lease_path.read_text(encoding="utf-8").strip()
                        except OSError:
                            continue
                        if owner in active_owners:
                            continue
                        lease_path.unlink(missing_ok=True)
                        removed.append(lease_path.stem)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return sorted(removed)


def prefix_length(network_cidr: str) -> int:
    return ipaddress.ip_network(network_cidr, strict=False).prefixlen


def address_in_network(address: str, network_cidr: str) -> bool:
    return ipaddress.ip_address(address) in ipaddress.ip_network(network_cidr, strict=False)
