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
  |   |-- connection-file IP rebind
  |   |-- SSH/SCP kernel transport
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
  |          no physical uplink
  |          /             \
  |         /               \
  v        v                 v
SSH    VNET jail          bhyve VM
       Python/ipykernel   FreeBSD + Python/ipykernel
```

## Trust and privilege boundaries

There are three distinct trust boundaries:

1. **Browser observation boundary.** JupyterLab can report notebook context and cell execution observations, but browser-originated requests cannot assert machine-only trust stages.
2. **Jupyter evidence boundary.** Jupyter Server owns the append-only evidence stream and the exported manifest. It runs without privileges to create jails, VMs, ZFS datasets, epairs, or bridges.
3. **Runtime lifecycle boundary.** `freebsd-lab-runtime-daemon` is root-owned and listens on a local Unix-domain socket. It accepts a fixed set of structured lifecycle operations instead of arbitrary shell commands.

The runtime socket defaults to:

```text
/var/run/freebsd-laboratory/runtime.sock
mode: 0660
owner: root
 group: freebsdlab
```

Runtime names are constrained to the generated `freebsd-lab-<id>` namespace. Jupyter kernel provisioners request runtime creation/destruction and receive only normalized runtime metadata such as the assigned private IP address.

## JupyterLab and Jupyter Server

`labextension/src/index.ts` provides the `Lab progression` panel and `Export evidence` action. It records notebook context and `NotebookActions.executed` observations through authenticated Jupyter Server endpoints.

`freebsd_laboratory/app.py` registers:

```text
/freebsd-lab/api/state
/freebsd-lab/api/events
/freebsd-lab/api/export
```

`freebsd_laboratory/service.py` owns the evidence stream. The browser API accepts only `notebook-context` and `cell-executed`; machine-stage events remain server-only.

On FreeBSD, the server extension also requests stale-runtime reconciliation from the runtime daemon during startup. Failure to reach the daemon is logged rather than converted into a false successful cleanup claim.

## Private runtime network

Jails and bhyve guests share one host-only L2 domain:

```text
bridge:          labbridge0
network:         172.31.254.0/24
host address:    172.31.254.1
runtime leases:  172.31.254.10-172.31.254.199
physical uplink: none
```

The bridge address is assigned to `labbridge0`, not to runtime member interfaces. Runtime-facing members are configured as private bridge ports. The network is therefore not bridged onto the host LAN, and private runtime members are not intended to forward directly to one another through the bridge.

The host-side bridge address remains a management endpoint. Deployments requiring complete guest-to-host network denial must additionally enforce host firewall policy on the laboratory interface.

## VNET jail runtime

`freebsd_laboratory/provisioner.py` remains a Jupyter `LocalProvisioner`, but it no longer creates a jail or invokes `jexec` directly. It asks the runtime daemon to create an isolated jail and then uses the same remote-kernel transport model as bhyve.

The privileged lifecycle is:

```text
reserve private address
  -> zfs clone freebsd-python@clean
  -> epair create
  -> epairXa -> labbridge0
  -> mark epairXa private
  -> jail -c ... vnet vnet.interface=epairXb
  -> rename epairXb to vnet0 inside jail
  -> assign private IPv4 to vnet0
  -> install laboratory SSH public key
  -> start sshd
```

The Jupyter-side lifecycle is then:

```text
wait for authenticated SSH
  -> rewrite Jupyter connection document IP to jail address
  -> SCP connection file to jail
  -> launch ipykernel through attached SSH process
```

This removes `ip4=inherit`: the jailed kernel no longer shares the host IP stack. The jail template still provides the FreeBSD userspace, Python/ipykernel, sshd, and the configured unprivileged guest account.

The default storage layout is:

```text
zroot/jails/templates/freebsd-python@clean
zroot/jails/containers/<runtime>
/usr/local/jails/containers/<runtime>
```

## bhyve runtime

`freebsd_laboratory/bhyve.py` uses the same runtime-daemon and SSH transport boundaries. Root-owned vm-bhyve commands are no longer executed by the Jupyter Server process.

The daemon binds a manual vm-bhyve switch named `freebsdlab` to `labbridge0` and enables private-switch behavior. It allocates an address from the same lease pool and creates the guest with the prepared `freebsd-python.raw` image and cloud-init network/key data.

The Jupyter-side sequence is:

```text
request bhyve runtime
  -> receive assigned private address
  -> wait for SSH
  -> rewrite connection-file IP
  -> stage connection file
  -> SSH <guest> <ipykernel command>
```

The local SSH process remains attached to the remote kernel so Jupyter observes loss of the remote kernel process. Cleanup requests VM destruction through the daemon.

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

`freebsd-lab-gc` invokes the same reconciliation engine used at daemon startup. Stale-only reconciliation:

1. keeps registered runtimes whose recorded owner PID still exists;
2. destroys registered runtimes whose owner PID is gone;
3. discovers prefixed jails, vm-bhyve guests, and ZFS datasets that have no retained registry owner;
4. removes unreferenced epair members from the dedicated laboratory bridge;
5. releases private-address leases that do not belong to retained runtimes.

This makes cleanup independent of the normal kernel `cleanup()` path and covers SIGKILL/server-crash cases where Jupyter cannot execute its shutdown hooks. A host power failure leaves persistent resource state for the next daemon startup to reconcile.

## Evidence manifest and optional signing

The export format separates artifact integrity from signer authenticity.

`manifest.json` contains the SHA-256 digest and byte size of each evidence artifact:

```text
evidence.json
environment.json
events.jsonl
```

The manifest also records the lab/session metadata and whether signing is configured. `SHA256SUMS` remains available for conventional checksum tooling.

When `evidence.signing.enabled` is true, `freebsd_laboratory/signing.py` signs the exact bytes of `manifest.json` using Ed25519 and writes:

```text
manifest.sig.json
```

The sidecar contains the algorithm, key id, manifest hash, public-key fingerprint, embedded public key, and signature. Because the signed manifest contains the artifact hashes, the signature transitively authenticates the evidence artifacts listed by that manifest.

`freebsd-lab-verify-evidence` always verifies artifact hashes. If a trusted public key is supplied, it additionally requires the signature to verify with that external key. Verification using only the embedded key demonstrates internal consistency but does not establish institutional identity.

## Trust progression

The progression model remains deliberately asymmetric:

| Stage | Current derivation | Trust source |
|---|---|---|
| Observed | at least one `cell-executed` event | browser observation |
| Explained | notebook context contains markdown | browser observation |
| Reproduced | `reproduction-complete` | server-only machine event |
| Modified | `mutation-applied` | server-only machine event |
| Verified | `verification-complete` | server-only machine event |
| Recovered | `recovery-complete` | server-only machine event |
| Designed | `design-validated` | server-only machine event |

The later five machine-event producers are still intentionally incomplete. Runtime isolation and cryptographic signing must not be mistaken for experimental verification.

## Declarative runtime choice

`lab.yaml` declares the VNET jail as the default executor and bhyve as an alternative. Both reference the same privileged control socket and private network model.

At this stage the Jupyter kernel picker selects the runtime. A later scheduler can select an executor from explicit laboratory capabilities such as `separate_kernel`, `boot_control`, `virtual_hardware`, or `privileged_networking`.

## Remaining validation boundary

Linux CI can validate the protocol model, portable reconciliation logic, lease allocation, evidence hashing/signing, and Python/TypeScript builds. It cannot prove actual FreeBSD lifecycle behavior for `jail(8)`, `epair(4)`, `ifconfig(8)`, ZFS, or bhyve.

The next implementation slice is therefore:

1. add a dedicated FreeBSD CI/self-hosted runner and execute real VNET-jail and bhyve kernel smoke tests;
2. bind executor identity and runtime lifecycle events into server-owned evidence;
3. execute clean-runtime repetition and emit `reproduction-complete` only from a declared comparison policy;
4. implement `checks:` as server-side assertions and emit `verification-complete` only from their results;
5. add explicit runtime resource controls and host firewall assertions;
6. include the executed notebook and per-output artifacts in the signed evidence manifest.
