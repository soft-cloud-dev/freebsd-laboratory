from __future__ import annotations

from typing import Any

from .remote_provisioner import RemoteRuntimeProvisioner


class FreeBSDBhyveProvisioner(RemoteRuntimeProvisioner):
    """Run ipykernel inside an ephemeral bhyve VM on the private lab switch.

    The root-owned runtime daemon performs vm-bhyve lifecycle operations. The
    unprivileged Jupyter process reaches the guest only through SSH; all Jupyter
    TCP channels remain loopback-only inside the VM.
    """

    runtime_label = "bhyve VM"
    provisioner_name_key = "vm_name"

    @property
    def vm_name(self) -> str | None:
        return self._runtime_name

    @vm_name.setter
    def vm_name(self, value: str | None) -> None:
        self._runtime_name = value

    def _request_create(self, name: str, owner_pid: int) -> dict[str, Any]:
        return self._client().create_bhyve(name, owner_pid)


__all__ = ["FreeBSDBhyveProvisioner"]
