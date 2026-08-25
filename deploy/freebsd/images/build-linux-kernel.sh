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
LINUX_SHA256=${LINUX_SHA256:-5aa39a9bd555133ad741058f9908a277e6b36bb928481e747d885b50aaaa93ed}
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

# FreeBSD host compatibility: ensure ELF relocation symbols exist for host tools
if [ -f arch/x86/tools/relocs.h ] && ! grep -q 'R_X86_64_JUMP_SLOT' arch/x86/tools/relocs.h 2>/dev/null; then
    printf '\n#ifndef R_X86_64_JUMP_SLOT\n#define R_X86_64_JUMP_SLOT 7\n#endif\n' >> arch/x86/tools/relocs.h
fi

# FreeBSD host compatibility: provide asm/types.h for tools/ and scripts/
mkdir -p tools/include/asm
if [ ! -f tools/include/asm/types.h ]; then
    cat > tools/include/asm/types.h <<'EOF'
#ifndef _ASM_TYPES_H
#define _ASM_TYPES_H
#include <uapi/asm-generic/types.h>
#endif
EOF
fi

# FreeBSD host compatibility: fix BSD install flag ordering in tools Makefiles
find tools -name Makefile -exec sed -i '' -e 's/\$(INSTALL) \$1 \$(if \$3,-m \$3,)/\$(INSTALL) \$(if \$3,-m \$3,) \$1/g' {} + 2>/dev/null || true

cp "$KERNEL_CONFIG" .config

HOST_FLAGS='HOST_EXTRACFLAGS=-DR_X86_64_JUMP_SLOT=7 -Wno-macro-redefined KBUILD_HOSTCFLAGS=-DR_X86_64_JUMP_SLOT=7 -Wno-macro-redefined'

printf 'Validating Linux kernel configuration...\n'
gmake LLVM=1 ARCH=x86_64 HOST_EXTRACFLAGS="-DR_X86_64_JUMP_SLOT=7 -Wno-macro-redefined" KBUILD_HOSTCFLAGS="-DR_X86_64_JUMP_SLOT=7 -Wno-macro-redefined" olddefconfig

NPROC=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 2)
printf 'Compiling Linux kernel EFI stub with %s jobs...\n' "$NPROC"
gmake LLVM=1 ARCH=x86_64 HOST_EXTRACFLAGS="-DR_X86_64_JUMP_SLOT=7 -Wno-macro-redefined" KBUILD_HOSTCFLAGS="-DR_X86_64_JUMP_SLOT=7 -Wno-macro-redefined" -j"$NPROC" bzImage

BZIMAGE="${SOURCE_DIR}/arch/x86/boot/bzImage"
[ -f "$BZIMAGE" ] || fail "Kernel bzImage was not produced at $BZIMAGE"

TARGET_KERNEL="${OUTPUT_DIR}/vmlinuz-${LINUX_VERSION}-bhyve.efi"
install -m 0644 "$BZIMAGE" "$TARGET_KERNEL"
sha256 -q "$TARGET_KERNEL" > "${TARGET_KERNEL}.sha256"

printf 'Successfully built Linux EFI kernel:\n'
printf '  Kernel: %s\n' "$TARGET_KERNEL"
printf '  SHA256: %s\n' "$(cat "${TARGET_KERNEL}.sha256")"
