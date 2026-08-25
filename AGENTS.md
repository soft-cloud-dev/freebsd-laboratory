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

## Security Invariants

- **Anti-symlink policy**: Every file open, write, delete, or directory
  creation must check `.is_symlink()` and refuse to follow symlinks. Use
  `follow_symlinks=False` on `os.chmod` / `os.chown`. This applies to keys,
  connection files, manifests, evidence bundles, registry records, lease
  files, and socket paths.
- **Bool-before-int type checking**: Always check `isinstance(v, bool)`
  before `isinstance(v, int)` in validation code, because `bool` is a
  subclass of `int` in Python.
- **Atomic file writes**: Use temp file + `os.fsync` + `os.chmod` +
  atomic `os.replace` for all persistent state (registry records, leases,
  connection files, evidence). Never write in place.
- **Filesystem permissions model**:
  - Keys, connection files, event logs, registry records: `0600`
  - Runtime, registry, evidence directories: `0700`
  - Socket directory: `0750` (`root:freebsdlab`)
  - Socket and lease lock: `0660` (`root:freebsdlab`)
- **Never bypass `runtime.sock`**: Jupyter runs unprivileged. Never execute
  `jail`, `zfs`, `ifconfig`, `vm`, or `bhyve` commands directly from the
  provisioner or server extension. All lifecycle operations go through the
  Unix socket to the root-owned daemon.

## Runtime Daemon Protocol

- The daemon uses single-line newline-delimited JSON over Unix socket.
  Max request: 64 KB. Max response: 1 MB.
- Response format: `{"ok": true, "result": {...}}` or
  `{"ok": false, "error": "<msg>"}`.
- Actions: `ping`, `create-jail`, `create-bhyve`, `destroy`, `gc`.
- Runtime names must match `^freebsd-lab-[a-z0-9]{1,16}$`. Generated from
  kernel IDs via `runtime_name(kernel_id)`.
- SSH public keys must be single-line Ed25519 (`ssh-ed25519 ` prefix,
  68-character base64 blob). Multi-line or RSA keys are rejected.
- Owner identity uses PID + UID + `sha256("{uid}:{lstart}")` fingerprint
  from `ps -o uid= -o lstart=`. Bare PID is never authoritative due to
  recycling.

## Evidence & Telemetry

- **Never persist raw notebook cell source code.** The evidence stream
  records only `source_sha256` and `source_bytes`, never the actual code.
- Client events (`cell-executed`, `notebook-context`) come from the browser.
  Machine events (`reproduction-complete`, etc.) are server-only. The API
  rejects machine-stage events from browser clients.
- All payloads are recursively redacted: keys matching `authorization`,
  `cookie`, `token`, `password`, `secret`, `api_key`, `private_key` are
  replaced with `[REDACTED]`.
- Resource limits: max 10,000 events (HTTP 429), max 1 MB payload (HTTP 413).
- Sentry telemetry is opt-in (`SENTRY_DSN`). PII scrubbing is always on:
  `send_default_pii=False`, no local variables, no source context. Kernel
  errors report only the exception class name, never tracebacks or code.

## Test Conventions

- Tests use `PortableRuntimeManager(RuntimeManager)` to stub FreeBSD-only
  commands (`jexec`, `zfs`, `ifconfig`, `vm`) while running all real Python
  logic on macOS/Linux CI.
- Symlink resistance: every filesystem operation has explicit tests asserting
  refusal to follow symlinks.
- Type guard tests: assert rejection of `bool` values in `int` fields.
- Concurrency tests: use `ThreadPoolExecutor` + `threading.Barrier` for
  parallel port lease allocation races.
- Test naming: `test_<component>_<behavior>_<expectation>`.
- **When adding new filesystem or security-sensitive code, always add
  matching symlink-resistance and type-guard tests.**

## Git & PR Workflow

- Create feature branches like `fix/<descriptive-name>`.
- Use conventional commit messages: `fix:`, `feat:`, etc.
- **Git Hooks & Pre-commit Invariants**:
  - `pre-commit`: Enforces clean whitespace (`git diff --check`) and strictly forbids non-ASCII filenames. Ensure no trailing whitespace or whitespace errors exist before committing.
  - `pre-push`: Rejects pushes containing commit messages starting with `WIP` (Work In Progress). Use proper conventional commit subjects instead.
- Create PRs via `gh pr create` targeting `main`.
