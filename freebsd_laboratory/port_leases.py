from __future__ import annotations

import hashlib
import os
import socket
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

from .process_identity import process_matches, query_process_identity

try:
    import fcntl
except ImportError:  # pragma: no cover - FreeBSD/Linux provide fcntl
    fcntl = None  # type: ignore[assignment]


_LOCAL_PORT_THREAD_LOCK = threading.Lock()


@dataclass
class LocalPortReservation:
    pool: "LocalPortLeasePool"
    owner: str
    pid: int
    uid: int
    process_digest: str
    ports: tuple[int, ...]
    _sockets: list[socket.socket] = field(default_factory=list, repr=False)

    def release_reservations(self) -> None:
        """Release pre-launch bind reservations while retaining lease ownership."""
        sockets, self._sockets = self._sockets, []
        for reserved_socket in sockets:
            try:
                reserved_socket.close()
            except OSError:
                pass

    def release(self) -> None:
        self.release_reservations()
        self.pool.release(
            self.ports,
            self.owner,
            self.pid,
            self.uid,
            self.process_digest,
        )


class LocalPortLeasePool:
    """Cross-process lease pool for host-side SSH forwarding ports.

    A lease filename carries a PID plus a hash of the process UID/start time.
    This prevents a recycled PID from keeping a stale port lease authoritative.
    """

    def __init__(
        self,
        start: int,
        end: int,
        directory: Path | str,
        bind_address: str = "127.0.0.1",
    ) -> None:
        if isinstance(start, bool) or isinstance(end, bool):
            raise ValueError("Tunnel port range must contain integer TCP ports")
        if not 1024 <= start <= 65535 or not 1024 <= end <= 65535 or start > end:
            raise ValueError("Invalid tunnel port range")
        self.start = start
        self.end = end
        self.directory = Path(directory)
        self.bind_address = bind_address

    @property
    def capacity(self) -> int:
        return self.end - self.start + 1

    @staticmethod
    def _nofollow_flag() -> int:
        return int(getattr(os, "O_NOFOLLOW", 0))

    @staticmethod
    def _directory_flag() -> int:
        return int(getattr(os, "O_DIRECTORY", 0))

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if fcntl is None:
            raise RuntimeError("Tunnel port leasing requires fcntl/flock support")

        directory_flags = os.O_RDONLY | self._directory_flag() | self._nofollow_flag()
        try:
            directory_fd = os.open(self.directory, directory_flags)
        except OSError as error:
            raise RuntimeError(
                f"Tunnel lease directory must be provisioned by the host service: "
                f"{self.directory}"
            ) from error

        try:
            try:
                lock_fd = os.open(
                    ".lock",
                    os.O_RDWR | self._nofollow_flag(),
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise RuntimeError(
                    f"Tunnel lease lock must be provisioned by the host service: "
                    f"{self.directory / '.lock'}"
                ) from error

            try:
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    raise RuntimeError("Tunnel lease lock is not a regular file")
                with _LOCAL_PORT_THREAD_LOCK:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _owner_digest(owner: str) -> str:
        return hashlib.sha256(owner.encode("utf-8")).hexdigest()

    def _lease_path(
        self,
        port: int,
        owner: str,
        pid: int,
        uid: int,
        process_digest: str,
    ) -> Path:
        return self.directory / (
            f"{port}.{pid}.{uid}.{process_digest}."
            f"{self._owner_digest(owner)}.lease"
        )

    def _lease_paths(self, port: int) -> list[Path]:
        return list(self.directory.glob(f"{port}.*.lease"))

    @staticmethod
    def _lease_identity(path: Path, port: int) -> tuple[int, int, str] | None:
        parts = path.name.split(".")
        if len(parts) != 6 or parts[0] != str(port) or parts[5] != "lease":
            return None
        try:
            pid = int(parts[1])
            uid = int(parts[2])
        except ValueError:
            return None
        process_digest = parts[3]
        owner_digest = parts[4]
        if pid <= 1 or uid < 0:
            return None
        for digest in (process_digest, owner_digest):
            if len(digest) != 64:
                return None
            try:
                int(digest, 16)
            except ValueError:
                return None
        return pid, uid, process_digest

    def _write_lease(self, path: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | self._nofollow_flag()
        lease_fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(lease_fd, 0o600)
        finally:
            os.close(lease_fd)

    def _reserve_socket(self, port: int) -> socket.socket | None:
        reserved_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            reserved_socket.bind((self.bind_address, port))
            reserved_socket.listen(1)
            return reserved_socket
        except OSError:
            reserved_socket.close()
            return None

    def _candidate_ports(self, owner: str) -> Iterator[int]:
        offset = int.from_bytes(
            hashlib.sha256(owner.encode("utf-8")).digest()[:8],
            "big",
        ) % self.capacity
        for index in range(self.capacity):
            yield self.start + ((offset + index) % self.capacity)

    def allocate(self, owner: str, pid: int, count: int) -> LocalPortReservation:
        if not owner:
            raise ValueError("Tunnel port lease owner is required")
        if pid != os.getpid():
            raise ValueError("Tunnel port lease PID must match the calling process")
        if count <= 0 or count > self.capacity:
            raise ValueError("Invalid tunnel port lease count")

        identity = query_process_identity(pid)
        if identity is None or identity.uid != os.getuid():
            raise RuntimeError("Unable to fingerprint tunnel port lease owner")

        sockets: list[socket.socket] = []
        ports: list[int] = []
        created_paths: list[Path] = []

        with self._locked():
            try:
                for port in self._candidate_ports(owner):
                    lease_paths = self._lease_paths(port)
                    if any(
                        identity_fields is not None
                        and process_matches(*identity_fields)
                        for path in lease_paths
                        if (identity_fields := self._lease_identity(path, port))
                        is not None
                    ):
                        continue

                    reserved_socket = self._reserve_socket(port)
                    if reserved_socket is None:
                        continue

                    try:
                        for stale_path in lease_paths:
                            stale_path.unlink(missing_ok=True)
                        lease_path = self._lease_path(
                            port,
                            owner,
                            pid,
                            identity.uid,
                            identity.digest,
                        )
                        self._write_lease(lease_path)
                    except Exception:
                        reserved_socket.close()
                        raise

                    created_paths.append(lease_path)
                    sockets.append(reserved_socket)
                    ports.append(port)
                    if len(ports) == count:
                        return LocalPortReservation(
                            pool=self,
                            owner=owner,
                            pid=pid,
                            uid=identity.uid,
                            process_digest=identity.digest,
                            ports=tuple(ports),
                            _sockets=sockets,
                        )
            except Exception:
                for reserved_socket in sockets:
                    reserved_socket.close()
                for lease_path in created_paths:
                    lease_path.unlink(missing_ok=True)
                raise

            for reserved_socket in sockets:
                reserved_socket.close()
            for lease_path in created_paths:
                lease_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Unable to reserve {count} SSH tunnel ports in {self.start}-{self.end}"
            )

    def release(
        self,
        ports: Sequence[int],
        owner: str,
        pid: int,
        uid: int,
        process_digest: str,
    ) -> None:
        if not ports or not owner or pid <= 1 or uid < 0 or not process_digest:
            return
        with self._locked():
            for port in ports:
                self._lease_path(
                    int(port),
                    owner,
                    pid,
                    uid,
                    process_digest,
                ).unlink(missing_ok=True)
