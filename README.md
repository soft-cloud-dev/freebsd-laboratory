# FreeBSD Laboratory

Executable FreeBSD documentation for JupyterLab. The project separates the notebook UI, server-owned evidence, and the runtime that actually executes a kernel.

## Current implementation

The prototype now contains three executable boundaries:

1. **JupyterLab extension** — adds the `Lab progression` right sidebar, records notebook execution observations, and adds `Export evidence` to notebook toolbars.
2. **Jupyter Server extension** — owns the evidence session, validates client event types, derives trust stages, and exports hashed evidence files.
3. **FreeBSD jail kernel provisioner** — clones a ZFS template snapshot, creates a disposable jail, mirrors the Jupyter connection file into it, and launches the kernel with `jexec`.

The browser is not treated as a trusted attestor. Events observed by the JupyterLab extension are explicitly marked `self-recorded`. Later stages require server-side machine events and cannot be asserted through the browser API.

## Development setup

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
jupyter server extension enable --py freebsd_laboratory --sys-prefix
freebsd-lab-install-kernel
cd labextension
npm install --no-audit --no-fund
npm run build
jupyter labextension develop . --overwrite
cd ..
jupyter lab
```

The Jupyter Server root must contain `lab.yaml`; the sample repository root already does.

## FreeBSD jail prerequisites

The jail provisioner fails closed unless the Jupyter Server host is FreeBSD and is running with the privileges required to create jails and ZFS clones.

The default kernelspec expects this template snapshot:

```text
zroot/jails/templates/freebsd-python@clean
```

The snapshot must contain a usable FreeBSD userspace with:

```text
/usr/local/bin/python3
ipykernel
```

Ephemeral clones are created below:

```text
zroot/jails/containers
/usr/local/jails/containers
```

Each kernel launch follows this lifecycle:

```text
kernel requested
  -> zfs clone template snapshot
  -> jail -c ... persist exec.clean mount.devfs
  -> mirror Jupyter connection file into jail
  -> jexec -l <jail> /usr/local/bin/python3 -m ipykernel_launcher ...
  -> kernel shutdown
  -> jail -r
  -> zfs destroy clone
```

The initial prototype uses `ip4=inherit` so the jailed kernel can communicate with the local Jupyter Server. That is a functional bridge, not the final network-isolation design. A dedicated private loopback/VNET transport is the next hardening step.

## Evidence API

Authenticated endpoints are mounted below the Jupyter Server base URL:

```text
GET  /freebsd-lab/api/state
POST /freebsd-lab/api/events
POST /freebsd-lab/api/export
```

Client POSTs are restricted to observation events (`notebook-context` and `cell-executed`). Machine trust-stage events are separate server-side operations.

Each Jupyter Server process creates a new evidence session in:

```text
.freebsd-lab/evidence/<session-id>/
```

An export currently contains:

```text
evidence.json
environment.json
events.jsonl
manifest.json
SHA256SUMS
```

## Tests

```sh
pytest -q
cd labextension && npm run build
```

Linux CI tests the portable evidence/state model and TypeScript compilation. The actual jail lifecycle requires a FreeBSD/ZFS runner and is intentionally not simulated by the standard CI job.
