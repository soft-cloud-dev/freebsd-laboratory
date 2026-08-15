from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from jupyter_client.provisioning import LocalProvisioner
from traitlets import Unicode


def runtime_name(kernel_id: str) -> str:
    compact = re.sub(r"[^a-zA-Z0-9]", "", kernel_id).lower()
    if not compact:
        raise ValueError("kernel_id does not contain a usable identifier")
    return f"freebsd-lab-{compact[:16]}"


def jail_path_for_host_path(jail_root: Path, host_path: Path) -> Path:
    if not host_path.is_absolute():
        raise ValueError("Connection file path must be absolute")
    root = jail_root.resolve()
    target = (root / str(host_path).lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise ValueError("Mirrored path escapes jail root")
    return target


class FreeBSDJailProvisioner(LocalProvisioner):
    """Launch a Jupyter kernel inside a disposable ZFS-backed FreeBSD jail.

    This first implementation intentionally requires the Jupyter Server itself to
    run on FreeBSD. It does not emulate a jail on other operating systems.
    """

    template_snapshot: str = Unicode(
        "zroot/jails/templates/freebsd-python@clean",
        help="ZFS snapshot containing the FreeBSD Python kernel template.",
    ).tag(config=True)
    dataset_parent: str = Unicode(
        "zroot/jails/containers",
        help="ZFS dataset under which ephemeral kernel clones are created.",
    ).tag(config=True)
    mount_root: str = Unicode(
        "/usr/local/jails/containers",
        help="Host directory under which ephemeral jail roots are mounted.",
    ).tag(config=True)

    jail_name: str | None = None
    jail_dataset: str | None = None
    jail_root: Path | None = None
    _clone_created = False
    _jail_created = False

    @staticmethod
    def _assert_supported_host() -> None:
        if platform.system() != "FreeBSD":
            raise RuntimeError("FreeBSD jail provisioner requires a FreeBSD host")
        if os.geteuid() != 0:
            raise PermissionError("FreeBSD jail provisioner requires root privileges")

    @staticmethod
    def _run(command: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "command failed"
            raise RuntimeError(f"{' '.join(command)}: {detail}")
        return result

    def _create_runtime(self) -> None:
        self.jail_name = runtime_name(str(self.kernel_id))
        self.jail_dataset = f"{self.dataset_parent.rstrip('/')}/{self.jail_name}"
        self.jail_root = (Path(self.mount_root) / self.jail_name).resolve()
        self._clone_created = False
        self._jail_created = False

        try:
            self._run(
                [
                    "zfs",
                    "clone",
                    "-o",
                    f"mountpoint={self.jail_root}",
                    self.template_snapshot,
                    self.jail_dataset,
                ]
            )
            self._clone_created = True
            self._run(
                [
                    "jail",
                    "-c",
                    f"name={self.jail_name}",
                    f"path={self.jail_root}",
                    f"host.hostname={self.jail_name}",
                    "persist",
                    "exec.clean",
                    "mount.devfs",
                    "ip4=inherit",
                    "ip6=disable",
                ]
            )
            self._jail_created = True
        except Exception:
            self._destroy_runtime()
            raise

    def _mirror_connection_file(self) -> None:
        if self.jail_root is None:
            raise RuntimeError("Jail root is not initialized")
        if self.parent is None or not getattr(self.parent, "connection_file", None):
            raise RuntimeError("Kernel manager connection file is unavailable")

        host_path = Path(self.parent.connection_file).resolve()
        if not host_path.is_file():
            raise RuntimeError(f"Kernel connection file does not exist: {host_path}")

        jail_path = jail_path_for_host_path(self.jail_root, host_path)
        jail_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(host_path, jail_path)
        jail_path.chmod(0o600)

    def _destroy_runtime(self) -> None:
        if self._jail_created and self.jail_name:
            self._run(["jail", "-r", self.jail_name], check=False)
        if self._clone_created and self.jail_dataset:
            self._run(["zfs", "destroy", "-r", self.jail_dataset], check=False)
        self._jail_created = False
        self._clone_created = False
        self.jail_name = None
        self.jail_dataset = None
        self.jail_root = None

    async def pre_launch(self, **kwargs: Any) -> dict[str, Any]:
        self._assert_supported_host()
        prepared = await super().pre_launch(**kwargs)
        self._create_runtime()
        try:
            self._mirror_connection_file()
            if self.jail_name is None:
                raise RuntimeError("Jail name is not initialized")
            kernel_command = list(prepared["cmd"])
            prepared["cmd"] = ["jexec", "-l", self.jail_name, *kernel_command]
            prepared.pop("cwd", None)
            return prepared
        except Exception:
            self._destroy_runtime()
            raise

    async def cleanup(self, restart: bool = False) -> None:
        try:
            await super().cleanup(restart=restart)
        finally:
            self._destroy_runtime()

    async def get_provisioner_info(self) -> dict[str, Any]:
        info = await super().get_provisioner_info()
        info.update(
            {
                "jail_name": self.jail_name,
                "jail_dataset": self.jail_dataset,
                "jail_root": str(self.jail_root) if self.jail_root else None,
                "clone_created": self._clone_created,
                "jail_created": self._jail_created,
            }
        )
        return info

    async def load_provisioner_info(self, provisioner_info: dict[str, Any]) -> None:
        await super().load_provisioner_info(provisioner_info)
        self.jail_name = provisioner_info.get("jail_name")
        self.jail_dataset = provisioner_info.get("jail_dataset")
        jail_root = provisioner_info.get("jail_root")
        self.jail_root = Path(jail_root) if jail_root else None
        self._clone_created = bool(provisioner_info.get("clone_created"))
        self._jail_created = bool(provisioner_info.get("jail_created"))
