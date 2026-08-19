from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, NoReturn

from .telemetry import capture_exception, init_sentry


DEFAULT_RUNTIME_SOCKET = "/var/run/freebsd-laboratory/runtime.sock"
MAX_RESPONSE_BYTES = 1024 * 1024


class RuntimeControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeClient:
    socket_path: str = DEFAULT_RUNTIME_SOCKET
    timeout: float = 30.0

    @staticmethod
    def _raise_control_error(
        action: str,
        message: str,
        cause: BaseException | None = None,
    ) -> NoReturn:
        try:
            if cause is None:
                raise RuntimeControlError(message)
            raise RuntimeControlError(message) from cause
        except RuntimeControlError as error:
            capture_exception(
                error,
                component="runtime-client",
                operation=f"runtime:{action}",
            )
            raise

    def request(self, action: str, **payload: Any) -> dict[str, Any]:
        init_sentry("runtime-client")
        request = {"action": action, **payload}
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            try:
                client.connect(self.socket_path)
                client.sendall(encoded)
                response = self._read_line(client)
            except RuntimeControlError as error:
                capture_exception(
                    error,
                    component="runtime-client",
                    operation=f"runtime:{action}",
                )
                raise
            except (OSError, TimeoutError) as error:
                self._raise_control_error(
                    action,
                    f"Runtime daemon unavailable at {self.socket_path}: {error}",
                    error,
                )

        try:
            document = json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._raise_control_error(action, "Runtime daemon returned invalid JSON", error)

        if not isinstance(document, dict):
            self._raise_control_error(action, "Runtime daemon returned a non-object response")
        if document.get("ok") is not True:
            detail = document.get("error") or "runtime operation failed"
            self._raise_control_error(action, str(detail))
        result = document.get("result", {})
        if not isinstance(result, dict):
            self._raise_control_error(action, "Runtime daemon returned an invalid result")
        return result

    @staticmethod
    def _read_line(client: socket.socket) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            newline = chunk.find(b"\n")
            if newline >= 0:
                chunk = chunk[:newline]
                chunks.append(chunk)
                size += len(chunk)
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise RuntimeControlError("Runtime daemon response exceeded size limit")
        if size > MAX_RESPONSE_BYTES:
            raise RuntimeControlError("Runtime daemon response exceeded size limit")
        if not chunks:
            raise RuntimeControlError("Runtime daemon closed the connection without a response")
        return b"".join(chunks)

    def ping(self) -> dict[str, Any]:
        return self.request("ping")

    def create_jail(
        self,
        name: str,
        owner_pid: int,
        ssh_public_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "create-jail",
            name=name,
            owner_pid=owner_pid,
            ssh_public_key=ssh_public_key,
        )

    def create_bhyve(
        self,
        name: str,
        owner_pid: int,
        ssh_public_key: str,
    ) -> dict[str, Any]:
        return self.request(
            "create-bhyve",
            name=name,
            owner_pid=owner_pid,
            ssh_public_key=ssh_public_key,
        )

    def destroy(self, name: str) -> dict[str, Any]:
        return self.request("destroy", name=name)

    def gc(self, *, stale_only: bool = True) -> dict[str, Any]:
        return self.request("gc", stale_only=stale_only)
