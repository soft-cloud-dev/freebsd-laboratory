# Fresh FreeBSD bootstrap

`deploy/freebsd/bootstrap.sh` turns a new ZFS-backed FreeBSD installation into a usable FreeBSD Laboratory host without requiring the manual setup sequence documented elsewhere in this directory.

The bootstrap is FreeBSD-native. It uses `pkg` binary packages for JupyterLab, PyZMQ, cryptography, ipykernel, and related runtime dependencies instead of asking pip to compile those projects from PyPI. The repository package itself is installed with `--no-deps` into two separate virtual environments. Pip build isolation remains enabled so the `[build-system]` requirement from `pyproject.toml` is honored even when the FreeBSD system setuptools package is older.

The environments are separated deliberately:

- a root-owned daemon environment under `/usr/local/libexec/freebsd-laboratory/`;
- a Jupyter environment in `<repo>/.venv` owned by the configured Jupyter user after bootstrap completes.

This prevents the privileged runtime daemon from executing Python code out of the user-writable Jupyter checkout.

## Default bootstrap: official RELEASE userland

Run as root on a fresh FreeBSD RELEASE installation:

```sh
fetch -o /tmp/freebsd-lab-bootstrap.sh \
  https://raw.githubusercontent.com/soft-cloud-dev/freebsd-laboratory/main/deploy/freebsd/bootstrap.sh
chmod 0555 /tmp/freebsd-lab-bootstrap.sh
LAB_JUPYTER_USER=freebsd /tmp/freebsd-lab-bootstrap.sh
```

`LAB_JAIL_IMAGE_MODE=release` is the default. The bootstrap does **not** clone `/usr/src` and does **not** run `buildworld` in this mode.

The jail image pipeline is:

```text
official FreeBSD X.Y-RELEASE base.txz
  -> verify base.txz SHA-256 against the official release MANIFEST
  -> extract into a versioned ZFS dataset
  -> derive pkg ABI from the target jail userland
  -> refresh package catalogues for that target ABI
  -> pkg install python3 + matching ipykernel
  -> initialize target-root dynamic linker hints
  -> validate Python shared-library resolution and ipykernel
  -> create the freebsd runtime account
  -> install the restricted SSH policy
  -> remove inherited SSH host keys
  -> enforce pkg audit or exact VuXML exceptions
  -> write ABI/audit/release provenance manifest
  -> ZFS @clean snapshot
```

The release is derived from the host userland, for example `15.1-RELEASE-p2 -> 15.1-RELEASE`. The official distribution path is derived automatically on amd64 and aarch64. The downloaded `MANIFEST` and `base.txz` are cached under `/var/cache/freebsd-laboratory/releases`; the archive is reused only if its SHA-256 still matches the current official MANIFEST.

Package resolution is tied to the target image rather than the host builder. The builder points pkg's `ABI_FILE` at the target root's `/bin/sh`, forces a catalogue refresh, and records the resolved ABI in `image-manifest.json`. It then runs the target root's `ldconfig` service before `ldd` and ipykernel validation. This catches stale or cross-major packages without creating compatibility symlinks between shared-library major versions.

The resulting snapshot is mode-specific, for example:

```text
zroot/jails/templates/freebsd-python-release-20260819T014500Z@clean
```

The complete bootstrap configures the rc.d daemon, VNET bridge, PF anchor, SSH transport, activates that exact snapshot, waits for an authenticated runtime-daemon `ping`, and runs a real jail smoke test requiring both `security.jail.jailed=1` and `vnet0`.

If daemon readiness times out, bootstrap prints `service freebsd_lab_daemon status` and relevant `/var/log/messages` lines before failing. `LAB_DAEMON_READY_TIMEOUT` controls the default 30-second deadline.

## Optional bhyve backend

The default jail-only installation does not require `vm-bhyve`. The daemon skips bhyve discovery when `/usr/local/sbin/vm` is absent and reports a specific error only when a bhyve runtime is requested.

To install the alternative backend during bootstrap:

```sh
LAB_JUPYTER_USER=freebsd \
LAB_INSTALL_BHYVE_BACKEND=YES \
  ./deploy/freebsd/bootstrap.sh
```

A prepared `freebsd-python.raw` image and vm-bhyve template are still required before the bhyve kernel can launch.

## Package audit policy

The default is fail-closed:

```text
LAB_FAIL_ON_PKG_AUDIT=YES
```

When the repository has no fixed package yet, accept only explicit FreeBSD VuXML records:

```sh
LAB_PKG_AUDIT_ALLOWED_VULN_IDS="<uuid-1> <uuid-2>" \
LAB_JUPYTER_USER=freebsd \
LAB_REBUILD_JAIL_IMAGE=YES \
  ./deploy/freebsd/bootstrap.sh
```

The parser tolerates indentation and hostname-case differences in `pkg audit` output, but every reported problem must map one-to-one to an exact allowlisted ID. Any extra or unparseable finding stops the build. Audit enforcement state and accepted IDs are written to the image manifest.

`LAB_FAIL_ON_PKG_AUDIT=NO` is diagnostic-only. It is forwarded to both release and source builders and recorded as `pkg_audit_enforced: false` in provenance.

## Source/reproducible bootstrap mode

Use source mode when the jail userland must originate from an explicit FreeBSD `releng/X.Y` source branch rather than the official RELEASE distribution set:

```sh
LAB_JUPYTER_USER=freebsd \
LAB_JAIL_IMAGE_MODE=source \
  ./deploy/freebsd/bootstrap.sh
```

The source pipeline is:

```text
git checkout releng/X.Y
  -> make buildworld
  -> make installworld DESTDIR=<ZFS jail root>
  -> make distribution DESTDIR=<ZFS jail root>
  -> resolve packages against the target userland ABI
  -> pkg install python3 + matching ipykernel
  -> initialize target-root dynamic linker hints
  -> validate Python/ipykernel and audit policy
  -> SSH/runtime configuration
  -> provenance manifest with exact source revision
  -> ZFS @clean snapshot
```

This mode is intentionally slower and requires a FreeBSD source checkout. `LAB_SRC_DIR` defaults to `/usr/src`. If `/usr/src` already exists as an empty mounted ZFS dataset, the bootstrap clones directly into that directory; it does not remove or unmount the dataset. An existing non-empty path must already be a Git checkout or bootstrap fails closed.

The resulting source-built snapshot is separate from the default RELEASE snapshot:

```text
zroot/jails/templates/freebsd-python-source-20260819T014500Z@clean
```

The source-built userland is tied to the exact revision recorded in the image manifest. Reproducibility of third-party runtime packages additionally depends on the configured pkg repository state; use a controlled repository through `LAB_PKG_REPOS_DIR` when package-level reproducibility is required.

## Existing image reuse

Bootstrap reuses the newest `@clean` snapshot for the selected mode unless `LAB_REBUILD_JAIL_IMAGE=YES` is set. Release and source snapshots are never selected interchangeably.

To force a new default RELEASE image:

```sh
LAB_JUPYTER_USER=freebsd \
LAB_REBUILD_JAIL_IMAGE=YES \
  ./deploy/freebsd/bootstrap.sh
```

To force a new source-built image:

```sh
LAB_JUPYTER_USER=freebsd \
LAB_JAIL_IMAGE_MODE=source \
LAB_REBUILD_JAIL_IMAGE=YES \
  ./deploy/freebsd/bootstrap.sh
```

## Host-only bootstrap

To prepare Jupyter, the daemon, networking, and PF without producing or activating a jail image:

```sh
LAB_JUPYTER_USER=freebsd \
LAB_BUILD_JAIL_IMAGE=NO \
LAB_SMOKE_TEST=NO \
  ./deploy/freebsd/bootstrap.sh
```

The VNET jail kernelspec is installed, but it cannot start until a valid jail template is activated.

## Important overrides

Environment variables are preferred over editing the script:

```text
LAB_JUPYTER_USER         existing account that runs JupyterLab
LAB_REPO_DIR             checkout; defaults to <user-home>/freebsd-laboratory
LAB_REPO_REF             Git branch to clone; default main
LAB_UPDATE_REPO          YES to fast-forward an existing checkout
LAB_ZFS_POOL             auto-selects zroot or the only available pool
LAB_DAEMON_READY_TIMEOUT positive seconds; default 30
LAB_INSTALL_BHYVE_BACKEND
                         YES to install vm-bhyve; default NO
LAB_JAIL_IMAGE_MODE      release (default) or source
LAB_BUILD_JAIL_IMAGE     YES by default
LAB_REBUILD_JAIL_IMAGE   YES to build a new mode-specific snapshot
LAB_SMOKE_TEST           YES by default when a jail image is active
LAB_FAIL_ON_PKG_AUDIT    YES by default
LAB_PKG_AUDIT_ALLOWED_VULN_IDS
                         exact space-separated FreeBSD VuXML IDs

Release mode:
LAB_RELEASE              official X.Y-RELEASE; normally derived from host
LAB_RELEASE_BASE_URL     default https://download.freebsd.org/releases
LAB_RELEASE_TARGET       first release-path architecture component
LAB_RELEASE_TARGET_ARCH  second release-path architecture component
LAB_RELEASE_CACHE_DIR    default /var/cache/freebsd-laboratory/releases

Source mode:
LAB_SRC_DIR              source checkout; default /usr/src
LAB_SRC_BRANCH           explicit releng/* branch; normally derived

Networking/security:
LAB_CONFIGURE_PF         YES by default
LAB_BRIDGE_CLONE         persistent bridge cloner; default bridge0
LAB_BRIDGE_NAME          laboratory bridge; default labbridge0
LAB_NETWORK              default 172.31.254.0/24
LAB_HOST_ADDRESS         default 172.31.254.1
LAB_ADDRESS_START        default 172.31.254.10
LAB_ADDRESS_END          default 172.31.254.199
```

The script backs up an existing `/etc/pf.conf`, validates the candidate ruleset with `pfctl -nf`, places the laboratory anchor before the prior ruleset, and only then reloads PF.

The runtime socket is group-accessible but peer-authenticated. FreeBSD `LOCAL_PEERCRED` binds create requests to the actual caller PID/UID and restricts destructive operations to the runtime owner or root.

## Starting JupyterLab

Bootstrap prints the exact command for the configured account. A typical installation is:

```sh
su - freebsd
cd ~/freebsd-laboratory
.venv/bin/jupyter lab \
  --ServerApp.root_dir="$(pwd -P)" \
  --ip=0.0.0.0 \
  --no-browser
```

A new login is required after bootstrap so a non-root account receives its new `freebsdlab` supplementary-group membership.
