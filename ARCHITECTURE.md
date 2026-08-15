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
Kernel provisioner
  |
  +--> FreeBSDJailProvisioner
  |      |-- ZFS clone
  |      |-- jail(8)
  |      |-- connection-file mirror
  |      `-- jexec(8)
  |            |
  |            v
  |      Disposable FreeBSD jail
  |            `-- Python / ipykernel
  |
  `--> FreeBSDBhyveProvisioner
         |-- vm-bhyve create/start
         |-- private IPv4 lease
         |-- cloud-init network + SSH key
         |-- connection-file rebind + SCP
         `-- attached SSH process
                |
                v
         Disposable bhyve VM
                `-- FreeBSD + Python / ipykernel
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

### Kernel runtime boundary

The project now exposes two Jupyter kernel provisioners:

```text
freebsd-jail-provisioner   -> same-kernel FreeBSD jail
freebsd-bhyve-provisioner  -> separate-kernel FreeBSD VM
```

The jail runtime remains the default for ordinary userspace laboratories. The bhyve runtime is the alternative when an experiment needs a separate kernel, boot lifecycle, virtual hardware, privileged networking, or a stronger isolation boundary.

Both provisioners fail closed outside a real FreeBSD host. Neither silently falls back to execution on the Jupyter Server host.

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

`freebsd_laboratory/provisioner.py` is a `LocalProvisioner` specialization. It requires a real FreeBSD host and root privileges, clones a declared ZFS snapshot, creates a persistent jail, mirrors the Jupyter connection file at the same absolute path inside the jail, and wraps the generated kernel command in `jexec -l`.

The lifecycle is fail-closed on non-FreeBSD hosts. Runtime creation errors trigger jail/dataset cleanup rather than falling back to a local host kernel.

The bundled `freebsd-python` kernelspec expects a ZFS template snapshot containing `/usr/local/bin/python3` and `ipykernel`.

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

The prototype currently uses `ip4=inherit` to preserve local Jupyter kernel connectivity. This is not the target security boundary. The production jail design should move kernel traffic onto a dedicated private loopback or VNET interface and add explicit resource controls.

## bhyve runtime

`freebsd_laboratory/bhyve.py` is a second `LocalProvisioner` specialization. It uses vm-bhyve to create and destroy one FreeBSD VM per Jupyter kernel while preserving Jupyter's normal process lifecycle through an attached SSH client.

The bundled `freebsd-python-bhyve` kernelspec points at `freebsd-bhyve-provisioner` and uses message-based interrupts because POSIX signals sent to the local SSH client are not a reliable kernel-interrupt transport.

### Host-to-guest transport

Each kernel receives a unique IPv4 address from a file-backed, `flock(2)`-protected lease pool. The default transport is:

```text
network:       172.31.254.0/24
host switch:   172.31.254.1/24
kernel leases: 172.31.254.100-172.31.254.199
```

No gateway is configured by default. This makes the initial design a host-to-guest transport network rather than a general guest egress network.

The vm-bhyve template uses a virtio NIC attached to the `freebsdlab` switch. `vm create` receives cloud-init arguments for the unique address and the laboratory SSH public key.

### Connection-file rebind

A jail can share the host IP namespace; a VM cannot. Before launching the bhyve kernel, the provisioner therefore rewrites the Jupyter connection document's `ip` field from the server-side address to the VM's leased address.

The resulting sequence is:

```text
LocalProvisioner prepares kernel command + connection file
  -> reserve VM address
  -> vm create/start guest
  -> wait for SSH
  -> rewrite connection-file IP to guest address
  -> create private remote connection-file directory
  -> SCP connection file to guest
  -> replace host connection-file argv with guest path
  -> SSH <guest> <kernel command>
```

The local SSH process remains attached to the remote ipykernel process. If the guest disappears, the SSH process exits and Jupyter observes the kernel process failure. On cleanup the VM is forcibly powered off if necessary, destroyed, and its address lease is released.

### bhyve lifecycle ownership

The provisioner generates the VM name from the Jupyter kernel id and checks for an existing guest before creation. It refuses to intentionally replace a pre-existing vm-bhyve guest with the same generated name.

Per-kernel SSH host keys are stored in a private runtime directory rather than the user's global `known_hosts`. The SSH client uses an explicitly configured private key, batch mode, and `StrictHostKeyChecking=accept-new` against that per-kernel file.

The VM image is expected to be prepared ahead of time with FreeBSD, cloud-init, SSH, `/usr/local/bin/python3`, and `ipykernel`. The provisioner is responsible for ephemeral execution, not for installing an operating system on every notebook start.

## Declarative runtime choice

`lab.yaml` keeps the jail executor as the declared default and exposes bhyve as an alternative:

```text
executor.type = jail
executor.alternatives.bhyve.kernelspec = freebsd-python-bhyve
```

At this stage the Jupyter kernel picker is the runtime-selection mechanism. A later scheduler can select the executor automatically from laboratory capabilities such as `separate_kernel`, `boot_control`, `virtual_hardware`, or `privileged_networking`.

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

1. Add a FreeBSD CI/self-hosted runner that creates and destroys both a real jail kernel and a bhyve kernel.
2. Bind kernel lifecycle/runtime identity events directly into the server evidence stream, including executor type and guest identity.
3. Execute the notebook twice in clean runtimes and emit `reproduction-complete` only when the declared comparison policy passes.
4. Implement `checks:` from `lab.yaml` as server-side assertions and emit `verification-complete` from their results.
5. Replace jail `ip4=inherit` with an isolated kernel transport.
6. Add explicit bhyve resource limits and a hardened guest image build pipeline.
7. Include executed notebook and per-output artifacts in the exported bundle.
