# FreeBSD Laboratory

Executable FreeBSD documentation for JupyterLab. The project separates notebook interaction, evidence ownership, privileged runtime lifecycle, and the FreeBSD environment that actually executes a kernel.

## Current implementation

The prototype contains five executable boundaries:

1. **JupyterLab extension** — adds the `Lab progression` right sidebar, records notebook execution observations, and adds `Export evidence` to notebook toolbars.
2. **Jupyter Server extension** — owns the evidence session, validates client event types, derives trust stages, exports artifact manifests, and can request stale-runtime reconciliation.
3. **Runtime daemon** — a small root-owned Unix-domain-socket service that is the only component allowed to create/destroy jails, ZFS clones, epairs, bridges, and vm-bhyve guests.
4. **FreeBSD VNET jail provisioner** — requests a disposable jail from the daemon and runs ipykernel through an SSH-only private transport.
5. **FreeBSD bhyve provisioner** — requests an ephemeral vm-bhyve guest from the same daemon and uses the same SSH-only transport.

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
jupyter labextension develop . --overwrite
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

## Privilege separation

Jupyter Server does not need root privileges. Privileged operations are delegated to:

```text
/var/run/freebsd-laboratory/runtime.sock
```

The socket is created with mode `0660`, owned by root and the `freebsdlab` group. The protocol intentionally accepts only:

```text
ping
create-jail
create-bhyve
destroy
gc
```

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

The Jupyter user only needs membership in `freebsdlab` and read access to the SSH private key used for kernel transport.

## Private laboratory network

Both jails and bhyve guests use the same host-only network:

```text
bridge:         labbridge0
network:        172.31.254.0/24
host address:   172.31.254.1
lease pool:     172.31.254.10-172.31.254.199
physical uplink: none
```

The daemon creates `labbridge0` if it does not exist. No physical interface is attached. Runtime-facing bridge members are marked private so laboratory guests do not forward directly to other private runtime ports.

The host firewall reference in `deploy/freebsd/` narrows this further: host-to-runtime traffic on the bridge is allowed only to TCP/22, all other host-to-runtime IPv4 traffic is blocked, and new runtime-originated IPv4 traffic is blocked.

## SSH-only Jupyter transport

Jupyter's five TCP channels are no longer exposed directly on the VNET/bhyve address. The provisioner rewrites the connection document to `127.0.0.1`, copies that document into the runtime, and keeps these same ports connected through SSH local forwards:

```text
Jupyter 127.0.0.1:<shell>   -> SSH -L -> runtime 127.0.0.1:<shell>
Jupyter 127.0.0.1:<iopub>   -> SSH -L -> runtime 127.0.0.1:<iopub>
Jupyter 127.0.0.1:<stdin>   -> SSH -L -> runtime 127.0.0.1:<stdin>
Jupyter 127.0.0.1:<control> -> SSH -L -> runtime 127.0.0.1:<control>
Jupyter 127.0.0.1:<hb>      -> SSH -L -> runtime 127.0.0.1:<hb>
```

The attached SSH process uses connection attempts, server-alive probes, TCP keepalives, and `ExitOnForwardFailure`. If the tunnel cannot be established, kernel startup fails closed. If the attached transport later dies, Jupyter observes the kernel process failure instead of retaining unreachable direct TCP channels.

### VNET jail lifecycle

The jail provisioner does not use `ip4=inherit`. A kernel launch is:

```text
Jupyter requests runtime
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
  -> connection file rebound to loopback and copied over SSH
  -> five local SSH forwards established
  -> ipykernel launched on runtime loopback
```

The jail template must contain `/usr/local/bin/python3`, ipykernel, sshd, and the `freebsd` user account.

### bhyve lifecycle

`vm-bhyve` remains the lifecycle manager around bhyve, but root commands run inside the runtime daemon rather than Jupyter Server.

```text
Jupyter requests runtime
  -> daemon reserves address from shared pool
  -> vm create -t freebsd-lab -i freebsd-python.raw -C -k <pubkey> -n <netconfig>
  -> vm start
  -> Jupyter waits for authenticated SSH
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

The common wrapper defaults to a FreeBSD `releng/*` Git source tree, builds world/kernel, then creates a versioned ZFS jail snapshot and a versioned raw bhyve image from the same build id. Both paths validate ipykernel and fail on `pkg audit -F` findings by default. A controlled Poudriere/pkg repository can be selected through `LAB_PKG_REPOS_DIR`.

See `deploy/freebsd/images/README.md` for patch rebuild, versioned activation, provenance and rollback procedures.

## Crash recovery and garbage collection

Runtime ownership is persisted under:

```text
/var/db/freebsd-laboratory/runtimes/
/var/db/freebsd-laboratory/network-leases/
```

Each registry record contains the runtime name, runtime type, owning Jupyter PID, private address, and runtime-specific resources such as the ZFS dataset or host epair.

`freebsd-lab-gc` asks the daemon to reconcile stale state. Reconciliation covers registered stale runtimes, prefixed active jails and vm-bhyve guests, prefixed ZFS child datasets, orphan epair members, and stale address leases.

The runtime daemon runs stale-only reconciliation at startup. The Jupyter Server extension also requests stale-only reconciliation during its own startup. A deliberate full laboratory reset is available with:

```sh
freebsd-lab-gc --all
```

## Evidence integrity and authenticity

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

Authenticated endpoints are mounted below the Jupyter Server base URL:

```text
GET  /freebsd-lab/api/state
POST /freebsd-lab/api/events
POST /freebsd-lab/api/export
```

Client POSTs remain restricted to observation events (`notebook-context` and `cell-executed`). Machine trust-stage events are separate server-side operations.

## Tests

```sh
pytest -q
cd labextension && npm run build
```

Linux CI validates the portable evidence/state model, Ed25519 signing and verification, runtime reconciliation logic, SSH tunnel construction, address allocation, provisioner helpers, shell syntax, and TypeScript compilation. Actual VNET/epair, PF, ZFS, jail, bhyve and golden-image lifecycles require execution on a dedicated FreeBSD environment.
