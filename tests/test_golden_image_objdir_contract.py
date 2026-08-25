from pathlib import Path

GOLDEN_BUILDER = Path("deploy/freebsd/images/build-golden-images.sh")
BHYVE_BUILDER = Path("deploy/freebsd/images/build-bhyve-image.sh")


def test_paired_golden_build_uses_one_object_root() -> None:
    golden = GOLDEN_BUILDER.read_text(encoding="utf-8")
    bhyve = BHYVE_BUILDER.read_text(encoding="utf-8")

    assert 'OBJ_ROOT=${OBJ_ROOT:-/var/tmp/freebsd-laboratory-vm-${BUILD_ID}}' in golden
    assert 'FreeBSD object root: %s\\n' in golden
    assert 'env MAKEOBJDIRPREFIX="$OBJ_ROOT"' in golden
    assert 'make -C "$SRC_DIR" -j "$JOBS" buildworld buildkernel' in golden
    assert golden.count('OBJ_ROOT="$OBJ_ROOT"') == 2
    assert golden.count('MAKEOBJDIRPREFIX="$OBJ_ROOT"') == 3
    assert 'LAB_JAIL_IMAGE_MODE=source' in golden
    assert '${SCRIPT_DIR}/build-bhyve-image.sh' in golden
    assert 'OBJ_ROOT=${OBJ_ROOT:-/var/tmp/freebsd-laboratory-vm-${BUILD_ID}}' in bhyve
    assert 'MAKEOBJDIRPREFIX="$OBJ_ROOT"' in bhyve


def test_bhyve_build_defaults_to_the_native_freebsd_architecture() -> None:
    bhyve = BHYVE_BUILDER.read_text(encoding="utf-8")

    assert 'case "$(uname -m)" in' in bhyve
    assert "aarch64|arm64)" in bhyve
    assert "DEFAULT_TARGET=arm64" in bhyve
    assert "DEFAULT_TARGET_ARCH=aarch64" in bhyve
    assert "TARGET=${TARGET:-$DEFAULT_TARGET}" in bhyve
    assert "TARGET_ARCH=${TARGET_ARCH:-$DEFAULT_TARGET_ARCH}" in bhyve
    assert 'TARGET="$TARGET" TARGET_ARCH="$TARGET_ARCH" -V .OBJDIR' in bhyve


def test_direct_bhyve_build_discards_only_stale_vm_image_state() -> None:
    bhyve = BHYVE_BUILDER.read_text(encoding="utf-8")

    assert 'VM_TARGET="${OBJDIR}/vm-image"' in bhyve
    assert 'VM_STAGE="${OBJDIR}/vm-image-raw-ufs"' in bhyve
    assert 'VM_INTERMEDIATE="${OBJDIR}/raw.ufs.img"' in bhyve
    assert 'SOURCE_IMAGE="${OBJDIR}/freebsd-python.ufs.raw"' in bhyve
    assert 'mount | grep -F " on ${VM_STAGE}/dev "' in bhyve
    assert 'umount "${VM_STAGE}/dev"' in bhyve
    assert 'rm -rf "$VM_STAGE"' in bhyve
    assert 'rm -f "$VM_TARGET" "$VM_INTERMEDIATE" "$SOURCE_IMAGE"' in bhyve
    assert 'rm -rf "$OBJ_ROOT"' not in bhyve


def test_direct_bhyve_build_recovers_incomplete_pkgbase_repository() -> None:
    bhyve = BHYVE_BUILDER.read_text(encoding="utf-8")

    assert 'PKGBASE_REPO="${OBJDIR}/pkgbase-repo"' in bhyve
    assert 'PKGBASE_CONFIG_DIR="${OBJDIR}/pkgbase-repo-dir"' in bhyve
    assert "pkgbase_repo_has_catalog()" in bhyve
    assert "-name packagesite.pkg -o -name packagesite.txz" in bhyve
    assert 'rm -rf "$PKGBASE_REPO" "$PKGBASE_CONFIG_DIR"' in bhyve
    assert 'rm -rf "$PKGBASE_CONFIG_DIR"' in bhyve
    assert 'file://${PKGBASE_REPO}/' in bhyve
    assert 'rm -rf "$OBJ_ROOT"' not in bhyve


def test_direct_bhyve_build_requires_ports_pkg_bootstrap_source() -> None:
    bhyve = BHYVE_BUILDER.read_text(encoding="utf-8")

    assert 'PORTSDIR=${PORTSDIR:-/usr/ports}' in bhyve
    assert 'PKG_BOOTSTRAP_MAKE_ARGS=${PKG_BOOTSTRAP_MAKE_ARGS:-mandir=/usr/local/share/man}' in bhyve
    assert '[ ! -f "${PORTSDIR}/ports-mgmt/pkg/Makefile" ]' in bhyve
    assert 'FreeBSD ports tree with ports-mgmt/pkg is required at ${PORTSDIR}' in bhyve
    assert 'MAKE_ARGS="$PKG_BOOTSTRAP_MAKE_ARGS"' in bhyve
    assert 'PORTSDIR="$PORTSDIR"' in bhyve
