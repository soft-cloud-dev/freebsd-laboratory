#!/bin/sh
set -eu

if [ "$(uname -s)" != "FreeBSD" ]; then
    echo "build-bhyve-image.sh requires FreeBSD" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "build-bhyve-image.sh must run as root" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC_DIR=${SRC_DIR:-/usr/src}
PORTSDIR=${PORTSDIR:-/usr/ports}
BUILD_ID=${BUILD_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
OUTPUT_DIR=${OUTPUT_DIR:-/var/db/freebsd-laboratory/images}
OBJ_ROOT=${OBJ_ROOT:-/var/tmp/freebsd-laboratory-vm-${BUILD_ID}}
VM_SIZE=${VM_SIZE:-3g}
PKG_BOOTSTRAP_MAKE_ARGS=${PKG_BOOTSTRAP_MAKE_ARGS:-mandir=/usr/local/share/man}
case "$(uname -m)" in
    amd64)
        DEFAULT_TARGET=amd64
        DEFAULT_TARGET_ARCH=amd64
        ;;
    aarch64|arm64)
        DEFAULT_TARGET=arm64
        DEFAULT_TARGET_ARCH=aarch64
        ;;
    *)
        if [ -z "${TARGET:-}" ] || [ -z "${TARGET_ARCH:-}" ]; then
            echo "Set TARGET and TARGET_ARCH explicitly on unsupported host architecture: $(uname -m)" >&2
            exit 1
        fi
        DEFAULT_TARGET=$TARGET
        DEFAULT_TARGET_ARCH=$TARGET_ARCH
        ;;
esac
TARGET=${TARGET:-$DEFAULT_TARGET}
TARGET_ARCH=${TARGET_ARCH:-$DEFAULT_TARGET_ARCH}
LAB_VM_PACKAGES=${LAB_VM_PACKAGES:-python3}
LAB_VM_IPYKERNEL_PACKAGE=${LAB_VM_IPYKERNEL_PACKAGE:-}
LAB_PKG_REPOS_DIR=${LAB_PKG_REPOS_DIR:-}
LAB_FAIL_ON_PKG_AUDIT=${LAB_FAIL_ON_PKG_AUDIT:-YES}
LAB_PKG_AUDIT_ALLOWED_VULN_IDS=${LAB_PKG_AUDIT_ALLOWED_VULN_IDS-}
VM_IMAGE_CONFIG=${VM_IMAGE_CONFIG:-${SCRIPT_DIR}/vmimage.conf}
LAB_SSHD_POLICY=${LAB_SSHD_POLICY:-${SCRIPT_DIR}/sshd-freebsd-lab.conf}

for command in git make sha256 install grep find mount umount rm; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is unavailable: $command" >&2
        exit 1
    fi
done

if [ ! -f "${SRC_DIR}/release/Makefile" ] || [ ! -f "${SRC_DIR}/release/Makefile.vm" ]; then
    echo "FreeBSD release source tree not found at ${SRC_DIR}" >&2
    exit 1
fi
if [ ! -f "${PORTSDIR}/ports-mgmt/pkg/Makefile" ]; then
    echo "FreeBSD ports tree with ports-mgmt/pkg is required at ${PORTSDIR}" >&2
    echo "Install or update ports, or set PORTSDIR to a populated ports checkout." >&2
    exit 1
fi
if [ ! -f "$VM_IMAGE_CONFIG" ]; then
    echo "VM image configuration not found: $VM_IMAGE_CONFIG" >&2
    exit 1
fi
if [ ! -f "$LAB_SSHD_POLICY" ]; then
    echo "Laboratory SSH policy not found: $LAB_SSHD_POLICY" >&2
    exit 1
fi
if ! grep -q 'VM_IMAGE_CONFIG' "${SRC_DIR}/release/Makefile.vm"; then
    echo "Selected FreeBSD source does not pass VM_IMAGE_CONFIG to the vm-image target." >&2
    echo "Refusing to build an uncustomized laboratory image. Use a source revision with VM_IMAGE_CONFIG support." >&2
    exit 1
fi

SOURCE_REVISION=$(git -C "$SRC_DIR" rev-parse --verify HEAD)
SOURCE_BRANCH=$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD)

mkdir -p "$OBJ_ROOT" "$OUTPUT_DIR"
OBJDIR=$(env MAKEOBJDIRPREFIX="$OBJ_ROOT" make -C "${SRC_DIR}/release" \
    TARGET="$TARGET" TARGET_ARCH="$TARGET_ARCH" -V .OBJDIR)
case "$OBJDIR" in
    /*) ;;
    *)
        echo "Release object directory must be absolute: $OBJDIR" >&2
        exit 1
        ;;
esac
if [ "$OBJDIR" = "/" ]; then
    echo "Refusing to use filesystem root as release object directory" >&2
    exit 1
fi

VM_TARGET="${OBJDIR}/vm-image"
VM_STAGE="${OBJDIR}/vm-image-raw-ufs"
VM_INTERMEDIATE="${OBJDIR}/raw.ufs.img"
SOURCE_IMAGE="${OBJDIR}/freebsd-python.ufs.raw"
PKGBASE_REPO="${OBJDIR}/pkgbase-repo"
PKGBASE_CONFIG_DIR="${OBJDIR}/pkgbase-repo-dir"
PKGBASE_CONFIG="${PKGBASE_CONFIG_DIR}/FreeBSD-base.conf"

pkgbase_repo_has_catalog()
{
    [ -d "$PKGBASE_REPO" ] || return 1
    find "$PKGBASE_REPO" -type f \
        \( -name packagesite.pkg -o -name packagesite.txz \) \
        -print -quit 2>/dev/null | grep -q .
}

if [ -d "$PKGBASE_REPO" ]; then
    if ! pkgbase_repo_has_catalog; then
        printf 'Discarding incomplete FreeBSD pkgbase repository: %s\n' "$PKGBASE_REPO"
        rm -rf "$PKGBASE_REPO" "$PKGBASE_CONFIG_DIR"
    fi
elif [ -e "$PKGBASE_CONFIG_DIR" ]; then
    printf 'Discarding stale FreeBSD pkgbase repository configuration: %s\n' "$PKGBASE_CONFIG_DIR"
    rm -rf "$PKGBASE_CONFIG_DIR"
fi

if [ -d "$PKGBASE_CONFIG_DIR" ]; then
    if [ ! -f "$PKGBASE_CONFIG" ] || \
        ! grep -Fq "file://${PKGBASE_REPO}/" "$PKGBASE_CONFIG"; then
        printf 'Discarding invalid FreeBSD pkgbase repository configuration: %s\n' "$PKGBASE_CONFIG_DIR"
        rm -rf "$PKGBASE_CONFIG_DIR"
    fi
fi

if mount | grep -F " on ${VM_STAGE}/dev " >/dev/null 2>&1; then
    if ! umount "${VM_STAGE}/dev"; then
        echo "Unable to unmount stale VM staging devfs: ${VM_STAGE}/dev" >&2
        exit 1
    fi
fi
rm -rf "$VM_STAGE"
rm -f "$VM_TARGET" "$VM_INTERMEDIATE" "$SOURCE_IMAGE"

set -- \
    env \
    MAKEOBJDIRPREFIX="$OBJ_ROOT" \
    MAKE_ARGS="$PKG_BOOTSTRAP_MAKE_ARGS" \
    PORTSDIR="$PORTSDIR" \
    LAB_BUILD_ID="$BUILD_ID" \
    LAB_SOURCE_BRANCH="$SOURCE_BRANCH" \
    LAB_SOURCE_REVISION="$SOURCE_REVISION" \
    LAB_VM_PACKAGES="$LAB_VM_PACKAGES" \
    LAB_VM_IPYKERNEL_PACKAGE="$LAB_VM_IPYKERNEL_PACKAGE" \
    LAB_FAIL_ON_PKG_AUDIT="$LAB_FAIL_ON_PKG_AUDIT" \
    LAB_PKG_AUDIT_ALLOWED_VULN_IDS="$LAB_PKG_AUDIT_ALLOWED_VULN_IDS" \
    LAB_SSHD_POLICY="$LAB_SSHD_POLICY"

if [ -n "$LAB_PKG_REPOS_DIR" ]; then
    set -- "$@" PKG_REPOS_DIR="$LAB_PKG_REPOS_DIR"
fi

"$@" make -C "${SRC_DIR}/release" \
    -DWITH_VMIMAGES \
    TARGET="$TARGET" \
    TARGET_ARCH="$TARGET_ARCH" \
    VMFORMATS=raw \
    VMFSLIST=ufs \
    VMBASE=freebsd-python \
    VMSIZE="$VM_SIZE" \
    VM_IMAGE_CONFIG="$VM_IMAGE_CONFIG" \
    vm-image

if [ ! -f "$SOURCE_IMAGE" ]; then
    echo "Expected customized release image was not produced: $SOURCE_IMAGE" >&2
    exit 1
fi

VERSIONED_IMAGE="${OUTPUT_DIR%/}/freebsd-python-${BUILD_ID}.raw"
CURRENT_IMAGE="${OUTPUT_DIR%/}/freebsd-python.raw"
install -m 0644 "$SOURCE_IMAGE" "$VERSIONED_IMAGE"
ln -sfn "$(basename "$VERSIONED_IMAGE")" "$CURRENT_IMAGE"
sha256 -q "$VERSIONED_IMAGE" > "${VERSIONED_IMAGE}.sha256"

cat > "${OUTPUT_DIR%/}/freebsd-python-${BUILD_ID}.manifest" <<EOF
schema=softcloud.freebsd-golden-image/v1
type=bhyve-raw
build_id=${BUILD_ID}
source_branch=${SOURCE_BRANCH}
source_revision=${SOURCE_REVISION}
target=${TARGET}
target_arch=${TARGET_ARCH}
vm_size=${VM_SIZE}
packages=${LAB_VM_PACKAGES};ipykernel=${LAB_VM_IPYKERNEL_PACKAGE:-auto}
sha256=$(cat "${VERSIONED_IMAGE}.sha256")
EOF

printf '%s\n' "Built bhyve golden image:"
printf '  image:  %s\n' "$VERSIONED_IMAGE"
printf '  sha256: %s\n' "$(cat "${VERSIONED_IMAGE}.sha256")"
printf '  source: %s (%s)\n' "$SOURCE_BRANCH" "$SOURCE_REVISION"
printf '%s\n' "Import/copy this artifact into the vm-bhyve image datastore as freebsd-python.raw."
