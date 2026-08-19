from __future__ import annotations

from typing import Any

from .remote_provisioner import RemoteRuntimeProvisioner, runtime_name


class FreeBSDJailProvisioner(RemoteRuntimeProvisioner):
    """Run ipykernel inside an ephemeral, VNET-isolated FreeBSD jail.

    Privileged lifecycle work is delegated to freebsd-lab-runtime-daemon over a
    peer-authenticated Unix-domain socket. The Jupyter process reaches the jail
    only through SSH; all Jupyter TCP channels are loopback-bound and forwarded
    through that SSH session.
    """

    runtime_label = "jail"
    provisioner_name_key = "jail_name"

    @property
    def jail_name(self) -> str | None:
        return self._runtime_name

    @jail_name.setter
    def jail_name(self, value: str | None) -> None:
        self._runtime_name = value

    def _request_create(
        self,
        name: str,
        owner_pid: int,
        ssh_public_key: str,
    ) -> dict[str, Any]:
        return self._client().create_jail(name, owner_pid, ssh_public_key)


__all__ = ["FreeBSDJailProvisioner", "runtime_name"]
