#!/bin/sh
set -eu

fail()
{
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KERNEL_CONFIG=${KERNEL_CONFIG:-${SCRIPT_DIR}/linux-kernel.config}
LINUX_VERSION=${LINUX_VERSION:-6.6.78}
LINUX_TARBALL="linux-${LINUX_VERSION}.tar.xz"
LINUX_URL=${LINUX_URL:-https://cdn.kernel.org/pub/linux/kernel/v6.x/${LINUX_TARBALL}}
LINUX_SHA256=${LINUX_SHA256:-c7f66a2e4e16d4826b158022718cbce4aaec0374e2d3b2f8a84594247547dfb4}
BUILD_ROOT=${BUILD_ROOT:-/var/tmp/freebsd-laboratory-linux-kernel}
OUTPUT_DIR=${OUTPUT_DIR:-/var/db/freebsd-laboratory/images}

[ -f "$KERNEL_CONFIG" ] || fail "Kernel configuration not found: $KERNEL_CONFIG"

for cmd in gmake clang ld.lld bc bison flex sha256 fetch tar; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        fail "Required command is unavailable: $cmd (install with pkg install gmake llvm bison flex)"
    fi
done

mkdir -p "$BUILD_ROOT" "$OUTPUT_DIR"
cd "$BUILD_ROOT"

if [ ! -f "$LINUX_TARBALL" ]; then
    printf 'Fetching Linux %s source...\n' "$LINUX_VERSION"
    fetch -o "$LINUX_TARBALL" "$LINUX_URL"
fi

ACTUAL_SHA256=$(sha256 -q "$LINUX_TARBALL")
if [ "$ACTUAL_SHA256" != "$LINUX_SHA256" ]; then
    fail "Linux source checksum mismatch: expected $LINUX_SHA256 but got $ACTUAL_SHA256"
fi

SOURCE_DIR="${BUILD_ROOT}/linux-${LINUX_VERSION}"
if [ ! -d "$SOURCE_DIR" ]; then
    printf 'Extracting Linux source...\n'
    tar -xf "$LINUX_TARBALL"
fi

cd "$SOURCE_DIR"
cp "$KERNEL_CONFIG" .config

printf 'Validating Linux kernel configuration...\n'
gmake LLVM=1 ARCH=x86_64 olddefconfig

NPROC=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 2)
printf 'Compiling Linux kernel EFI stub with %s jobs...\n' "$NPROC"
gmake LLVM=1 ARCH=x86_64 -j"$NPROC" bzImage

BZIMAGE="${SOURCE_DIR}/arch/x86/boot/bzImage"
[ -f "$BZIMAGE" ] || fail "Kernel bzImage was not produced at $BZIMAGE"

TARGET_KERNEL="${OUTPUT_DIR}/vmlinuz-${LINUX_VERSION}-bhyve.efi"
install -m 0644 "$BZIMAGE" "$TARGET_KERNEL"
sha256 -q "$TARGET_KERNEL" > "${TARGET_KERNEL}.sha256"

printf 'Successfully built Linux EFI kernel:\n'
printf '  Kernel: %s\n' "$TARGET_KERNEL"
printf '  SHA256: %s\n' "$(cat "${TARGET_KERNEL}.sha256")"
