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

# FreeBSD host compatibility: provide asm/ headers for tools/ and scripts/
mkdir -p tools/include/asm
cat > tools/include/asm/types.h <<'EOF'
#ifndef _ASM_TYPES_H
#define _ASM_TYPES_H
#include <stdint.h>
typedef int8_t __s8;
typedef uint8_t __u8;
typedef int16_t __s16;
typedef uint16_t __u16;
typedef int32_t __s32;
typedef uint32_t __u32;
typedef int64_t __s64;
typedef uint64_t __u64;
#endif
EOF

cat > tools/include/asm/posix_types.h <<'EOF'
#ifndef _ASM_POSIX_TYPES_H
#define _ASM_POSIX_TYPES_H
#include <sys/types.h>
typedef unsigned long __kernel_ulong_t;
typedef long __kernel_long_t;
typedef unsigned int __kernel_mode_t;
typedef int __kernel_pid_t;
typedef int __kernel_ipc_pid_t;
typedef unsigned int __kernel_uid_t;
typedef unsigned int __kernel_gid_t;
typedef __kernel_ulong_t __kernel_size_t;
typedef __kernel_long_t __kernel_ssize_t;
typedef __kernel_long_t __kernel_ptrdiff_t;
typedef __kernel_long_t __kernel_time_t;
typedef __kernel_long_t __kernel_clock_t;
typedef int __kernel_timer_t;
typedef int __kernel_clockid_t;
typedef char * __kernel_caddr_t;
typedef unsigned short __kernel_uid16_t;
typedef unsigned short __kernel_gid16_t;
typedef long long __kernel_loff_t;
typedef __kernel_long_t __kernel_old_time_t;
typedef long __kernel_time64_t;
typedef struct { int val[2]; } __kernel_fsid_t;
#endif
EOF

cat > tools/include/asm/bitsperlong.h <<'EOF'
#ifndef _ASM_BITSPERLONG_H
#define _ASM_BITSPERLONG_H
#define __BITS_PER_LONG 64
#endif
EOF

mkdir -p tools/include/linux
cat > tools/include/linux/elf.h <<'EOF'
#ifndef _TOOLS_LINUX_ELF_H
#define _TOOLS_LINUX_ELF_H
#include <elf.h>
#endif
EOF

# FreeBSD host compatibility: provide dummy objtool to bypass host tool compilation
mkdir -p tools/objtool
cat > tools/objtool/Makefile <<'EOF'
all:
	@printf '#!/bin/sh\nexit 0\n' > $(CURDIR)/objtool
	@chmod +x $(CURDIR)/objtool
clean:
EOF

# FreeBSD host compatibility: fix sed-voffset regex in arch/x86/boot/compressed/Makefile for BSD sed
sed -i '' -e 's/sed-voffset := -e/sed-voffset := -E -e/' \
          -e 's/\\(\[0-9a-fA-F\]\*\\)/([0-9a-fA-F]*)/' \
          -e 's/\\(_text\\|__start_rodata\\|__bss_start\\|_end\\)/(_text|__start_rodata|__bss_start|_end)/' \
          arch/x86/boot/compressed/Makefile

# FreeBSD host compatibility: fix sed-zoffset regex in arch/x86/boot/Makefile for BSD sed
sed -i '' -e 's/sed-zoffset := -e/sed-zoffset := -E -e/' \
          -e 's/\\(\[0-9a-fA-F\]\*\\)/([0-9a-fA-F]*)/' \
          -e 's/\\(startup_32\\|efi.._stub_entry\\|efi\\(32\\)\\?_pe_entry\\|input_data\\|kernel_info\\|_end\\|_ehead\\|_text\\|_e\\?data\\|z_.\*\\)/(startup_32|efi.._stub_entry|efi(32)?_pe_entry|input_data|kernel_info|_end|_ehead|_text|_e?data|z_.*)/' \
          arch/x86/boot/Makefile

cp "$KERNEL_CONFIG" .config

HOST_EXTRACFLAGS="-I${SOURCE_DIR}/tools/include -DR_X86_64_JUMP_SLOT=7 -Wno-macro-redefined"
KBUILD_HOSTCFLAGS="-I${SOURCE_DIR}/tools/include -DR_X86_64_JUMP_SLOT=7 -Wno-macro-redefined"

printf 'Validating Linux kernel configuration...\n'
gmake LLVM=1 ARCH=x86_64 HOST_EXTRACFLAGS="$HOST_EXTRACFLAGS" KBUILD_HOSTCFLAGS="$KBUILD_HOSTCFLAGS" olddefconfig

NPROC=$(sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 2)
printf 'Compiling Linux kernel EFI stub with %s jobs...\n' "$NPROC"
gmake LLVM=1 ARCH=x86_64 HOST_EXTRACFLAGS="$HOST_EXTRACFLAGS" KBUILD_HOSTCFLAGS="$KBUILD_HOSTCFLAGS" -j"$NPROC" bzImage

BZIMAGE="${SOURCE_DIR}/arch/x86/boot/bzImage"
[ -f "$BZIMAGE" ] || fail "Kernel bzImage was not produced at $BZIMAGE"

TARGET_KERNEL="${OUTPUT_DIR}/vmlinuz-${LINUX_VERSION}-bhyve.efi"
install -m 0644 "$BZIMAGE" "$TARGET_KERNEL"
sha256 -q "$TARGET_KERNEL" > "${TARGET_KERNEL}.sha256"

printf 'Successfully built Linux EFI kernel:\n'
printf '  Kernel: %s\n' "$TARGET_KERNEL"
printf '  SHA256: %s\n' "$(cat "${TARGET_KERNEL}.sha256")"
