# FreeBSD Laboratory golden images

The laboratory has two runtime artifact families:

```text
VNET jail: versioned ZFS dataset snapshot ending in @clean
bhyve VM:  versioned raw disk image with a freebsd-python.raw activation name
```

The VNET jail builder now supports two explicit image modes. The normal host bootstrap uses an official FreeBSD RELEASE distribution set; the source pipeline remains available for source-controlled/reproducible builds.

## Jail mode: release (default)

`build-jail-template.sh` defaults to `LAB_JAIL_IMAGE_MODE=release`.

The builder:

1. derives the host `X.Y-RELEASE` value unless `LAB_RELEASE` is supplied;
2. downloads the official release `MANIFEST` and `base.txz` from `download.freebsd.org`;
3. verifies the `base.txz` SHA-256 against the hash in that MANIFEST;
4. extracts the userland into a new versioned ZFS dataset;
5. installs Python and ipykernel from pkg;
6. creates the `freebsd` runtime account and applies the restricted SSH policy;
7. removes SSH host private keys so each runtime creates its own identity;
8. validates ipykernel and runs `pkg audit -F` by default;
9. writes release URL/hash/package provenance into the image manifest;
10. creates the final `@clean` snapshot.

Example:

```sh
LAB_JAIL_IMAGE_MODE=release \
LAB_RELEASE=15.1-RELEASE \
JAIL_DATASET_PREFIX=zroot/jails/templates/freebsd-python-release \
  ./build-jail-template.sh
```

Typical result:

```text
zroot/jails/templates/freebsd-python-release-20260819T014500Z@clean
```

On amd64 and aarch64 the official release path is derived automatically. Other architectures must set `LAB_RELEASE_TARGET` and `LAB_RELEASE_TARGET_ARCH` explicitly.

## Jail mode: source

Source mode keeps the previous `buildworld -> installworld` model and records the exact FreeBSD source revision:

```sh
cd /usr/src
git fetch --all --prune
git checkout releng/<release>
make -j"$(sysctl -n hw.ncpu)" buildworld

cd /path/to/freebsd-laboratory/deploy/freebsd/images
LAB_JAIL_IMAGE_MODE=source \
SRC_DIR=/usr/src \
JAIL_DATASET_PREFIX=zroot/jails/templates/freebsd-python-source \
  ./build-jail-template.sh
```

The builder requires a Git checkout on `releng/*`, runs `installworld` and `distribution` into the new ZFS dataset, adds the runtime packages and SSH policy, records the exact branch/revision, and snapshots the result.

Typical result:

```text
zroot/jails/templates/freebsd-python-source-20260819T014500Z@clean
```

`build-golden-images.sh` remains the paired source-build workflow for jail + bhyve artifacts. It explicitly invokes `build-jail-template.sh` with `LAB_JAIL_IMAGE_MODE=source`, so its jail and VM artifacts continue to originate from the same FreeBSD source revision.

## Paired source rebuild

Run this on a dedicated FreeBSD image-builder host after updating `/usr/src` to the intended patched `releng/*` revision:

```sh
cd /usr/src
git fetch --all --prune
git checkout releng/<release>
git pull --ff-only
cd /path/to/freebsd-laboratory
sudo env SRC_DIR=/usr/src deploy/freebsd/images/build-golden-images.sh
```

The wrapper uses one object root (`OBJ_ROOT`, defaulting to `/var/tmp/freebsd-laboratory-vm-<build-id>`) for `buildworld`, `buildkernel`, the source jail installation, and `vm-image`; no phase may use a different `MAKEOBJDIRPREFIX`. Set `SKIP_SOURCE_BUILD=YES` only when that same `OBJ_ROOT` already contains matching world/kernel output from the exact source tree.

## Package source

The runtime package set is deliberately small. Builders install the repository's `python3` meta-package, then derive the matching `pyXY-ipykernel` (and, for bhyve, `pyXY-cloud-init`) package from that interpreter inside the target image. This avoids pinning a retired Python package flavor. Direct builder use can override `LAB_JAIL_PACKAGES`, `LAB_JAIL_IPYKERNEL_PACKAGE`, `LAB_VM_PACKAGES`, `LAB_VM_IPYKERNEL_PACKAGE`, or `LAB_VM_CLOUD_INIT_PACKAGE`.

For controlled package provenance, point `LAB_PKG_REPOS_DIR` at a root-owned directory containing pkg repository configuration files for the laboratory's Poudriere repository. If it is unset, the host's normal pkg repository configuration is used.

The build fails on `pkg audit -F` findings by default. `LAB_FAIL_ON_PKG_AUDIT=NO` exists for diagnostics and should not be used to activate a production golden image.

## Activation

Activation is explicit. Point the runtime daemon at the selected snapshot through the root-controlled rc.conf arguments, restart the daemon, execute a laboratory smoke test, and retain the previous snapshot until validation is complete.

```sh
sysrc freebsd_lab_daemon_runtime_args="--jail-template=zroot/jails/templates/freebsd-python-<mode>-<build-id>@clean"
service freebsd_lab_daemon restart
```

Do not destroy an older snapshot while active ZFS clones still depend on it.

## bhyve image

`build-bhyve-image.sh` remains source-based. It drives the FreeBSD release `vm-image` target with raw/UFS output and `vmimage.conf`. The configuration adds Python, ipykernel, cloud-init support, the `freebsd` account, and the restricted SSH policy before the image is unmounted. FreeBSD's native `nuageinit` service processes vm-bhyve's NoCloud seed, including `network-config` and SSH authorized keys. The image enables `nuageinit`, not the Python package's `cloudinit` rc service. It pre-creates `freebsd`'s `authorized_keys` with that user's numeric UID/GID because nuageinit otherwise creates the file as root and sshd rejects it under strict modes. It also pre-creates the target `/usr/local/lib` directory before FreeBSD's initial linker-cache setup, then runs `ldconfig forcestart` after each package-installation phase before executing the target Python interpreter.

The builder fails closed unless the selected FreeBSD source revision exposes `VM_IMAGE_CONFIG` in `release/Makefile.vm`. Without that support, `make vm-image` would ignore the laboratory customization file and could produce an apparently valid but unusable base image.

FreeBSD's `vm-image` target bootstraps `pkg` from the ports tree while it prepares its pkgbase repository. Before starting the image build, this wrapper therefore requires a populated `${PORTSDIR:-/usr/ports}/ports-mgmt/pkg` directory. Set `PORTSDIR` when the ports checkout is not mounted at `/usr/ports`.

The release helper replaces the port's normal configure arguments while building the pkgbase copy of `pkg`. The wrapper therefore supplies `MAKE_ARGS=mandir=/usr/local/share/man` for that port install so its staged manpages continue to match the current ports packing list. Override `PKG_BOOTSTRAP_MAKE_ARGS` only when using a ports revision with different staging requirements.

Artifacts are versioned under `/var/db/freebsd-laboratory/images` by default:

```text
freebsd-python-<build-id>.raw
freebsd-python-<build-id>.raw.sha256
freebsd-python-<build-id>.manifest
freebsd-python.raw -> freebsd-python-<build-id>.raw
```

## SSH image policy

`sshd-freebsd-lab.conf` disables password/root login, agent/X11/tunnel forwarding and gateway ports. `AllowTcpForwarding local` remains enabled because all five Jupyter TCP channels are intentionally carried through local SSH forwards rather than exposed on `labbridge0`.

SSH host private keys are removed from golden artifacts. Each instantiated jail or VM must generate its own host keys instead of inheriting the builder's identity.

## CI boundary

The normal GitHub Actions workflow runs portable Python/TypeScript tests and shell syntax checks. Full image construction is not attached to pull requests: it requires a privileged FreeBSD host with ZFS, network access to the selected FreeBSD/pkg repositories, and, for source mode, enough local resources for `buildworld`.

The release gate remains:

```text
portable CI green
  -> build on FreeBSD host
  -> pkg audit passes
  -> record release hash or source revision
  -> real VNET jail smoke test
  -> activate versioned artifact
```
