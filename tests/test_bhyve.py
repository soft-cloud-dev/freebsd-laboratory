from pathlib import Path
from unittest.mock import Mock

import pytest

from freebsd_laboratory.bhyve import FreeBSDBhyveProvisioner, LinuxBhyveProvisioner
from freebsd_laboratory.remote_kernel import remote_kernel_command


def test_bhyve_keeps_longer_startup_timeout() -> None:
    assert FreeBSDBhyveProvisioner.startup_timeout.default_value == 90
    assert LinuxBhyveProvisioner.startup_timeout.default_value == 90


def test_linux_bhyve_provisioner_label() -> None:
    assert LinuxBhyveProvisioner.runtime_label == "Linux bhyve VM"


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


def test_remote_kernel_command_rebinds_linux_python_path() -> None:
    host_path = Path("/var/run/jupyter/kernel-linux.json")
    assert remote_kernel_command(
        [
            "/usr/bin/python3",
            "-m",
            "ipykernel_launcher",
            "-f",
            str(host_path),
        ],
        host_path,
        "/tmp/freebsd-laboratory/kernel-linux.json",
    ) == [
        "/usr/bin/python3",
        "-m",
        "ipykernel_launcher",
        "-f",
        "/tmp/freebsd-laboratory/kernel-linux.json",
    ]


def test_remote_kernel_command_requires_connection_file() -> None:
    with pytest.raises(RuntimeError):
        remote_kernel_command(
            ["/usr/local/bin/python3", "-m", "ipykernel_launcher"],
            Path("/var/run/jupyter/kernel-abc.json"),
            "/tmp/freebsd-laboratory/kernel-abc.json",
        )


def test_linux_bhyve_provisioner_requests_linux_profile() -> None:
    provisioner = LinuxBhyveProvisioner()
    client = Mock()
    client.ping.return_value = {
        "service": "freebsd-laboratory-runtime",
        "version": 4,
        "capabilities": ["jail", "bhyve.freebsd", "bhyve.linux"],
    }
    client.create_bhyve.return_value = {"guest_ip": "172.31.254.20"}
    provisioner._client = Mock(return_value=client)

    result = provisioner._request_create("freebsd-lab-1234", 42, "ssh-ed25519 AAA...")
    assert result == {"guest_ip": "172.31.254.20"}
    client.create_bhyve.assert_called_once_with(
        "freebsd-lab-1234",
        42,
        "ssh-ed25519 AAA...",
        profile="linux-python",
    )


def test_linux_bhyve_provisioner_refuses_when_capability_missing() -> None:
    provisioner = LinuxBhyveProvisioner()
    client = Mock()
    client.ping.return_value = {
        "service": "freebsd-laboratory-runtime",
        "version": 3,
        "capabilities": ["jail", "bhyve.freebsd"],
    }
    provisioner._client = Mock(return_value=client)

    with pytest.raises(RuntimeError, match="missing bhyve.linux capability"):
        provisioner._request_create("freebsd-lab-1234", 42, "ssh-ed25519 AAA...")
