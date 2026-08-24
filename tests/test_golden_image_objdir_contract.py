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
