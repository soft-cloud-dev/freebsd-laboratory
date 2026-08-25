# Implementation architecture

## Governing rule

The browser reports observations. It does not manufacture platform-verified claims. The Jupyter process also does not own privileged FreeBSD lifecycle operations.

```text
Browser / JupyterLab
  |  minimized NotebookActions observations
  |  authenticated REST
  v
Jupyter Server (unprivileged)
  |-- bounded/redacted evidence session
  |-- event validation + fsync JSONL
  |-- progression derivation
  |-- evidence manifest / optional signature
  |
  | shared Jupyter kernel provisioner
  |   |-- fingerprinted host-wide tunnel-port lease
  |   |-- loopback connection-file rebind
  |   |-- SSH/SCP + 5 local TCP forwards
  |   `-- Unix socket lifecycle request
  |               |
  |               | FreeBSD LOCAL_PEERCRED
  |               v
  |     freebsd-lab-runtime-daemon (root)
  |       |-- peer PID/UID ownership enforcement
  |       |-- strict runtime namespace/actions
  |       |-- atomic runtime registry + reconciliation
  |       |-- ZFS / jail / epair / bridge
  |       `-- optional vm-bhyve lifecycle
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

1. **Browser observation boundary.** JupyterLab can report notebook context and cell execution observations, but browser-originated requests cannot assert machine-only trust stages. Full cell outputs, metadata, and source text are not copied into the evidence stream; source identity is represented by SHA-256 and byte count.
2. **Jupyter evidence boundary.** Jupyter Server owns a bounded, recursively redacted, append-only evidence stream and exported manifest. Accepted JSONL events are fsynced by default. It runs without privileges to create jails, VMs, ZFS datasets, epairs, or bridges.
3. **Runtime lifecycle boundary.** `freebsd-lab-runtime-daemon` is root-owned and listens on a local Unix-domain socket. The filesystem group controls who may connect; FreeBSD `LOCAL_PEERCRED` then authenticates the peer PID, UID, and GID for each request. The daemon accepts a fixed set of structured lifecycle operations instead of arbitrary shell commands and scopes destructive operations to the authenticated owner UID or root.
4. **Runtime network boundary.** The host communicates with laboratory runtimes only through SSH on the private bridge. Jupyter's five TCP channels remain loopback-only and are tunneled through that SSH session.

The runtime socket defaults to:

```text
/var/run/freebsd-laboratory/runtime.sock
mode: 0660
owner: root
group: freebsdlab
```

Runtime names are constrained to the generated `freebsd-lab-<id>` namespace. A create request must use the exact PID returned by `LOCAL_PEERCRED`; a group member cannot nominate another long-lived PID. The registry persists owner UID/GID and a PID/start-time fingerprint. `destroy` and non-stale GC require the same UID or root. For non-root callers, `gc --all` means all runtimes owned by that UID, not all host runtimes.

## JupyterLab and Jupyter Server

`labextension/src/index.ts` provides the `Lab progression` panel and `Export evidence` action. It records notebook context and `NotebookActions.executed` observations through authenticated Jupyter Server endpoints.

A cell event contains:

```text
notebook path
cell id
success / error-present flags
cell type
source SHA-256 and byte count
execution count
output count
```

It deliberately excludes rendered outputs, arbitrary metadata, traceback text, and source text.

`freebsd_laboratory/app.py` registers APIHandler-based endpoints:

```text
/freebsd-lab/api/state
/freebsd-lab/api/events
/freebsd-lab/api/export
```

`freebsd_laboratory/service.py` owns the evidence stream. The browser API accepts only `notebook-context` and `cell-executed`; machine-stage events remain server-only. Payloads are recursively redacted under common credential-bearing keys, canonicalized, size-checked, and durably appended. Per-session event count and per-event payload size are traitlet-configurable. Oversized events return HTTP 413; an exhausted session returns HTTP 429.

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

The policy is active only when the host main `/etc/pf.conf` evaluates and loads the `freebsd-lab` anchor. Installing `/usr/local/etc/pf.anchors/freebsd-lab` alone is not an isolation boundary. Deployment therefore validates the `anchor` and `load anchor` declarations, validates the complete ruleset with `pfctl -nf /etc/pf.conf`, reloads it with `pfctl -f /etc/pf.conf`, and verifies the active anchor.

This removes direct guest-to-host access as an implicit dependency. Host services such as SSH, databases, NFS, Jupyter HTTP, or DNS are unavailable to a laboratory runtime unless a deployment deliberately adds a narrowly scoped exception.

## SSH-only Jupyter transport

The remote transport is split into focused modules:

```text
remote_connection.py  Jupyter connection-document validation/rewrite/restore
port_leases.py        host-wide collision-safe port leases
ssh_transport.py      SSH/SCP command and readiness policy
remote_provisioner.py shared jail/bhyve lifecycle
```

The standard TCP connection fields are validated:

```text
shell_port
iopub_port
stdin_port
control_port
hb_port
```

The provisioner does not trust the original Jupyter random-port selection as a host-wide forwarding namespace. It allocates five ports from a shared lease pool, rewrites the connection document to those leased ports on `127.0.0.1`, and publishes the same leased ports back into the active Jupyter connection state.

Default host-side tunnel allocation is:

```text
range:          30000-44999
lease dir:      /var/run/freebsd-laboratory/tunnel-port-leases
lease dir mode: 2770 root:freebsdlab
ports/kernel:   5
bind address:   127.0.0.1
```

Allocation uses a process-local mutex plus a cross-process `flock` on the shared lease directory. Every candidate is actually bound on loopback before acceptance, so an existing host listener is never intentionally selected. Reservation sockets remain open through runtime creation, SSH readiness, connection rewriting and SCP. Immediately before `LocalProvisioner.launch_kernel()` starts OpenSSH, the reservation sockets are released; lease files remain owned by that kernel until cleanup.

A lease filename carries the owner PID, UID, and SHA-256 of the UID/start-time fingerprint. PID existence alone is not considered authoritative: when a PID is recycled, the start-time digest changes and the old lease can be reclaimed after the candidate port is successfully rebound.

This provides collision exclusion between cooperating FreeBSD Laboratory sessions, including simultaneous sessions in different Jupyter processes that share the lease directory. A non-cooperating host process can theoretically bind during the final reservation-to-OpenSSH handoff; `ExitOnForwardFailure=yes` converts that race into a failed launch rather than a silently miswired kernel.

The attached SSH process forwards each leased port from host loopback to runtime loopback. Therefore the bridge carries SSH packets, not exposed Jupyter ZMQ sockets.

The default transport resilience settings are:

```text
ConnectTimeout=5
ConnectionAttempts=3
ServerAliveInterval=15
ServerAliveCountMax=4
TCPKeepAlive=yes
ExitOnForwardFailure=yes
```

The jail and bhyve provisioners inherit one shared lifecycle implementation. It removes stale per-kernel cache directories before a same-ID restart, restores the original connection document on every failure path, and catches transient runtime-daemon errors during cleanup so local lease/cache cleanup still completes. Privileged leftovers are left for authenticated stale-owner reconciliation.

## VNET jail runtime

`freebsd_laboratory/provisioner.py` supplies the jail-specific create request while `remote_provisioner.py` owns the common Jupyter/SSH lifecycle.

Privileged lifecycle:

```text
LOCAL_PEERCRED authenticate creator
  -> record UID/GID + PID/start-time fingerprint
  -> reserve private address
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
  -> lease five host-wide loopback ports
  -> replace superseded Jupyter cached ports
  -> bind connection document to loopback/leased ports
  -> SCP connection file to jail
  -> hand reservation listeners to OpenSSH
  -> establish five SSH local forwards
  -> launch loopback-bound ipykernel through attached SSH process
```

The jail template provides FreeBSD userspace, Python/ipykernel, sshd, and the configured unprivileged guest account. `ip4=inherit` is not used.

## bhyve runtime

`freebsd_laboratory/bhyve.py` supplies the bhyve-specific create request and preserves a longer VM startup timeout. Root-owned vm-bhyve commands are not executed by Jupyter Server.

The daemon treats vm-bhyve as optional. Jail-only startup, ping, and reconciliation do not execute `/usr/local/sbin/vm`. A create-bhyve request fails with a specific installation error when the command is absent.

When available, the daemon binds a manual vm-bhyve switch named `freebsdlab` to `labbridge0`, allocates an address from the shared lease pool, and creates the guest from the prepared raw image with cloud-init network/key data.

Cleanup requests VM destruction through the daemon and releases the tunnel-port lease.

## Golden image lifecycle

The normal bootstrap and source-reproducible paths are explicit:

```text
default release mode
  official X.Y-RELEASE MANIFEST + base.txz
  -> verify base.txz SHA-256
  -> extract to versioned ZFS dataset
  -> resolve pkg against target ABI
  -> install Python/ipykernel
  -> initialize target-root ldconfig hints
  -> validate Python/ipykernel
  -> pkg audit policy
  -> provenance manifest
  -> @clean snapshot

source mode
  explicit releng/X.Y revision
  -> buildworld
  -> installworld/distribution to versioned ZFS dataset
  -> same target-ABI/package/ldconfig/audit pipeline
  -> provenance manifest with exact source revision
  -> @clean snapshot
```

Exact temporary audit exceptions are expressed as FreeBSD VuXML IDs. Parser whitespace and hostname case are normalized, but every reported problem must map one-to-one to an allowlisted ID. The image manifest records whether audit enforcement was active and which IDs were accepted. `LAB_FAIL_ON_PKG_AUDIT=NO` remains diagnostic-only and is visible in provenance.

The paired `build-golden-images.sh` path remains source-based for jail and bhyve artifacts that must originate from the same build ID and source revision. A root-controlled `LAB_PKG_REPOS_DIR` can point builders at a Poudriere/pkg repository configuration.

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

A runtime record binds the generated runtime name to its runtime type, authenticated owner UID/GID, owner PID/start-time fingerprint, private address, and runtime-specific resources such as the ZFS dataset and epair interface.

`freebsd-lab-gc` invokes the same reconciliation engine used at daemon startup. Stale-only reconciliation retains a runtime only when the live process UID and start-time digest still match the registry. It removes stale registered runtimes, discovers orphan prefixed jails/VMs/datasets, removes unreferenced epairs, and releases orphan address leases. Discovery of an optional backend is skipped when its command is absent.

The daemon binds and permissions its control socket before startup reconciliation, so one broken orphan cannot prevent control-plane readiness. Reconciliation catches and reports per-runtime cleanup errors rather than aborting the service. Bootstrap polls authenticated `ping` readiness with a bounded timeout and prints service/syslog diagnostics on failure.

Tunnel-port leases are separate host-process coordination state under `/var/run`. They use the same UID/start-time fingerprint principle rather than a bare PID. A host restart clears the volatile namespace.

## Evidence manifest and optional signing

`manifest.json` contains the SHA-256 digest and byte size of each exported evidence artifact. `SHA256SUMS` remains available for conventional checksum tooling.

When `evidence.signing.enabled` is true, `freebsd_laboratory/signing.py` signs the exact manifest bytes using Ed25519 and writes `manifest.sig.json`. A trusted external public key is required to turn signature consistency into signer identity.

## Trust progression

| Stage | Current derivation | Trust source |
|---|---|---|
| Observed | at least one bounded `cell-executed` event | browser observation |
| Explained | notebook context contains markdown | browser observation |
| Reproduced | `reproduction-complete` | server-only machine event |
| Modified | `mutation-applied` | server-only machine event |
| Verified | `verification-complete` | server-only machine event |
| Recovered | `recovery-complete` | server-only machine event |
| Designed | `design-validated` | server-only machine event |

The later five machine-event producers remain intentionally incomplete. Runtime isolation, a green CI run, and cryptographic signing are not substitutes for experimental verification.

## Declarative runtime choice

`lab.yaml` declares the VNET jail as the default executor and bhyve as an alternative. Both reference the same privileged control socket and private network model. The Jupyter kernel picker currently selects the executor.

## Autonomous agent controller

The optional agent subsystem (`pip install freebsd-laboratory[agent]`) lets a locally-hosted GGUF model propose guest-level shell actions inside disposable runtimes. The controller preserves the existing trust hierarchy: `AgentModel → AgentController → RuntimeClient → runtime.sock → runtime-daemon`.

```text
┌──────────────────────┐
│  Local LLM Engine    │
│  llama-cpp-python    │
│  GGUF model          │
└──────────┬───────────┘
           │ proposed action
           ▼
┌──────────────────────┐
│  Agent Controller    │
│  command policy      │
│  timeout/output caps │
│  evidence generation │
└──────┬────────┬──────┘
       │        │
       ▼        ▼
 RuntimeClient  SSHTransport
       │        (per-runtime key,
       ▼         known_hosts, freebsd@)
 runtime-daemon
       │
       ▼
 Isolated Runtime
```

The model's observable/action space:

- **Observable**: goal string, bounded command outputs (head/tail buffers + actual byte counts).
- **Actions**: `COMMAND: <single shell action>` or `FINAL: <task result>`.

The controller never injects host-side lifecycle capabilities, credentials, socket paths, SSH key paths, or registry paths into the model context. Guest-observable facts such as the jail hostname or IP address are not secrets.

bhyve is the default isolation mode for autonomous execution. VNET jails are available via explicit `--mode jail` for controlled experiments that accept the weaker shared-kernel boundary. The controller fails explicitly when bhyve is unavailable rather than silently falling back to jail.

Agent evidence is a standalone durable append-only JSONL log recording SHA-256 hashes and byte counts of commands and outputs. Raw command text and output content are not persisted. This remains separate from the laboratory evidence stream until a process-safe cross-process evidence sink is implemented.

Cleanup on `SIGINT`/`SIGTERM` destroys the runtime synchronously. After `SIGKILL`, crashes, or power loss, daemon stale-owner reconciliation reclaims orphaned runtimes.

## Remaining validation boundary

Linux CI validates portable protocol logic, ownership policy, PID fingerprints, reconciliation, concurrent tunnel-port lease allocation, bounded evidence, signing, SSH tunnel construction, shell syntax, Ruff, and Python/TypeScript builds. It cannot prove actual FreeBSD behavior for `LOCAL_PEERCRED`, PF, `jail(8)`, `epair(4)`, ZFS, bhyve, the release image build, or `rc.d` boot ordering.

The next evidence-producing implementation slice is:

1. execute real VNET-jail and bhyve kernel smoke tests on a dedicated FreeBSD environment;
2. exercise two distinct UIDs in `freebsdlab` and prove cross-UID destroy/full-GC denial through the real Unix socket;
3. validate the PF anchor with `pfctl -nf`, a live main-ruleset reload, and network-negative tests;
4. build release and source golden artifacts and record their hashes/manifests;
5. bind executor identity and runtime lifecycle events into server-owned evidence;
6. execute clean-runtime repetition and emit `reproduction-complete` only from a declared comparison policy;
7. implement `checks:` as server-side assertions and emit `verification-complete` only from their results.
