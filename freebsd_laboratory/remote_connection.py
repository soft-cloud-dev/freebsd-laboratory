from __future__ import annotations

import json
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
    """Bind the Jupyter connection document to leased loopback tunnel ports."""

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
    if normalized_ports:
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
        if normalized_ports:
            for field_name, port in zip(
                CONNECTION_PORT_FIELDS,
                normalized_ports,
                strict=True,
            ):
                document[field_name] = port
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)
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
