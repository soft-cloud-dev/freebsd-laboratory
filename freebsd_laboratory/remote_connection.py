from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from typing import Any, Sequence


CONNECTION_PORT_FIELDS = (
    "shell_port",
    "iopub_port",
    "stdin_port",
    "control_port",
    "hb_port",
)


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
    if not isinstance(document, dict):
        raise ValueError("Connection document must be a dictionary")
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
    if isinstance(ports, (str, bytes)):
        raise ValueError(f"Expected {len(CONNECTION_PORT_FIELDS)} Jupyter connection ports")
    try:
        normalized = tuple(ports)
    except TypeError:
        raise ValueError(f"Expected {len(CONNECTION_PORT_FIELDS)} Jupyter connection ports")
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
    """Bind the Jupyter connection document to leased loopback tunnel ports."""

    try:
        ipaddress.ip_address(bind_ip)
    except ValueError as error:
        raise ValueError(f"Invalid bind_ip: {bind_ip}") from error

    connection_file = getattr(parent, "connection_file", None)
    if not connection_file:
        raise RuntimeError("Kernel manager connection file is unavailable")
    raw_path = Path(connection_file)
    if raw_path.is_symlink():
        raise RuntimeError(f"Connection file must not be a symbolic link: {raw_path}")
    host_path = raw_path.resolve()
    if not host_path.is_file():
        raise RuntimeError(f"Connection file is unavailable: {host_path}")
    try:
        document = json.loads(host_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid connection file JSON: {error}") from error
    if not isinstance(document, dict) or document.get("transport", "tcp") != "tcp":
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
    if temporary.is_symlink():
        temporary.unlink()
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600, follow_symlinks=False)
    temporary.replace(host_path)
    return host_path, original_ip, original_ports, tunnel_ports


def restore_connection_file(
    parent: Any,
    original_ip: str | None,
    original_ports: Sequence[int] = (),
) -> None:
    normalized_ports: tuple[int, ...] = ()
    if original_ports:
        try:
            normalized_ports = _validate_port_sequence(original_ports)
        except ValueError:
            normalized_ports = ()

    if original_ip is not None:
        setattr(parent, "ip", original_ip)
    if normalized_ports:
        for field_name, port in zip(CONNECTION_PORT_FIELDS, normalized_ports, strict=True):
            setattr(parent, field_name, port)

    connection_file = getattr(parent, "connection_file", None)
    if not connection_file:
        return
    raw_path = Path(connection_file)
    if raw_path.is_symlink():
        return
    path = raw_path.resolve()
    if not path.is_file():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            return
        if original_ip is not None:
            document["ip"] = original_ip
        if normalized_ports:
            for field_name, port in zip(
                CONNECTION_PORT_FIELDS,
                normalized_ports,
                strict=True,
            ):
                document[field_name] = port
        temporary = path.with_name(f".{path.name}.restore.tmp")
        if temporary.is_symlink():
            temporary.unlink()
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600, follow_symlinks=False)
        temporary.replace(path)
    except (OSError, ValueError, TypeError):
        return


def release_jupyter_cached_ports(provisioner: Any, ports: Sequence[int]) -> None:
    """Return superseded LocalProvisioner ports before installing tunnel leases."""
    if not getattr(provisioner, "ports_cached", False):
        return
    from jupyter_client.connect import LocalPortCache

    cache = LocalPortCache.instance()
    for port in ports:
        cache.return_port(int(port))
    provisioner.ports_cached = False
