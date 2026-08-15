# Implementation architecture

## Governing rule

The browser reports observations. It does not manufacture platform-verified claims.

```text
JupyterLab
  |  NotebookActions execution observations
  |  authenticated REST
  v
FreeBSD Laboratory server extension
  |-- evidence session
  |-- event validation
  |-- progression derivation
  `-- evidence export + SHA-256 manifest
  |
  | Jupyter kernel lifecycle
  v
FreeBSDJailProvisioner
  |-- ZFS clone
  |-- jail(8)
  |-- connection-file mirror
  `-- jexec(8)
       |
       v
Disposable FreeBSD jail
       `-- Python / ipykernel
```

## Implemented boundaries

### JupyterLab extension

`labextension/src/index.ts` adds the right-side progression surface and an `Export evidence` notebook-toolbar action. It observes `NotebookActions.executed` and records the serialized cell, execution success state, notebook path, and cell id.

Those events are useful evidence but remain browser-originated. The server labels them `jupyterlab-observer` and exposes the session attestation as `self-recorded`.

### Jupyter Server extension

`freebsd_laboratory/app.py` registers authenticated endpoints:

```text
/freebsd-lab/api/state
/freebsd-lab/api/events
/freebsd-lab/api/export
```

`freebsd_laboratory/service.py` owns the append-only session event stream. The browser API only accepts `notebook-context` and `cell-executed`; it cannot submit `verification-complete`, `recovery-complete`, or other machine-stage events.

Progression is derived from evidence rather than from manually toggled UI state.

### Kernel provisioner

`freebsd_laboratory/provisioner.py` is a `LocalProvisioner` specialization. It requires a real FreeBSD host and root privileges, clones a declared ZFS snapshot, creates a persistent jail, mirrors the Jupyter connection file at the same absolute path inside the jail, and wraps the generated kernel command in `jexec -l`.

The lifecycle is fail-closed on non-FreeBSD hosts. Runtime creation errors trigger jail/dataset cleanup rather than falling back to a local host kernel.

## Trust model

The current trust levels are deliberately asymmetric:

| Stage | Current derivation | Trust source |
|---|---|---|
| Observed | at least one `cell-executed` event | self-recorded browser observation |
| Explained | notebook context contains markdown | self-recorded browser observation |
| Reproduced | `reproduction-complete` | server-only machine event |
| Modified | `mutation-applied` | server-only machine event |
| Verified | `verification-complete` | server-only machine event |
| Recovered | `recovery-complete` | server-only machine event |
| Designed | `design-validated` | server-only machine event |

The later five event producers are not implemented yet. This prevents the UI from showing those stages as complete before there is an actual verifier/reproducer behind them.

## Jail runtime

The bundled kernelspec registers `freebsd-jail-provisioner` and expects a ZFS template snapshot containing `/usr/local/bin/python3` and `ipykernel`.

A kernel launch performs:

```text
zfs clone
  -> jail -c
  -> copy connection file into jail root
  -> jexec -l <jail> <kernel command>
```

Shutdown performs:

```text
kernel process ends
  -> jail -r
  -> zfs destroy -r <ephemeral clone>
```

The prototype currently uses `ip4=inherit` to preserve local Jupyter kernel connectivity. This is not the target security boundary. The production design should move kernel traffic onto a dedicated private loopback or VNET interface and add explicit resource controls.

## Evidence bundle

A server-owned export currently produces:

```text
evidence/
`-- <session-id>/
    |-- evidence.json
    |-- environment.json
    |-- events.jsonl
    |-- manifest.json
    `-- SHA256SUMS
```

`lab.yaml` itself is hashed into the evidence document. Payload hashes are calculated by the server after receipt.

## Next implementation slice

1. Add a FreeBSD CI/self-hosted runner that creates and destroys a real jail kernel.
2. Bind kernel lifecycle/runtime identity events directly into the server evidence stream.
3. Execute the notebook twice in clean clones and emit `reproduction-complete` only when the declared comparison policy passes.
4. Implement `checks:` from `lab.yaml` as server-side assertions and emit `verification-complete` from their results.
5. Replace `ip4=inherit` with an isolated kernel transport.
6. Include executed notebook and per-output artifacts in the exported bundle.
