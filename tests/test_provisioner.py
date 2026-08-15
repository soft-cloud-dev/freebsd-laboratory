from pathlib import Path

import pytest

from freebsd_laboratory.provisioner import jail_path_for_host_path, runtime_name


def test_runtime_name_is_jail_safe() -> None:
    assert runtime_name("15C6F1C8-98CD-4D1A") == "freebsd-lab-15c6f1c898cd4d1a"


def test_connection_file_is_mirrored_at_same_absolute_path() -> None:
    jail_root = Path("/usr/local/jails/containers/kernel")
    host_path = Path("/var/run/jupyter/kernel-abc.json")

    assert jail_path_for_host_path(jail_root, host_path) == Path(
        "/usr/local/jails/containers/kernel/var/run/jupyter/kernel-abc.json"
    )


def test_mirror_rejects_relative_connection_path() -> None:
    with pytest.raises(ValueError):
        jail_path_for_host_path(Path("/tmp/jail"), Path("kernel.json"))
