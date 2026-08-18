# Fresh FreeBSD bootstrap

`deploy/freebsd/bootstrap.sh` turns a new ZFS-backed FreeBSD installation into a usable FreeBSD Laboratory host without requiring the manual setup sequence documented elsewhere in this directory.

The bootstrap is intentionally FreeBSD-native. It uses `pkg` binary packages for JupyterLab, PyZMQ, cryptography, ipykernel, and related runtime dependencies instead of asking pip to compile those projects from PyPI. The repository package itself is installed with `--no-deps` into two separate virtual environments. Pip build isolation remains enabled so the `[build-system]` requirement from `pyproject.toml` (`setuptools>=77`) is honored even when the FreeBSD system setuptools package is older; only the lightweight build backend is isolated, while heavy runtime dependencies continue to come from FreeBSD packages.

- a root-owned daemon environment under `/usr/local/libexec/freebsd-laboratory/`;
- a Jupyter environment in `<repo>/.venv` owned by the configured Jupyter user after bootstrap completes.

This separation prevents the privileged runtime daemon from executing Python code out of the user-writable Jupyter checkout.

## Complete bootstrap

Run as root on a fresh installation:

```sh
fetch -o /tmp/freebsd-lab-bootstrap.sh \
  https://raw.githubusercontent.com/soft-cloud-dev/freebsd-laboratory/main/deploy/freebsd/bootstrap.sh
chmod 0555 /tmp/freebsd-lab-bootstrap.sh
LAB_JUPYTER_USER=freebsd /tmp/freebsd-lab-bootstrap.sh
```

If the host does not have a `freebsd` account, either set `LAB_JUPYTER_USER` to an existing account or omit it and the script falls back to root.

The default path is complete rather than fast. It:

1. bootstraps `pkg` and installs Git, Python, npm, JupyterLab, ipykernel, cryptography, pytest, and PyYAML from FreeBSD packages;
2. clones or reuses the laboratory repository;
3. creates the `freebsdlab` operator group and SSH transport key;
4. creates the ZFS template/container parents;
5. installs the root runtime daemon into an isolated root-owned virtual environment;
6. creates the Jupyter virtual environment, builds the TypeScript extension, enables the server extension, and installs both kernelspecs;
7. derives the matching FreeBSD source branch from the installed release, for example `15.1-RELEASE -> releng/15.1`, and checks out `/usr/src`;
8. runs `buildworld` and creates a versioned `freebsd-python-<build-id>@clean` jail template when no reusable template is present;
9. installs the rc.d service, bridge settings, PF anchor, and tunnel-port infrastructure;
10. starts the runtime daemon and performs a real VNET jail smoke test that requires `security.jail.jailed=1` and `vnet0` to exist.

The `buildworld` phase is expected to dominate bootstrap time. On a host that already has a valid versioned jail template, the existing latest `freebsd-python-*@clean` snapshot is reused unless rebuilding is explicitly requested.

## Faster host-only bootstrap

To prepare Jupyter, the daemon, networking, and PF without building a jail image yet:

```sh
LAB_JUPYTER_USER=freebsd \
LAB_BUILD_JAIL_IMAGE=NO \
LAB_SMOKE_TEST=NO \
  /tmp/freebsd-lab-bootstrap.sh
```

The VNET jail kernelspec will be installed, but it cannot start until a valid jail template is activated.

## Important overrides

Environment variables are preferred over editing the script:

```text
LAB_JUPYTER_USER       existing account that runs JupyterLab
LAB_REPO_DIR           repository checkout; defaults to <user-home>/freebsd-laboratory
LAB_REPO_REF           Git branch to clone; default main
LAB_UPDATE_REPO        YES to fast-forward an existing checkout
LAB_ZFS_POOL           ZFS pool; auto-selects zroot or the only available pool
LAB_SRC_DIR            FreeBSD source checkout; default /usr/src
LAB_SRC_BRANCH         explicit releng/* branch; normally derived automatically
LAB_BUILD_JAIL_IMAGE   YES by default
LAB_REBUILD_JAIL_IMAGE YES to build a new versioned template even if one exists
LAB_SMOKE_TEST         YES by default when a jail image is active
LAB_CONFIGURE_PF       YES by default
LAB_BRIDGE_CLONE       persistent bridge cloner; default bridge0
LAB_BRIDGE_NAME        laboratory bridge; default labbridge0
LAB_NETWORK            default 172.31.254.0/24
LAB_HOST_ADDRESS       default 172.31.254.1
LAB_ADDRESS_START      default 172.31.254.10
LAB_ADDRESS_END        default 172.31.254.199
```

The script backs up an existing `/etc/pf.conf` before changing it, validates the candidate ruleset with `pfctl -nf`, places the laboratory anchor before the prior ruleset, and only then reloads PF.

## Starting JupyterLab

The bootstrap prints the exact command for the configured account. A typical installation is:

```sh
su - freebsd
cd ~/freebsd-laboratory
.venv/bin/jupyter lab \
  --ServerApp.root_dir="$(pwd -P)" \
  --ip=0.0.0.0 \
  --no-browser
```

A new login is required after bootstrap so a non-root account receives its new `freebsdlab` supplementary-group membership.
