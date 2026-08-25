# FreeBSD Laboratory — Agent Guidelines

## Project Overview

This is a Jupyter kernel provisioner for FreeBSD. It provisions VNET jails and
bhyve VMs as Jupyter kernel runtimes, connected via SSH tunnels over a private
bridge network. See `ARCHITECTURE.md` for the full trust model.

## Remote Host Workflow

- The FreeBSD host is accessed via SSH from macOS (typically `ssh root@192.168.1.111`).
- The repository lives at `/home/freebsd/freebsd-laboratory` on the host.
- The Python virtualenv is at `/home/freebsd/freebsd-laboratory/.venv`.
- JupyterLab runs under user `freebsd` on port `8888`.
- The runtime daemon socket is at `/var/run/freebsd-laboratory/runtime.sock`.
- When running commands on the remote host through Terminal.app, use `osascript`
  to send commands to the active SSH tab, then read back terminal contents.

## Build System Conventions

### Golden Image (bhyve)

- FreeBSD 15.x uses **`nuageinit`** (not Python `cloudinit`) for NoCloud
  first-boot metadata. The vm-bhyve template seeds SSH keys and network config
  through nuageinit's NoCloud datasource.
- Package installations inside `vmimage.conf` must use `INSTALL_AS_USER=yes`
  and `-o METALOG="${DESTDIR}/METALOG.pkg"` so that all installed files are
  tracked by makefs and included in the final raw UFS image.
- Run `ldconfig forcestart` (via `vm_refresh_ldconfig` or
  `refresh_target_ldconfig`) after every package installation phase. Without
  this, `/var/run/ld-elf.so.hints` won't include `/usr/local/lib` and shared
  libraries like `libpython3.12.so.1` won't be found.
- Unset `MAKEFLAGS` before any nested make invocations (e.g., `etcupdate
  extract`) inside `mk-vmimage.sh` callbacks to prevent recursive flag leakage
  from the parent release build.
- The builder auto-detects host architecture from `uname -m` (supports
  `amd64` and `arm64/aarch64`). Always pass `TARGET` and `TARGET_ARCH` to
  `make -V .OBJDIR` when resolving the release object directory.
- `pkg` bootstrap from ports requires `MAKE_ARGS=mandir=/usr/local/share/man`
  to match the port's packing list expectations.

### Golden Image (jail)

- Source-based jail images use `build-jail-template.sh`.
- The `refresh_target_ldconfig` helper must be called after each
  `pkg_root install` phase (currently called 3 times total).

## Test Contracts

- All shell script behaviors are verified by Python contract tests in `tests/`.
- `test_golden_image_python_package_contract.py` asserts expected strings
  and counts in `vmimage.conf`, `build-bhyve-image.sh`, and
  `build-jail-template.sh`.
- `test_golden_image_objdir_contract.py` verifies architecture detection,
  object directory isolation, and ports requirements.
- `test_bootstrap.py` verifies the bootstrap script's package lists and
  conditional logic.
- **When modifying any build shell script, always update the corresponding
  contract test assertions.**
- Run the full test suite with `.venv/bin/python -m pytest -v` on the
  FreeBSD host. All tests must pass before committing.

## SSH Transport

- `SSHTransport.wait_until_ready()` retries SSH probes within the overall
  `startup_timeout` window. Individual probe timeouts and `RuntimeError`
  from timed-out commands are caught and retried, not propagated.
- Default `startup_timeout` is 90s; `ssh_connect_timeout` is 5s.
- The first bhyve VM on the private bridge gets IP `172.31.254.10`.

## Git & PR Workflow

- Create feature branches like `fix/<descriptive-name>`.
- Use conventional commit messages: `fix:`, `feat:`, etc.
- Create PRs via `gh pr create` targeting `main`.
