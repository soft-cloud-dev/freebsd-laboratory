from pathlib import Path

import pytest

from freebsd_laboratory.bhyve import FreeBSDBhyveProvisioner
from freebsd_laboratory.remote_kernel import remote_kernel_command


def test_bhyve_keeps_longer_startup_timeout() -> None:
    assert FreeBSDBhyveProvisioner.startup_timeout.default_value == 90


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
