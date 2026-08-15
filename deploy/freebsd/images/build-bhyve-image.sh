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
BUILD_ID=${BUILD_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
OUTPUT_DIR=${OUTPUT_DIR:-/var/db/freebsd-laboratory/images}
OBJ_ROOT=${OBJ_ROOT:-/var/tmp/freebsd-laboratory-vm-${BUILD_ID}}
VM_SIZE=${VM_SIZE:-8g}
TARGET=${TARGET:-amd64}
TARGET_ARCH=${TARGET_ARCH:-amd64}
LAB_VM_PACKAGES=${LAB_VM_PACKAGES:-"python311 py311-ipykernel py311-cloud-init"}
LAB_PKG_REPOS_DIR=${LAB_PKG_REPOS_DIR:-}
LAB_FAIL_ON_PKG_AUDIT=${LAB_FAIL_ON_PKG_AUDIT:-YES}
VM_IMAGE_CONFIG=${VM_IMAGE_CONFIG:-${SCRIPT_DIR}/vmimage.conf}
LAB_SSHD_POLICY=${LAB_SSHD_POLICY:-${SCRIPT_DIR}/sshd-freebsd-lab.conf}

for command in git make sha256 install grep; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is unavailable: $command" >&2
        exit 1
    fi
done

if [ ! -f "${SRC_DIR}/release/Makefile" ] || [ ! -f "${SRC_DIR}/release/Makefile.vm" ]; then
    echo "FreeBSD release source tree not found at ${SRC_DIR}" >&2
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
OBJDIR=$(env MAKEOBJDIRPREFIX="$OBJ_ROOT" make -C "${SRC_DIR}/release" -V .OBJDIR)

set -- \
    env \
    MAKEOBJDIRPREFIX="$OBJ_ROOT" \
    LAB_BUILD_ID="$BUILD_ID" \
    LAB_SOURCE_BRANCH="$SOURCE_BRANCH" \
    LAB_SOURCE_REVISION="$SOURCE_REVISION" \
    LAB_VM_PACKAGES="$LAB_VM_PACKAGES" \
    LAB_FAIL_ON_PKG_AUDIT="$LAB_FAIL_ON_PKG_AUDIT" \
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

SOURCE_IMAGE="${OBJDIR}/freebsd-python.ufs.raw"
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
packages=${LAB_VM_PACKAGES}
sha256=$(cat "${VERSIONED_IMAGE}.sha256")
EOF

printf '%s\n' "Built bhyve golden image:"
printf '  image:  %s\n' "$VERSIONED_IMAGE"
printf '  sha256: %s\n' "$(cat "${VERSIONED_IMAGE}.sha256")"
printf '  source: %s (%s)\n' "$SOURCE_BRANCH" "$SOURCE_REVISION"
printf '%s\n' "Import/copy this artifact into the vm-bhyve image datastore as freebsd-python.raw."
