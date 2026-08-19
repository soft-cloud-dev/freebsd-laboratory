from __future__ import annotations

import asyncio
from pathlib import Path

from freebsd_laboratory.remote_provisioner import (
    RemoteRuntimeProvisioner,
    runtime_name,
)
from freebsd_laboratory.runtime_client import RuntimeControlError


def test_runtime_name_is_jail_safe() -> None:
    assert runtime_name("15C6F1C8-98CD-4D1A") == "freebsd-lab-15c6f1c898cd4d1a"


def test_stale_runtime_directory_is_removed_before_reuse(tmp_path: Path) -> None:
    runtime_path = tmp_path / "freebsd-lab-stale"
    runtime_path.mkdir()
    (runtime_path / "known_hosts").write_text("stale", encoding="utf-8")

    RemoteRuntimeProvisioner._remove_runtime_path(runtime_path)

    assert not runtime_path.exists()


def test_runtime_path_cleanup_rejects_no_symlink_following(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    marker = target / "marker"
    marker.write_text("keep", encoding="utf-8")
    link = tmp_path / "freebsd-lab-link"
    link.symlink_to(target, target_is_directory=True)

    RemoteRuntimeProvisioner._remove_runtime_path(link)

    assert not link.exists()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_destroy_rpc_failure_does_not_skip_local_cleanup(tmp_path: Path) -> None:
    runtime_name_value = "freebsd-lab-test"
    runtime_path = tmp_path / runtime_name_value
    runtime_path.mkdir()
    (runtime_path / "known_hosts").touch()

    class Logger:
        def warning(self, *args: object) -> None:
            return None

    class Client:
        def destroy(self, name: str) -> None:
            raise RuntimeControlError("daemon unavailable")

    class FakeProvisioner:
        _destroy_runtime = RemoteRuntimeProvisioner._destroy_runtime
        _remove_runtime_path = staticmethod(RemoteRuntimeProvisioner._remove_runtime_path)
        runtime_label = "jail"
        runtime_dir = str(tmp_path)
        _runtime_name = runtime_name_value
        _runtime_created = True
        _tunnel_reservation = None
        _tunnel_ports: tuple[int, ...] = ()
        guest_ip = "172.31.254.10"
        known_hosts_file = runtime_path / "known_hosts"
        log = Logger()

        def _client(self) -> Client:
            return Client()

        def _release_tunnel_ports(self) -> None:
            RemoteRuntimeProvisioner._release_tunnel_ports(self)  # type: ignore[arg-type]

    provisioner = FakeProvisioner()
    asyncio.run(provisioner._destroy_runtime())

    assert provisioner._runtime_created is False
    assert provisioner._runtime_name is None
    assert provisioner.guest_ip is None
    assert provisioner.known_hosts_file is None
    assert not runtime_path.exists()


def test_load_provisioner_info_requires_complete_unique_port_sets() -> None:
    provisioner = RemoteRuntimeProvisioner()
    base_info = {
        "kernel_id": "test-kernel",
        "connection_info": {},
        "pid": 1234,
        "pgid": 1234,
        "ip": "127.0.0.1",
        "ports_cached": False,
    }

    asyncio.run(
        provisioner.load_provisioner_info(
            {
                **base_info,
                "original_connection_ports": [41001, 41002, 41003],
                "tunnel_ports": [42001, 42001, 42003, 42004, 42005],
            }
        )
    )

    assert provisioner._original_connection_ports == ()
    assert provisioner._tunnel_ports == ()

    asyncio.run(
        provisioner.load_provisioner_info(
            {
                **base_info,
                "original_connection_ports": [41001, 41002, 41003, 41004, 41005],
                "tunnel_ports": [42001, 42002, 42003, 42004, 42005],
            }
        )
    )

    assert provisioner._original_connection_ports == (41001, 41002, 41003, 41004, 41005)
    assert provisioner._tunnel_ports == (42001, 42002, 42003, 42004, 42005)
