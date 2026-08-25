---
name: freebsd-lab-services
description: >-
  Use this skill when restarting, debugging, or verifying the FreeBSD
  laboratory services on the remote host (192.168.1.111). Covers the full
  sequence for restarting the runtime daemon and JupyterLab, reinstalling
  the editable package, and verifying service health.
---

# FreeBSD Laboratory Services Management

## Service Architecture

| Service | Managed by | User | Port/Socket |
|---------|-----------|------|-------------|
| `freebsd_lab_daemon` | rc script `/usr/local/etc/rc.d/freebsd_lab_daemon` | root | `/var/run/freebsd-laboratory/runtime.sock` |
| JupyterLab | `daemon(8)` via `start-jupyter.sh` | freebsd | `:8888` |

Both services consume the `freebsd-laboratory` Python package from their own venvs:
- Daemon venv: `/usr/local/libexec/freebsd-laboratory/daemon-venv`
- Jupyter venv: `/home/freebsd/freebsd-laboratory/.venv`

## Full Clean Restart Sequence

Run these steps **in order** on the remote host (via osascript or direct SSH):

### Step 1 — Reinstall editable package in both venvs

```sh
/usr/local/libexec/freebsd-laboratory/daemon-venv/bin/pip install -e /home/freebsd/freebsd-laboratory
/home/freebsd/freebsd-laboratory/.venv/bin/pip install -e /home/freebsd/freebsd-laboratory
```

### Step 2 — Restart the runtime daemon

```sh
service freebsd_lab_daemon restart
```

Verify:
```sh
ls -la /var/run/freebsd-laboratory/runtime.sock
# Expected: srw-rw---- 1 root freebsdlab
```

### Step 3 — Kill ALL stale Jupyter and ipykernel processes

```sh
killall -9 python python3 jupyter-lab 2>/dev/null || true
pkill -u freebsd -f jupyter || true
pkill -u freebsd -f ipykernel_launcher || true
sleep 1
```

> **Why**: `daemon -f -p <pidfile>` blocks if the pidfile already exists from a
> still-running process. Always kill first.

### Step 4 — Start JupyterLab via the persistent launcher script

```sh
su - freebsd -c "/usr/sbin/daemon -f -p /tmp/jupyter.pid /home/freebsd/freebsd-laboratory/start-jupyter.sh"
```

The launcher script (`start-jupyter.sh`) is created during bootstrap and contains:
```sh
#!/bin/sh
cd /home/freebsd/freebsd-laboratory
export SENTRY_DSN='https://f9bdddd3d28ea5110c58e7ca6a534307@o4504684341428224.ingest.us.sentry.io/4511937663991808'
export SENTRY_ENVIRONMENT='lab'
exec .venv/bin/jupyter-lab --no-browser --ip=0.0.0.0 --port=8888 \
    --ServerApp.root_dir=/home/freebsd/freebsd-laboratory \
    --IdentityProvider.token='' --ServerApp.token='' --ServerApp.password=''
```

If the file doesn't exist yet, recreate it with `cat > /home/freebsd/freebsd-laboratory/start-jupyter.sh << 'EOF' ... EOF`.

### Step 5 — Verify both services

```sh
sockstat -4 -l -p 8888          # expect: freebsd python3.12 *:8888
ls -la /var/run/freebsd-laboratory/runtime.sock   # expect: srw-rw---- root freebsdlab
curl -s http://127.0.0.1:8888/api/status          # expect: {"connections":0,"kernels":0,...}
```

## Checking Process State

```sh
ps aux | grep -E "freebsd_lab_daemon|jupyter|daemon"
```

Look for:
- `daemon: freebsd-lab-runtime-daemon[NNNN]` — rc-managed daemon
- `daemon: start-jupyter.sh[NNNN]` — Jupyter supervisor
- `python ... jupyter-lab ...` — actual Jupyter worker

## Common Failure: `daemon: process already running`

If you see `daemon: process already running, pid: NNNN`:
1. Kill the old process: `kill -9 NNNN`
2. Remove the stale pidfile: `rm -f /tmp/jupyter.pid`
3. Retry Step 4.
