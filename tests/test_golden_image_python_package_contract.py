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
    assert (
        "pkg_root install -y $LAB_JAIL_PACKAGES\n"
        "refresh_target_ldconfig\n\n"
        'if [ -z "$LAB_JAIL_IPYKERNEL_PACKAGE" ]'
    ) in jail
    assert jail.count("refresh_target_ldconfig") == 3

    assert 'LAB_VM_PACKAGES=${LAB_VM_PACKAGES:-python3}' in vm_builder
    assert 'LAB_VM_IPYKERNEL_PACKAGE="$LAB_VM_IPYKERNEL_PACKAGE"' in vm_builder
    assert 'LAB_PKG_AUDIT_ALLOWED_VULN_IDS="$LAB_PKG_AUDIT_ALLOWED_VULN_IDS"' in vm_builder

    assert ': ${LAB_VM_PACKAGES:="python3"}' in vm_config
    assert 'export VM_RC_LIST="${VM_RC_LIST:-} sshd nuageinit"' in vm_config
    assert '"${DESTDIR}/home/freebsd/.ssh/authorized_keys"' in vm_config
    assert 'LAB_VM_FREEBSD_UID=$(pw -R "${DESTDIR}" usershow freebsd -7' in vm_config
    assert 'print("py{}{}".format(sys.version_info.major, sys.version_info.minor))' in vm_config
    assert 'LAB_VM_IPYKERNEL_PACKAGE="${LAB_VM_PYTHON_TAG}-ipykernel"' in vm_config
    assert vm_config.count("INSTALL_AS_USER=yes ${PKG_CMD}") == 2
    assert vm_config.count('-o METALOG="${DESTDIR}/METALOG.pkg"') == 2
    assert vm_config.count('-o PKG_DBDIR="${DESTDIR}/var/db/pkg"') == 2
    assert 'install -y -r "${PKG_REPO_NAME}"' in vm_config
    assert '"${LAB_VM_IPYKERNEL_PACKAGE}" || return 1' in vm_config
    assert "vm_refresh_ldconfig()" in vm_config
    assert 'chroot "${DESTDIR}" /etc/rc.d/ldconfig forcestart || return 1' in vm_config
    assert 'chroot "${DESTDIR}" /sbin/ldconfig -r' in vm_config
    assert "Target linker hints do not include /usr/local/lib" in vm_config
    assert vm_config.count("vm_refresh_ldconfig || return 1") == 3
    assert "vm_create_base()" in vm_config
    assert "unset MAKEFLAGS" in vm_config
    assert 'mkdir -p "${DESTDIR}/usr/local/lib" || return 1' in vm_config
    assert "vm_extra_install_base()" in vm_config
    assert "vm_pkg_audit()" in vm_config
    assert "vm_pkg_audit || return 1" in vm_config
    assert "LAB_VM_AUDIT_ACCEPTED_IDS" in vm_config
    assert '"pkg_audit_enforced": ${LAB_VM_AUDIT_ENFORCED}' in vm_config
    assert 'syslogd_flags="-ss"' in jail
    assert 'syslogd_flags="-ss"' in vm_config
