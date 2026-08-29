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
- **`osascript` quoting rule**: Never embed multi-line shell scripts or scripts
  containing double-quotes directly inside `do script "..."`. Instead, write the
  script to a file on the remote host first, then pass the path:
  ```sh
  # WRONG — breaks when inner script has double-quotes:
  osascript -e 'tell application "Terminal" to do script "VAR=\"value\" && cmd" ...'

  # RIGHT — write script to file first, then execute by path:
  osascript -e 'tell application "Terminal" to do script "cat > /tmp/run.sh << EOF\n#!/bin/sh\n...\nEOF\nsh /tmp/run.sh" in window id NNNN'
  ```
  For JupyterLab specifically, use `/home/freebsd/freebsd-laboratory/start-jupyter.sh`
  as the persistent launcher script (created during bootstrap).
- **Sleep & Polling Timeout Efficiency**:
  - Never introduce artificial sleep delays or `schedule` timers between standard `osascript` reads or fast operations.
  - Reserve sleep timers exclusively for heavy, CPU-intensive operations (e.g., compiling Linux/FreeBSD kernels, formatting/populating raw UFS/ext4 disk images, or building packages).
  - For quick terminal reads or status queries, inspect output immediately.
  - **Single Realistic Timer Duration**: Estimate the expected runtime for heavy operations upfront and use a single realistic duration rather than chaining multiple short 10s/15s timers in a loop.
  - **Active Sleep Activation**:
    - Proactively schedule a single one-shot timer (`schedule(DurationSeconds=N, Prompt="...", TimerCondition="never"|"any")`) when launching multi-step benchmarks, builds, package installations, or VM operations with predictable runtimes, rather than performing repetitive intermediate `osascript` terminal reads.
    - This drastically saves tokens, prevents terminal thrashing, and yields execution until the operation has completed or reached its expected checkpoint.
    - When the user explicitly commands `Sleep.` or `sleep`, immediately schedule a realistic one-shot timer, acknowledge with a single concise message, and end the turn without intermediate polling.
  - **Leverage `TimerCondition`**: When setting a `schedule` timer to monitor background tasks or subagents, specify `TimerCondition: "<task-id>"` or `TimerCondition: "any"` so the timer cancels early as soon as a relevant update or notification arrives.

## Local Development & Labextension Workflow

- **Local vs. Remote Boundary**:
  - Labextension frontend code (`labextension/src/`, `labextension/style/`), UI styles, contract assertions, and documentation changes are developed, edited, and validated locally in the workspace.
  - Do not attempt to SSH to the remote host for frontend/UI tasks unless explicitly asked to restart services or perform host-level system diagnostics.
- **Local Python Contract Testing**:
  - Python contract tests in `tests/` verify image build scripts, UI geometry, and package configurations without requiring remote host access.
  - When `pytest` is unavailable in the local environment, run contract tests using Python's standard library `unittest` runner:
    ```sh
    python3 -c "
    import unittest
    suite = unittest.TestSuite()
    for mod_name in ['tests.test_ui_reference_contract', 'tests.test_labextension_contract', 'tests.test_bootstrap', 'tests.test_golden_image_python_package_contract', 'tests.test_golden_image_linux_contract', 'tests.test_golden_image_objdir_contract']:
        mod = __import__(mod_name, fromlist=['*'])
        for name in dir(mod):
            if name.startswith('test_') and callable(getattr(mod, name)):
                fn = getattr(mod, name)
                class C(unittest.TestCase): pass
                setattr(C, name, lambda self, f=fn: f())
                suite.addTest(C(name))
    runner = unittest.TextTestRunner(verbosity=2)
    assert runner.run(suite).wasSuccessful()
    "
    ```
- **Labextension NPM Build Structure**:
  - Source files: `labextension/src/index.ts` (TypeScript) and `labextension/style/index.css` (CSS) are tracked in git.
  - Build script: `npm run build` executes `npm run build:lib` (`tsc`) followed by `npm run build:labextension` (`jupyter-builder build .`).
  - Keep `src/index.ts` and `lib/index.js` synchronized when modifying extension logic.

## OS Distribution Family & Laboratory Architecture

The FreeBSD Laboratory framework separates the **Operating System Distribution under study** from the **Execution Mechanism**:
- **Distribution Repositories (`soft-cloud-dev/os-*`)**:
  - `os-freebsd`: Builds native FreeBSD VNET jail templates and bhyve raw disk images.
  - `os-linux`: Builds Linux EFI stub kernels (`kernel/build.sh`), Alpine/Debian rootfs, bhyve raw disk images (`runtime/bhyve/build-image.sh`), and Linuxulator targets.
  - `os-laboratory-template`: Defines canonical JSON schemas (`schemas/artifact-v1.schema.json`, `schemas/os-v1.schema.json`), directory contracts, and MyST publication structures.
- **Executor Repository (`soft-cloud-dev/freebsd-laboratory`)**:
  - Acts as a pure execution engine (Jupyter provisioners, Unix socket runtime daemon, port lease manager, SSH transport, server extension).
  - Ingests declarative `softcloud.artifact/v1` manifests via `ArtifactStore` (`freebsd_laboratory/artifact_store.py`) to resolve host-specific storage (ZFS datasets, disk image paths, zvols).

## Build System Conventions & Artifact Contracts

### Golden Image Artifact Contracts (`softcloud.artifact/v1`)

- OS distribution builders emit declarative artifact manifests (`artifact-manifest.json`).
- FreeBSD 15.x uses **`nuageinit`** (not Python `cloudinit`) for NoCloud first-boot metadata in `os-freebsd`.
- Package installations in `os-freebsd/runtime/bhyve/vmimage.conf` use `INSTALL_AS_USER=yes` and `-o METALOG="${DESTDIR}/METALOG.pkg"`.
- Target ldconfig linker hints (`/var/run/ld-elf.so.hints`) must include `/usr/local/lib` after package phases.
- `freebsd-laboratory` never hardcodes internal build script paths of external `os-*` distributions; it consumes validated artifact manifests.

## Test Contracts

- All shell script behaviors in `os-freebsd` and `os-linux` are verified by contract tests in their respective repositories.
- `test_artifact_store.py` in `freebsd-laboratory` validates `softcloud.artifact/v1` manifest ingestion, storage resolution, SHA-256 digest integrity, symlink rejection, and capability matching.
- Run the full test suite with `.venv/bin/python -m pytest -v` on the FreeBSD host. All tests must pass before committing.

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
