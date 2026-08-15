from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - FreeBSD/Linux provide fcntl
    fcntl = None  # type: ignore[assignment]


CONNECTION_PORT_FIELDS = (
    "shell_port",
    "iopub_port",
    "stdin_port",
    "control_port",
    "hb_port",
)

_LOCAL_PORT_THREAD_LOCK = threading.Lock()


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


def connection_ports(document: dict[str, Any]) -> tuple[int, ...]:
    ports: list[int] = []
    for field_name in CONNECTION_PORT_FIELDS:
        value = document.get(field_name)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValueError(f"Invalid Jupyter connection port: {field_name}")
        ports.append(value)
    if len(set(ports)) != len(ports):
        raise ValueError("Jupyter connection ports must be unique")
    return tuple(ports)


def _validate_port_sequence(ports: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(ports)
    if len(normalized) != len(CONNECTION_PORT_FIELDS):
        raise ValueError(
            f"Expected {len(CONNECTION_PORT_FIELDS)} Jupyter connection ports"
        )
    document = dict(zip(CONNECTION_PORT_FIELDS, normalized, strict=True))
    return connection_ports(document)


def rewrite_connection_file(
    parent: Any,
    bind_ip: str = "127.0.0.1",
    ports: Sequence[int] | None = None,
) -> tuple[Path, str, tuple[int, ...], tuple[int, ...]]:
    """Bind the Jupyter connection document to leased loopback tunnel ports.

    The original Jupyter ports are retained for cleanup only. When ``ports`` is
    supplied, the host manager and the staged runtime document are both rewritten
    to the same leased port set so every runtime gets a collision-free local SSH
    forwarding namespace without exposing ZMQ on the laboratory bridge.
    """

    connection_file = getattr(parent, "connection_file", None)
    if not connection_file:
        raise RuntimeError("Kernel manager connection file is unavailable")
    host_path = Path(connection_file).resolve()
    document = json.loads(host_path.read_text(encoding="utf-8"))
    if document.get("transport", "tcp") != "tcp":
        raise ValueError("SSH kernel transport requires Jupyter TCP connections")

    original_ports = connection_ports(document)
    tunnel_ports = original_ports if ports is None else _validate_port_sequence(ports)
    original_ip = str(document.get("ip", getattr(parent, "ip", "")))

    document["ip"] = bind_ip
    setattr(parent, "ip", bind_ip)
    for field_name, port in zip(CONNECTION_PORT_FIELDS, tunnel_ports, strict=True):
        document[field_name] = port
        setattr(parent, field_name, port)

    temporary = host_path.with_name(f".{host_path.name}.remote.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(host_path)
    return host_path, original_ip, original_ports, tunnel_ports


def restore_connection_file(
    parent: Any,
    original_ip: str | None,
    original_ports: Sequence[int] = (),
) -> None:
    normalized_ports: tuple[int, ...] = ()
    if original_ports:
        normalized_ports = _validate_port_sequence(original_ports)

    if original_ip is not None:
        setattr(parent, "ip", original_ip)
    for field_name, port in zip(CONNECTION_PORT_FIELDS, normalized_ports, strict=True):
        setattr(parent, field_name, port)

    connection_file = getattr(parent, "connection_file", None)
    if not connection_file:
        return
    path = Path(connection_file)
    if not path.is_file():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if original_ip is not None:
            document["ip"] = original_ip
        for field_name, port in zip(CONNECTION_PORT_FIELDS, normalized_ports, strict=True):
            document[field_name] = port
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
    except (OSError, ValueError, TypeError):
        return


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class LocalPortReservation:
    pool: "LocalPortLeasePool"
    owner: str
    pid: int
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
        self.pool.release(self.ports, self.owner)


class LocalPortLeasePool:
    """Cross-process lease pool for the five host-side SSH forwarding ports.

    Allocation is serialized with ``flock`` and each selected TCP port is bound on
    loopback until the provisioner calls ``launch_kernel``. Lease files remain in
    place while SSH owns the listeners, preventing concurrent laboratory sessions
    from selecting the same ports even across separate Jupyter server processes.
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

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if fcntl is None:
            raise RuntimeError("Tunnel port leasing requires fcntl/flock support")
        self.directory.mkdir(parents=True, exist_ok=True)
        lock_path = self.directory / ".lock"
        with _LOCAL_PORT_THREAD_LOCK:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o660)
            try:
                os.fchmod(lock_fd, 0o660)
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _lease_path(self, port: int) -> Path:
        return self.directory / f"{port}.lease"

    def _read_lease(self, path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

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
            hashlib.sha256(owner.encode("utf-8")).digest()[:8], "big"
        ) % self.capacity
        for index in range(self.capacity):
            yield self.start + ((offset + index) % self.capacity)

    def allocate(self, owner: str, pid: int, count: int) -> LocalPortReservation:
        if not owner:
            raise ValueError("Tunnel port lease owner is required")
        if pid <= 0:
            raise ValueError("Tunnel port lease PID must be positive")
        if count <= 0 or count > self.capacity:
            raise ValueError("Invalid tunnel port lease count")

        sockets: list[socket.socket] = []
        ports: list[int] = []
        created_paths: list[Path] = []

        with self._locked():
            try:
                for port in self._candidate_ports(owner):
                    lease_path = self._lease_path(port)
                    if lease_path.exists():
                        lease = self._read_lease(lease_path)
                        lease_pid = 0
                        if lease is not None:
                            try:
                                lease_pid = int(lease.get("pid", 0))
                            except (TypeError, ValueError):
                                lease_pid = 0
                        if lease_pid > 0 and _pid_is_alive(lease_pid):
                            continue
                        lease_path.unlink(missing_ok=True)

                    reserved_socket = self._reserve_socket(port)
                    if reserved_socket is None:
                        continue

                    lease_path.write_text(
                        json.dumps({"owner": owner, "pid": pid}) + "\n",
                        encoding="utf-8",
                    )
                    lease_path.chmod(0o660)
                    created_paths.append(lease_path)
                    sockets.append(reserved_socket)
                    ports.append(port)
                    if len(ports) == count:
                        return LocalPortReservation(
                            pool=self,
                            owner=owner,
                            pid=pid,
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

    def release(self, ports: Sequence[int], owner: str) -> None:
        if not ports or not owner:
            return
        with self._locked():
            for port in ports:
                lease_path = self._lease_path(int(port))
                if not lease_path.exists():
                    continue
                lease = self._read_lease(lease_path)
                if lease is not None and lease.get("owner") == owner:
                    lease_path.unlink(missing_ok=True)


@dataclass
class SSHTransport:
    host: str
    user: str
    private_key: str
    known_hosts_file: Path
    ssh_command: str = "/usr/bin/ssh"
    scp_command: str = "/usr/bin/scp"
    config_file: str = "/dev/null"
    connect_timeout: int = 5
    connection_attempts: int = 3
    server_alive_interval: int = 15
    server_alive_count_max: int = 4
    tcp_keep_alive: bool = True
    bind_address: str = "127.0.0.1"

    def assert_available(self) -> None:
        for command in (self.ssh_command, self.scp_command):
            if not executable_exists(command):
                raise RuntimeError(f"Required executable is unavailable: {command}")
        if not Path(self.private_key).is_file():
            raise RuntimeError(f"Required SSH private key is unavailable: {self.private_key}")
        if not Path(self.config_file).is_file():
            raise RuntimeError(f"SSH client configuration path is unavailable: {self.config_file}")

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def options(self) -> list[str]:
        return [
            "-F",
            self.config_file,
            "-i",
            self.private_key,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            f"ConnectionAttempts={self.connection_attempts}",
            "-o",
            f"ServerAliveInterval={self.server_alive_interval}",
            "-o",
            f"ServerAliveCountMax={self.server_alive_count_max}",
            "-o",
            f"TCPKeepAlive={'yes' if self.tcp_keep_alive else 'no'}",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"UserKnownHostsFile={self.known_hosts_file}",
        ]

    def command(
        self,
        remote_command: str,
        *,
        forward_ports: Sequence[int] = (),
    ) -> list[str]:
        command = [self.ssh_command, *self.options(), "-T"]
        for port in sorted(set(forward_ports)):
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("Invalid SSH forwarding port")
            command.extend(
                [
                    "-L",
                    f"{self.bind_address}:{port}:127.0.0.1:{port}",
                ]
            )
        command.extend([self.target, remote_command])
        return command

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
        probe_timeout = max(5, self.connect_timeout + 2)
        while time.monotonic() < deadline:
            result = self._run(self.command("true"), check=False, timeout=probe_timeout)
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
