from pathlib import Path

import pytest

from freebsd_laboratory.bhyve import (
    IPv4LeasePool,
    build_netconfig,
    remote_kernel_command,
)


def test_build_netconfig_uses_declared_private_network() -> None:
    assert build_netconfig(
        "vtnet0",
        "172.31.254.100",
        "172.31.254.0/24",
        "freebsd-lab-abc",
    ) == (
        "interface=vtnet0;ip=172.31.254.100/24;hostname=freebsd-lab-abc"
    )


def test_build_netconfig_rejects_address_outside_network() -> None:
    with pytest.raises(ValueError):
        build_netconfig(
            "vtnet0",
            "192.0.2.10",
            "172.31.254.0/24",
            "freebsd-lab-abc",
        )


def test_remote_kernel_command_rebinds_connection_file() -> None:
    host_path = Path("/var/run/jupyter/kernel-abc.json")
    assert remote_kernel_command(
        [
            "/usr/local/bin/python3",
            "-m",
            "ipykernel_launcher",
            "-f",
            str(host_path),
        ],
        host_path,
        "/tmp/freebsd-laboratory/kernel-abc.json",
    ) == [
        "/usr/local/bin/python3",
        "-m",
        "ipykernel_launcher",
        "-f",
        "/tmp/freebsd-laboratory/kernel-abc.json",
    ]


def test_remote_kernel_command_requires_connection_file() -> None:
    with pytest.raises(RuntimeError):
        remote_kernel_command(
            ["/usr/local/bin/python3", "-m", "ipykernel_launcher"],
            Path("/var/run/jupyter/kernel-abc.json"),
            "/tmp/freebsd-laboratory/kernel-abc.json",
        )


def test_address_pool_prevents_concurrent_reuse(tmp_path: Path) -> None:
    pool = IPv4LeasePool(
        "172.31.254.0/24",
        "172.31.254.100",
        "172.31.254.101",
        tmp_path,
    )

    first = pool.allocate("vm-a")
    second = pool.allocate("vm-b")

    assert first == "172.31.254.100"
    assert second == "172.31.254.101"
    with pytest.raises(RuntimeError):
        pool.allocate("vm-c")

    assert pool.release(first, "vm-b") is False
    assert pool.release(first, "vm-a") is True
    assert pool.allocate("vm-c") == first
