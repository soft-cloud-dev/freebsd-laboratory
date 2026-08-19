# FreeBSD Laboratory

Executable FreeBSD documentation for JupyterLab. The project separates notebook interaction, evidence ownership, privileged runtime lifecycle, and the FreeBSD environment that actually executes a kernel.

## Current implementation

The prototype contains five executable boundaries:

1. **JupyterLab extension** — adds the `Lab progression` right sidebar, records bounded notebook execution observations, and adds `Export evidence` to notebook toolbars.
2. **Jupyter Server extension** — owns the evidence session, validates and redacts client event types, derives trust stages, exports artifact manifests, and can request stale-runtime reconciliation.
3. **Runtime daemon** — a small root-owned Unix-domain-socket service that is the only component allowed to create or destroy jails, ZFS clones, epairs, bridges, and vm-bhyve guests.
4. **FreeBSD VNET jail provisioner** — requests a disposable jail from the daemon and runs ipykernel through an SSH-only private transport.
5. **FreeBSD bhyve provisioner** — requests an ephemeral vm-bhyve guest from the same daemon and uses the same SSH-only transport. This backend is optional on a jail-only host.

The browser is not treated as a trusted attestor. Events observed by the JupyterLab extension remain `self-recorded`. Later trust stages require server-side machine events.

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
cd ..
jupyter lab
```

The Jupyter Server root must contain `lab.yaml`; the repository root contains the sample definition.

`freebsd-lab-install-kernel` installs both runtime choices:

```text
FreeBSD (Python 3, VNET jail) -> disposable VNET jail
FreeBSD (Python 3, bhyve)      -> disposable bhyve VM
```

The sample notebook remains jail-backed by default. Use bhyve for experiments requiring a separate kernel, boot behavior, virtual hardware, kernel modules, or privileged guest networking.

## Privilege separation and caller authentication

Jupyter Server does not need root privileges. Privileged operations are delegated to:

```text
/var/run/freebsd-laboratory/runtime.sock
```

The socket is created with mode `0660`, owned by root and the `freebsdlab` group. Filesystem access to the socket is necessary but is not treated as sufficient authorization. For every accepted FreeBSD Unix-domain connection, the daemon obtains `LOCAL_PEERCRED` from the kernel and records the authenticated peer PID, UID, GID, process start time, and a process fingerprint.

The protocol intentionally accepts only:

```text
ping
create-jail
create-bhyve
destroy
gc
```

Create requests must name the exact PID reported by `LOCAL_PEERCRED`; a group member cannot forge another long-lived PID to defeat stale-owner reconciliation. A non-root caller can destroy only a runtime owned by the same authenticated UID. `gc --all` is likewise UID-scoped for a non-root caller; only root can reset every user's runtime. Stale-only reconciliation may reclaim a runtime after its recorded PID/start-time fingerprint no longer matches a live process.

Runtime names must match the generated `freebsd-lab-<id>` namespace. The daemon does not expose a general-purpose command execution endpoint.

A representative host setup is:

```sh
pw groupadd freebsdlab
pw groupmod freebsdlab -m <jupyter-user>
install -d -m 0755 /var/run/freebsd-laboratory
install -d -m 0700 /var/db/freebsd-laboratory
install -d -m 0750 -g freebsdlab /usr/local/etc/freebsd-laboratory
ssh-keygen -t ed25519 -N '' -f /usr/local/etc/freebsd-laboratory/id_ed25519
chgrp freebsdlab /usr/local/etc/freebsd-laboratory/id_ed25519
chmod 0640 /usr/local/etc/freebsd-laboratory/id_ed25519
install -m 0555 deploy/freebsd/rc.d/freebsd_lab_daemon /usr/local/etc/rc.d/freebsd_lab_daemon
sysrc freebsd_lab_daemon_enable=YES
service freebsd_lab_daemon start
```

The Jupyter user only needs membership in `freebsdlab` and read access to the SSH private key used for kernel transport. Installing `vm-bhyve` is required only for the bhyve executor; jail-only daemon startup and garbage collection do not depend on `/usr/local/sbin/vm`.

## Private laboratory network

Both jails and bhyve guests use the same host-only network:

```text
bridge:          labbridge0
network:         172.31.254.0/24
host address:    172.31.254.1
lease pool:      172.31.254.10-172.31.254.199
physical uplink: none
```

The daemon creates `labbridge0` if it does not exist. No physical interface is attached. Runtime-facing bridge members are marked private so laboratory guests do not forward directly to other private runtime ports.

The host firewall reference in `deploy/freebsd/` narrows this further: host-to-runtime traffic on the bridge is allowed only to TCP/22, all other host-to-runtime IPv4 traffic is blocked, and new runtime-originated IPv4 traffic is blocked. Installing the anchor file is not sufficient by itself: `/etc/pf.conf` must reference and load the `freebsd-lab` anchor, and the main ruleset must then be reloaded with `pfctl -f /etc/pf.conf`. See `deploy/freebsd/README.md` and `deploy/freebsd/validate-pf.sh`.

## SSH-only Jupyter transport

Jupyter's five TCP channels are not exposed directly on the VNET/bhyve address. For each kernel, the shared provisioner reserves five unique host loopback ports from a host-wide lease pool, rewrites the Jupyter connection document to those ports on `127.0.0.1`, copies that document into the runtime, and forwards the leased ports through SSH:

```text
Jupyter 127.0.0.1:<leased-shell>   -> SSH -L -> runtime 127.0.0.1:<leased-shell>
Jupyter 127.0.0.1:<leased-iopub>   -> SSH -L -> runtime 127.0.0.1:<leased-iopub>
Jupyter 127.0.0.1:<leased-stdin>   -> SSH -L -> runtime 127.0.0.1:<leased-stdin>
Jupyter 127.0.0.1:<leased-control> -> SSH -L -> runtime 127.0.0.1:<leased-control>
Jupyter 127.0.0.1:<leased-hb>      -> SSH -L -> runtime 127.0.0.1:<leased-hb>
```

The default tunnel pool is `30000-44999`. Allocation is serialized through a shared `flock`-protected lease directory and every candidate is actually bound on loopback before acceptance. Reservation sockets are held until immediately before OpenSSH starts, while lease ownership remains until kernel cleanup. Lease filenames carry the owner PID, UID, and a hash of the process start time, so a recycled numeric PID cannot keep an abandoned lease authoritative.

The attached SSH process uses connection attempts, server-alive probes, TCP keepalives, and `ExitOnForwardFailure`. If an unrelated host process wins the final reservation-to-OpenSSH bind race, kernel startup fails rather than silently using a conflicting listener. If the attached transport later dies, Jupyter observes the kernel process failure instead of retaining unreachable direct TCP channels.

The jail and bhyve provisioners share one remote-runtime lifecycle implementation. Crash leftovers under `~/.cache/freebsd-laboratory/runtime/` are removed before a same-ID kernel restart, and a transient daemon failure during cleanup does not prevent local connection-file, tunnel-lease, or cache cleanup. Daemon-side stale-owner reconciliation remains the privileged-resource backstop.

### VNET jail lifecycle

The jail provisioner does not use `ip4=inherit`. A kernel launch is:

```text
Jupyter requests runtime
  -> daemon authenticates caller PID/UID from LOCAL_PEERCRED
  -> daemon reserves private address
  -> ZFS clone from declared @clean snapshot
  -> epair create
  -> epairXa -> labbridge0 (private member)
  -> jail -c ... vnet vnet.interface=epairXb
  -> epairXb renamed to vnet0 inside jail
  -> private IPv4 assigned to vnet0
  -> daemon injects the configured SSH public key
  -> sshd started inside jail
  -> Jupyter waits for authenticated SSH
  -> five host-wide tunnel ports leased
  -> connection file rebound to loopback and copied over SSH
  -> five local SSH forwards established
  -> ipykernel launched on runtime loopback
```

The jail template must contain `/usr/local/bin/python3`, ipykernel, sshd, and the `freebsd` user account.

### bhyve lifecycle

`vm-bhyve` remains the lifecycle manager around bhyve, but root commands run inside the runtime daemon rather than Jupyter Server.

```text
Jupyter requests runtime
  -> daemon authenticates caller PID/UID from LOCAL_PEERCRED
  -> daemon verifies that vm-bhyve is installed
  -> daemon reserves address from shared pool
  -> vm create -t freebsd-lab -i freebsd-python.raw -C -k <pubkey> -n <netconfig>
  -> vm start
  -> Jupyter waits for authenticated SSH
  -> five host-wide tunnel ports leased
  -> connection file rebound to loopback and copied over SSH
  -> five local SSH forwards established
  -> ipykernel launched on VM loopback
```

The prepared raw image must provide cloud-init, SSH, `/usr/local/bin/python3`, and ipykernel. The bundled vm-bhyve template is `freebsd_laboratory/vm-bhyve/freebsd-lab.conf`.

## Golden image lifecycle

`deploy/freebsd/images/` contains build tooling for both runtime artifacts:

```text
build-golden-images.sh
build-jail-template.sh
build-bhyve-image.sh
vmimage.conf
sshd-freebsd-lab.conf
```

Normal host bootstrap builds a jail from the official RELEASE `base.txz`; the separate reproducible path builds from an explicit `releng/X.Y` source revision. Package selection is resolved against the target userland ABI, target-root dynamic linker hints are initialized before validation, and images fail on `pkg audit -F` findings by default.

An operator can accept only named FreeBSD VuXML records through `LAB_PKG_AUDIT_ALLOWED_VULN_IDS`. The parser tolerates hostname case and output indentation but still requires a one-to-one mapping between every reported problem and an exact allowlisted ID. Audit enforcement state and accepted IDs are written into the image provenance manifest. A controlled Poudriere/pkg repository can be selected through `LAB_PKG_REPOS_DIR`.

See `deploy/freebsd/images/README.md` for patch rebuild, versioned activation, provenance, and rollback procedures.

## Crash recovery and garbage collection

Runtime ownership is persisted under:

```text
/var/db/freebsd-laboratory/runtimes/
/var/db/freebsd-laboratory/network-leases/
```

Each registry record contains the runtime name, runtime type, authenticated owner UID/GID, owner PID/start-time fingerprint, private address, and runtime-specific resources such as the ZFS dataset or host epair.

`freebsd-lab-gc` asks the daemon to reconcile stale state. Reconciliation covers registered stale runtimes, prefixed active jails and vm-bhyve guests, prefixed ZFS child datasets, orphan epair members, and stale address leases. Missing optional backends do not prevent reconciliation of installed runtime types.

The runtime daemon creates and permission-controls its socket before startup reconciliation. Reconciliation continues across individual cleanup failures and reports them instead of preventing the service from accepting control requests. The bootstrap waits for an authenticated daemon `ping` and prints service/syslog diagnostics if readiness times out.

A deliberate reset is available with:

```sh
freebsd-lab-gc --all
```

For a non-root caller this resets only runtimes owned by that caller's UID. Root can reset all laboratory runtimes.

## Evidence integrity, minimization, and authenticity

The JupyterLab observer does not persist full cell output, cell metadata, or source text. A `cell-executed` event records the cell identifier, success state, source SHA-256, source byte count, execution count, and output count. This retains a reproducible identity without copying arbitrary rendered output or notebook secrets into the evidence stream.

The server recursively redacts values under common credential-bearing keys before hashing or persistence. Evidence sessions have configurable event-count and canonical-payload-size limits, and accepted JSONL events are flushed with `fsync` by default. Relevant extension settings are:

```text
FreeBSDLaboratoryApp.max_evidence_events
FreeBSDLaboratoryApp.max_event_payload_bytes
FreeBSDLaboratoryApp.fsync_evidence_events
```

Every export includes a manifest containing the SHA-256 hash and byte size of each evidence artifact. `SHA256SUMS` is still produced for conventional tooling.

Unsigned export:

```text
evidence.json
environment.json
events.jsonl
manifest.json
SHA256SUMS
```

Optional Ed25519 signing adds `manifest.sig.json`. The signature is calculated over the exact bytes of `manifest.json`; the manifest itself binds the artifact hashes.

Install signing support with:

```sh
python -m pip install -e ".[signing]"
```

Generate a PKCS#8 Ed25519 key pair:

```sh
openssl genpkey -algorithm ED25519 -out /usr/local/etc/freebsd-laboratory/evidence-ed25519.pem
openssl pkey -in /usr/local/etc/freebsd-laboratory/evidence-ed25519.pem -pubout -out /usr/local/etc/freebsd-laboratory/evidence-ed25519.pub.pem
```

Enable it in `lab.yaml`, then verify an export against a trusted institutional public key with:

```sh
freebsd-lab-verify-evidence .freebsd-lab/evidence/<session-id> \
  --public-key /usr/local/etc/freebsd-laboratory/evidence-ed25519.pub.pem
```

Without `--public-key`, verification proves only that the embedded key signed the manifest; it does not establish institutional trust in that key.

## Evidence API

Authenticated JSON endpoints use Jupyter Server's `APIHandler` and are mounted below the configured base URL:

```text
GET  /freebsd-lab/api/state
POST /freebsd-lab/api/events
POST /freebsd-lab/api/export
```

Client POSTs remain restricted to observation events (`notebook-context` and `cell-executed`). Machine trust-stage events are separate server-side operations. Oversized payloads return HTTP 413 and exhausted sessions return HTTP 429.

## Tests

```sh
ruff check freebsd_laboratory tests
pytest -q
cd labextension && npm run build
```

Linux CI validates the portable evidence/state model, Ed25519 signing and verification, authenticated runtime ownership rules, PID-reuse-resistant reconciliation and port leasing, SSH tunnel construction, provisioner crash cleanup, address allocation, shell syntax, Ruff, and TypeScript compilation. Actual `LOCAL_PEERCRED`, VNET/epair, PF, ZFS, jail, bhyve and golden-image lifecycles require execution on a dedicated FreeBSD environment.
