# Implementation architecture

## Governing rule

The browser reports observations. It does not manufacture platform-verified claims. The Jupyter process also does not own privileged FreeBSD lifecycle operations.

```text
Browser / JupyterLab
  |  NotebookActions observations
  |  authenticated REST
  v
Jupyter Server (unprivileged)
  |-- evidence session
  |-- event validation
  |-- progression derivation
  |-- evidence manifest / optional signature
  |
  | Jupyter kernel provisioner
  |   |-- loopback connection-file rebind
  |   |-- SSH/SCP + 5 local TCP forwards
  |   `-- Unix socket lifecycle requests
  |               |
  |               v
  |     freebsd-lab-runtime-daemon (root)
  |       |-- strict runtime namespace/actions
  |       |-- runtime registry + reconciliation
  |       |-- ZFS / jail / epair / bridge
  |       `-- vm-bhyve lifecycle
  |               |
  |         private labbridge0
  |          172.31.254.0/24
  |          PF: host -> guest TCP/22 only
  |          no physical uplink
  |          /             \
  |         /               \
  v        v                 v
SSH    VNET jail          bhyve VM
       Python/ipykernel   FreeBSD + Python/ipykernel
       loopback ZMQ       loopback ZMQ
```

## Trust and privilege boundaries

There are four independent boundaries:

1. **Browser observation boundary.** JupyterLab can report notebook context and cell execution observations, but browser-originated requests cannot assert machine-only trust stages.
2. **Jupyter evidence boundary.** Jupyter Server owns the append-only evidence stream and exported manifest. It runs without privileges to create jails, VMs, ZFS datasets, epairs, or bridges.
3. **Runtime lifecycle boundary.** `freebsd-lab-runtime-daemon` is root-owned and listens on a local Unix-domain socket. It accepts a fixed set of structured lifecycle operations instead of arbitrary shell commands.
4. **Runtime network boundary.** The host communicates with laboratory runtimes only through SSH on the private bridge. Jupyter's five TCP channels remain loopback-only and are tunneled through that SSH session.

The runtime socket defaults to:

```text
/var/run/freebsd-laboratory/runtime.sock
mode: 0660
owner: root
group: freebsdlab
```

Runtime names are constrained to the generated `freebsd-lab-<id>` namespace. Jupyter provisioners request runtime creation/destruction and receive normalized runtime metadata such as the assigned private address.

## JupyterLab and Jupyter Server

`labextension/src/index.ts` provides the `Lab progression` panel and `Export evidence` action. It records notebook context and `NotebookActions.executed` observations through authenticated Jupyter Server endpoints.

`freebsd_laboratory/app.py` registers:

```text
/freebsd-lab/api/state
/freebsd-lab/api/events
/freebsd-lab/api/export
```

`freebsd_laboratory/service.py` owns the evidence stream. The browser API accepts only `notebook-context` and `cell-executed`; machine-stage events remain server-only.

On FreeBSD, the server extension also requests stale-runtime reconciliation from the runtime daemon during startup. Failure to reach the daemon is logged rather than converted into a false cleanup claim.

## Private runtime network and PF boundary

Jails and bhyve guests share one host-only L2 domain:

```text
bridge:          labbridge0
network:         172.31.254.0/24
host address:    172.31.254.1
runtime leases:  172.31.254.10-172.31.254.199
physical uplink: none
```

Runtime-facing members are private bridge ports. PF is evaluated on `labbridge0` with bridge-level pfil enabled. The reference anchor is deliberately narrower than the L2 network itself:

```text
host -> runtime TCP/22:            pass, stateful
host -> runtime any other IPv4:    block + log
runtime -> host/routed IPv4:       block + log
reply traffic for host SSH state:  pass through PF state
```

This removes direct guest-to-host access as an implicit dependency. Host services such as SSH, databases, NFS, Jupyter HTTP, or DNS are unavailable to a laboratory runtime unless a deployment deliberately adds a narrowly scoped exception.

## SSH-only Jupyter transport

`freebsd_laboratory/remote_kernel.py` turns the Jupyter connection document into a tunnel contract rather than a set of bridge-visible listeners.

The standard TCP connection fields are validated:

```text
shell_port
iopub_port
stdin_port
control_port
hb_port
```

The connection IP is rewritten to `127.0.0.1` before the file is staged into the runtime. The same five random ports are then forwarded by the attached SSH process:

```text
127.0.0.1:<port> on host
  -> ssh -L 127.0.0.1:<port>:127.0.0.1:<port>
  -> 127.0.0.1:<port> in runtime
```

Therefore ipykernel binds only to runtime loopback, while the Jupyter client binds only to host loopback. The bridge carries SSH packets, not exposed Jupyter ZMQ sockets.

The default transport resilience settings are:

```text
ConnectTimeout=5
ConnectionAttempts=3
ServerAliveInterval=15
ServerAliveCountMax=4
TCPKeepAlive=yes
ExitOnForwardFailure=yes
```

`ExitOnForwardFailure` makes a local-forward bind/setup failure fatal to kernel startup. Server-alive probes detect a broken SSH path independently of Jupyter heartbeat traffic. If the attached SSH process ultimately exits, `LocalProvisioner` exposes that process failure to Jupyter instead of leaving a remote kernel that appears locally attached.

These values are traitlet-backed provisioner configuration and can be overridden per kernelspec.

## VNET jail runtime

`freebsd_laboratory/provisioner.py` is a Jupyter `LocalProvisioner` specialization. It asks the runtime daemon to create an isolated jail and then uses the common SSH tunnel transport.

Privileged lifecycle:

```text
reserve private address
  -> zfs clone declared @clean snapshot
  -> epair create
  -> epairXa -> labbridge0
  -> mark epairXa private
  -> jail -c ... vnet vnet.interface=epairXb
  -> rename epairXb to vnet0 inside jail
  -> assign private IPv4 to vnet0
  -> install laboratory SSH public key
  -> start sshd
```

Jupyter-side lifecycle:

```text
wait for authenticated SSH
  -> bind connection document to loopback
  -> SCP connection file to jail
  -> establish five SSH local forwards
  -> launch loopback-bound ipykernel through attached SSH process
```

The jail template provides FreeBSD userspace, Python/ipykernel, sshd, and the configured unprivileged guest account. `ip4=inherit` is not used.

## bhyve runtime

`freebsd_laboratory/bhyve.py` uses the same runtime-daemon and SSH tunnel boundaries. Root-owned vm-bhyve commands are not executed by Jupyter Server.

The daemon binds a manual vm-bhyve switch named `freebsdlab` to `labbridge0`, allocates an address from the shared lease pool, and creates the guest from the prepared raw image with cloud-init network/key data.

```text
request bhyve runtime
  -> receive assigned private address
  -> wait for SSH
  -> bind connection document to loopback
  -> stage connection file
  -> establish five SSH local forwards
  -> launch loopback-bound ipykernel
```

Cleanup requests VM destruction through the daemon.

## Golden image lifecycle

The two runtime paths have a common rebuild policy under `deploy/freebsd/images/`.

```text
build-golden-images.sh
  |
  |-- validate clean FreeBSD releng/* source revision
  |-- buildworld + buildkernel
  |
  +-- build-jail-template.sh
  |     -> installworld/distribution into versioned ZFS dataset
  |     -> install Python/ipykernel
  |     -> restricted sshd configuration
  |     -> pkg audit
  |     -> embedded source/runtime manifest
  |     -> immutable @clean snapshot
  |
  `-- build-bhyve-image.sh
        -> FreeBSD release vm-image target
        -> raw/UFS image
        -> Python/ipykernel/cloud-init
        -> restricted sshd configuration
        -> pkg audit
        -> versioned raw artifact + SHA-256 + manifest
```

Both artifacts receive the same build id and source revision. Construction is versioned; activation is a separate operator decision. This preserves rollback and avoids silently replacing the base of a running laboratory.

A root-controlled `LAB_PKG_REPOS_DIR` can point the builders at a Poudriere/pkg repository configuration. Public/default package repositories remain the fallback when that variable is unset.

The golden SSH policy permits only the feature needed by the transport (`AllowTcpForwarding local`) and disables password/root login, agent/X11 forwarding, gateway ports, and SSH tunnels. Image host keys are removed before finalization so instances do not inherit the builder's SSH identity.

## Crash recovery and reconciliation

The daemon writes atomic resource records below:

```text
/var/db/freebsd-laboratory/runtimes/
```

Address leases are persisted below:

```text
/var/db/freebsd-laboratory/network-leases/
```

A runtime record binds the generated runtime name to its runtime type, owning Jupyter PID, private address, and runtime-specific resources such as the ZFS dataset and epair interface.

`freebsd-lab-gc` invokes the same reconciliation engine used at daemon startup. Stale-only reconciliation keeps runtimes whose recorded owner PID exists; removes stale registered runtimes; discovers orphan prefixed jails, VMs and datasets; removes unreferenced epairs; and releases orphan address leases.

This makes cleanup independent of the normal kernel `cleanup()` path and covers SIGKILL/server-crash cases where Jupyter cannot execute shutdown hooks. A host power failure leaves persistent state for the next daemon startup to reconcile.

## Evidence manifest and optional signing

`manifest.json` contains the SHA-256 digest and byte size of each exported evidence artifact. `SHA256SUMS` remains available for conventional checksum tooling.

When `evidence.signing.enabled` is true, `freebsd_laboratory/signing.py` signs the exact manifest bytes using Ed25519 and writes `manifest.sig.json`. A trusted external public key is required to turn signature consistency into signer identity.

## Trust progression

| Stage | Current derivation | Trust source |
|---|---|---|
| Observed | at least one `cell-executed` event | browser observation |
| Explained | notebook context contains markdown | browser observation |
| Reproduced | `reproduction-complete` | server-only machine event |
| Modified | `mutation-applied` | server-only machine event |
| Verified | `verification-complete` | server-only machine event |
| Recovered | `recovery-complete` | server-only machine event |
| Designed | `design-validated` | server-only machine event |

The later five machine-event producers remain intentionally incomplete. Runtime isolation, a green CI run, and cryptographic signing are not substitutes for experimental verification.

## Declarative runtime choice

`lab.yaml` declares the VNET jail as the default executor and bhyve as an alternative. Both reference the same privileged control socket and private network model. The Jupyter kernel picker currently selects the executor.

## Remaining validation boundary

Linux CI validates portable protocol logic, reconciliation, lease allocation, evidence signing, SSH tunnel construction, shell syntax, and Python/TypeScript builds. It cannot prove actual FreeBSD behavior for PF, `jail(8)`, `epair(4)`, ZFS, bhyve, the release image build, or `rc.d` boot ordering.

The next evidence-producing implementation slice is:

1. execute real VNET-jail and bhyve kernel smoke tests on a dedicated FreeBSD environment;
2. validate the PF anchor with `pfctl -nf` and network-negative tests;
3. build both golden artifacts from a clean `releng/*` source revision and record their hashes/manifests;
4. bind executor identity and runtime lifecycle events into server-owned evidence;
5. execute clean-runtime repetition and emit `reproduction-complete` only from a declared comparison policy;
6. implement `checks:` as server-side assertions and emit `verification-complete` only from their results.
