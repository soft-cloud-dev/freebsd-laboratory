---
name: freebsd-golden-image-debug
description: >-
  Use this skill when debugging FreeBSD golden image build failures for bhyve
  or jail templates, diagnosing missing files in raw UFS images, fixing
  ldconfig/linker issues, or troubleshooting package installation inside
  chroot/makefs environments.
---

# FreeBSD Golden Image Build Debugging

## Common Failure Modes

### 1. Missing files in raw UFS image after makefs

**Symptom**: Python packages or binaries exist in the chroot staging directory
but are absent from the final `.raw` image.

**Root Cause**: Late package installations (after the initial `vm-image` target)
are not tracked in the METALOG manifest that `makefs` uses.

**Fix**: Use `INSTALL_AS_USER=yes` and `-o METALOG="${DESTDIR}/METALOG.pkg"` for
all `pkg install` invocations inside `vmimage.conf` callbacks.

### 2. Shared library not found (`libpython3.12.so.1`)

**Symptom**: `python3 -c "import ..."` fails with "Shared object not found".

**Root Cause**: `/var/run/ld-elf.so.hints` was not refreshed after installing
packages that place `.so` files in `/usr/local/lib`.

**Fix**: Run `chroot "${DESTDIR}" /etc/rc.d/ldconfig forcestart` after every
package installation phase. Verify with `chroot "${DESTDIR}" /sbin/ldconfig -r`.

### 3. etcupdate / distribution fails inside vm-image

**Symptom**: `make -C /usr/src/release -V BRANCH` returns exit status 1 inside
the `mk-vmimage.sh` distribution pass.

**Root Cause**: Parent release `make` leaks `MAKEFLAGS` (including target
variables like `WITH_PKGBASE=yes`) into nested make invocations.

**Fix**: Add `unset MAKEFLAGS` at the start of `vm_create_base()` in
`vmimage.conf`.

### 4. pkg bootstrap packing list mismatch (ARM64)

**Symptom**: `pkg create -m ... -p ...` fails with "plist error" for manpage
paths during `FreeBSD-pkg` archive creation.

**Root Cause**: FreeBSD's release helper overrides the port's `--mandir` flag.
The staged manpages end up in a different directory than the plist expects.

**Fix**: Pass `MAKE_ARGS=mandir=/usr/local/share/man` when building the pkgbase
copy of `pkg`. This is controlled by `PKG_BOOTSTRAP_MAKE_ARGS` in
`build-bhyve-image.sh`.

### 5. sshd rejects authorized_keys (strict mode)

**Symptom**: SSH to bhyve VM fails even though the key file exists.

**Root Cause**: `nuageinit` created `~freebsd/.ssh/authorized_keys` as `root:wheel`.
FreeBSD's `sshd` with `StrictModes yes` rejects keys owned by wrong user.

**Fix**: Pre-create the file with correct ownership in `vmimage.conf` using
the user's numeric UID/GID from `pw -R "${DESTDIR}" usershow freebsd`.

### 6. Architecture mismatch on ARM64 hosts

**Symptom**: Build attempts to cross-compile for amd64 or tries to build
`qemu-user-static` on aarch64.

**Root Cause**: `TARGET` and `TARGET_ARCH` defaulted to `amd64`.

**Fix**: Auto-detect from `uname -m` and map:
- `amd64` → `TARGET=amd64`, `TARGET_ARCH=amd64`
- `aarch64|arm64` → `TARGET=arm64`, `TARGET_ARCH=aarch64`

## Verification Checklist

1. Run contract tests: `.venv/bin/python -m pytest tests/test_golden_image_*.py -v`
2. Shell syntax check: `sh -n deploy/freebsd/images/*.sh deploy/freebsd/images/vmimage.conf`
3. Git whitespace check: `git diff --check`
4. In-image verification (chroot):
   - `chroot "$DESTDIR" /usr/local/bin/python3 -c "import ipykernel"`
   - `chroot "$DESTDIR" /sbin/ldconfig -r | grep /usr/local/lib`
   - `ls -la "$DESTDIR/home/freebsd/.ssh/authorized_keys"`
5. Full test suite: `.venv/bin/python -m pytest -v` (150 tests)
