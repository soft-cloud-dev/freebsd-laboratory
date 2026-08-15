# FreeBSD Laboratory golden images

The laboratory has two rebuildable runtime artifacts:

```text
VNET jail: versioned ZFS dataset snapshot ending in @clean
bhyve VM:  versioned raw disk image with a freebsd-python.raw activation name
```

Both builders use a FreeBSD source tree and record the exact source revision. The wrapper refuses a non-`releng/*` source branch by default so security-image builds do not accidentally come from CURRENT or STABLE.

## One-command rebuild

Run this on a dedicated FreeBSD image-builder host after updating `/usr/src` to the intended patched `releng/*` revision:

```sh
cd /usr/src
git fetch --all --prune
git checkout releng/<release>
git pull --ff-only
cd /path/to/freebsd-laboratory
sudo env SRC_DIR=/usr/src deploy/freebsd/images/build-golden-images.sh
```

The wrapper builds world/kernel first, then creates both runtime artifacts from the same source revision and build id. Set `SKIP_SOURCE_BUILD=YES` only when the matching world/kernel have already been built from that exact source tree.

## Package source

The default package set is deliberately small:

```text
jail:  python311 py311-ipykernel
bhyve: python311 py311-ipykernel py311-cloud-init
```

Override `LAB_JAIL_PACKAGES` or `LAB_VM_PACKAGES` when a release uses different package names.

For controlled package provenance, point `LAB_PKG_REPOS_DIR` at a root-owned directory containing pkg repository configuration files for the laboratory's Poudriere repository. If it is unset, the host's normal pkg repository configuration is used.

The build fails on `pkg audit -F` findings by default. `LAB_FAIL_ON_PKG_AUDIT=NO` exists for diagnostics and should not be used to activate a production golden image.

## Jail image

`build-jail-template.sh` creates a new versioned ZFS dataset, installs the already-built FreeBSD world into it, adds the runtime packages, creates the `freebsd` account, installs the restricted SSH policy, validates that `ipykernel` imports inside the template, records provenance, and snapshots the result.

Example result:

```text
zroot/jails/templates/freebsd-python-20260815T190000Z@clean
```

Activation is explicit. Point the runtime daemon at the new snapshot through the root-controlled rc.conf arguments, restart the daemon, execute a laboratory smoke test, and keep the previous snapshot until validation is complete.

```sh
sysrc freebsd_lab_daemon_runtime_args="--jail-template=zroot/jails/templates/freebsd-python-<build-id>@clean"
service freebsd_lab_daemon restart
```

Do not destroy an older snapshot while active ZFS clones still depend on it.

## bhyve image

`build-bhyve-image.sh` drives the FreeBSD release `vm-image` target with raw/UFS output and `vmimage.conf`. The configuration adds Python, ipykernel, cloud-init, the `freebsd` account, and the restricted SSH policy before the image is unmounted.

Artifacts are versioned under `/var/db/freebsd-laboratory/images` by default:

```text
freebsd-python-<build-id>.raw
freebsd-python-<build-id>.raw.sha256
freebsd-python-<build-id>.manifest
freebsd-python.raw -> freebsd-python-<build-id>.raw
```

Import or copy the validated versioned raw image into the vm-bhyve image datastore under the name configured by `--vm-image` (currently `freebsd-python.raw`). Perform activation only when no running laboratory VM depends on the previous base image.

## SSH image policy

`sshd-freebsd-lab.conf` disables password/root login, agent/X11/tunnel forwarding and gateway ports. `AllowTcpForwarding local` remains enabled because all five Jupyter TCP channels are intentionally carried through local SSH forwards rather than exposed on `labbridge0`.

SSH host private keys are removed from the golden artifacts. Each instantiated jail or VM must generate its own host keys instead of inheriting the builder's identity.

## Rebuild triggers

Rebuild both artifacts whenever one of these changes:

- the selected FreeBSD `releng/*` revision advances for a security or errata fix;
- Python/ipykernel/cloud-init packages are updated;
- the laboratory SSH policy changes;
- the expected runtime dependency set changes.

Treat the source revision, package audit result, embedded image manifest and external SHA-256 file as release evidence. Keep the previous known-good artifacts until the new pair has completed a real VNET-jail and bhyve smoke test.

## CI runner

`.github/workflows/freebsd-images.yml` is a manual workflow for a dedicated self-hosted FreeBSD image builder. It intentionally does not run on normal pull requests because building world, ZFS snapshots and raw VM images requires a privileged FreeBSD host with local storage.

The ordinary Linux test workflow still syntax-checks all builder scripts so shell regressions are caught before the FreeBSD job is invoked.
