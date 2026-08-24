from pathlib import Path

JAIL_BUILDER = Path("deploy/freebsd/images/build-jail-template.sh")
VM_BUILDER = Path("deploy/freebsd/images/build-bhyve-image.sh")
VM_CONFIG = Path("deploy/freebsd/images/vmimage.conf")


def test_golden_images_derive_python_flavor_from_python3() -> None:
    jail = JAIL_BUILDER.read_text(encoding="utf-8")
    vm_builder = VM_BUILDER.read_text(encoding="utf-8")
    vm_config = VM_CONFIG.read_text(encoding="utf-8")

    assert 'LAB_JAIL_PACKAGES=${LAB_JAIL_PACKAGES:-python3}' in jail
    assert 'LAB_JAIL_IPYKERNEL_PACKAGE=${LAB_JAIL_IPYKERNEL_PACKAGE:-}' in jail
    assert 'print(f"py{sys.version_info.major}{sys.version_info.minor}")' in jail
    assert 'LAB_JAIL_IPYKERNEL_PACKAGE="${LAB_JAIL_PYTHON_TAG}-ipykernel"' in jail
    assert 'pkg_root install -y "$LAB_JAIL_IPYKERNEL_PACKAGE"' in jail

    assert 'LAB_VM_PACKAGES=${LAB_VM_PACKAGES:-python3}' in vm_builder
    assert 'LAB_VM_IPYKERNEL_PACKAGE="$LAB_VM_IPYKERNEL_PACKAGE"' in vm_builder
    assert 'LAB_VM_CLOUD_INIT_PACKAGE="$LAB_VM_CLOUD_INIT_PACKAGE"' in vm_builder

    assert ': ${LAB_VM_PACKAGES:="python3"}' in vm_config
    assert 'print("py{}{}".format(sys.version_info.major, sys.version_info.minor))' in vm_config
    assert 'LAB_VM_IPYKERNEL_PACKAGE="${LAB_VM_PYTHON_TAG}-ipykernel"' in vm_config
    assert 'LAB_VM_CLOUD_INIT_PACKAGE="${LAB_VM_PYTHON_TAG}-cloud-init"' in vm_config
    assert 'install -y "${LAB_VM_IPYKERNEL_PACKAGE}" "${LAB_VM_CLOUD_INIT_PACKAGE}"' in vm_config
    assert "vm_refresh_ldconfig()" in vm_config
    assert 'chroot "${DESTDIR}" /etc/rc.d/ldconfig forcestart || return 1' in vm_config
    assert vm_config.count("vm_refresh_ldconfig || return 1") == 2
    assert "vm_create_base()" in vm_config
    assert 'mkdir -p "${DESTDIR}/usr/local/lib" || return 1' in vm_config
