# FreeBSD Laboratory

Executable FreeBSD documentation for JupyterLab. The project separates notebook interaction, evidence ownership, privileged runtime lifecycle, and the FreeBSD environment that actually executes a kernel.

## Current implementation

The prototype contains five executable boundaries:

1. **JupyterLab extension** — adds the `Lab progression` right sidebar, records notebook execution observations, and adds `Export evidence` to notebook toolbars.
2. **Jupyter Server extension** — owns the evidence session, validates client event types, derives trust stages, exports artifact manifests, and can request stale-runtime reconciliation.
3. **Runtime daemon** — a small root-owned Unix-domain-socket service that is the only component allowed to create/destroy jails, ZFS clones, epairs, bridges, and vm-bhyve guests.
4. **FreeBSD VNET jail provisioner** — requests a disposable jail from the daemon, connects over the private laboratory network, stages the Jupyter connection file over SSH, and runs ipykernel inside the jail.
5. **FreeBSD bhyve provisioner** — requests an ephemeral vm-bhyve guest from the same daemon and uses the same private laboratory network and SSH kernel transport.

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

Jupyter Server no longer needs root privileges. Privileged operations are delegated to:

```text
/var/run/freebsd-laboratory/runtime.sock
```

The socket is created with mode `0660`, owned by root and the `freebsdlab` group. The protocol intentionally accepts only these lifecycle operations:

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
freebsd-lab-runtime-daemon
```

Run the daemon as a root-managed service in production. The Jupyter user only needs membership in `freebsdlab` and read access to the SSH private key used for kernel transport.

## Private laboratory network

Both jails and bhyve guests use the same host-only network:

```text
bridge:       labbridge0
network:      172.31.254.0/24
host address: 172.31.254.1
lease pool:   172.31.254.10-172.31.254.199
physical uplink: none
```

The daemon creates `labbridge0` if it does not exist. No physical interface is attached. Runtime-facing bridge members are marked private so laboratory guests do not forward directly to other private runtime ports.

### VNET jail lifecycle

The jail provisioner no longer uses `ip4=inherit`. A kernel launch is:

```text
Jupyter requests runtime
  -> daemon reserves private address
  -> ZFS clone from freebsd-python@clean
  -> epair create
  -> epairXa -> labbridge0 (private member)
  -> jail -c ... vnet vnet.interface=epairXb
  -> epairXb renamed to vnet0 inside jail
  -> private IPv4 assigned to vnet0
  -> daemon injects the configured SSH public key
  -> sshd started inside jail
  -> Jupyter rewrites connection-file IP to jail address
  -> connection file copied over SSH
  -> ipykernel launched over attached SSH session
```

The VNET jail owns its network stack and cannot bind sockets on the host stack. Because the laboratory bridge has no physical uplink, the runtime is not bridged onto the host LAN. The bridge host address remains reachable for host/runtime management; deployments requiring zero guest-to-host reachability should additionally enforce host firewall policy on the laboratory interface.

The jail template snapshot must contain:

```text
/usr/local/bin/python3
ipykernel
sshd
freebsd user account
```

Default storage remains:

```text
zroot/jails/templates/freebsd-python@clean
zroot/jails/containers/<runtime>
/usr/local/jails/containers/<runtime>
```

### bhyve lifecycle

`vm-bhyve` remains the lifecycle manager around bhyve, but root commands now run inside the runtime daemon rather than inside Jupyter Server.

The daemon ensures a manual vm-bhyve switch named `freebsdlab` is bound to `labbridge0` and enables vm-bhyve private-switch behavior. A bhyve kernel launch is:

```text
Jupyter requests runtime
  -> daemon reserves address from shared pool
  -> vm create -t freebsd-lab -i freebsd-python.raw -C -k <pubkey> -n <netconfig>
  -> vm start
  -> Jupyter waits for authenticated SSH
  -> connection-file IP rewritten to VM address
  -> connection file copied over SSH
  -> ipykernel launched over attached SSH session
```

The prepared `freebsd-python.raw` image must provide cloud-init, SSH, `/usr/local/bin/python3`, and ipykernel. The bundled vm-bhyve template is:

```text
freebsd_laboratory/vm-bhyve/freebsd-lab.conf
```

## Crash recovery and garbage collection

Runtime ownership is persisted under:

```text
/var/db/freebsd-laboratory/runtimes/
/var/db/freebsd-laboratory/network-leases/
```

Each registry record contains the runtime name, runtime type, owning Jupyter PID, private address, and runtime-specific resources such as the ZFS dataset or host epair.

`freebsd-lab-gc` asks the daemon to reconcile stale state. By default it keeps runtimes whose recorded owner PID is still alive and removes stale resources. Reconciliation covers:

- registered stale runtimes;
- prefixed active jails;
- prefixed vm-bhyve guests;
- prefixed ZFS child datasets;
- orphan epair members attached to `labbridge0`;
- stale private-address leases.

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

Optional Ed25519 signing adds:

```text
manifest.sig.json
```

The signature is calculated over the exact bytes of `manifest.json`. Because the manifest contains the artifact hashes, a valid manifest signature binds the signature to the exported evidence files.

Install signing support with:

```sh
python -m pip install -e ".[signing]"
```

Generate a PKCS#8 Ed25519 key pair, for example:

```sh
openssl genpkey -algorithm ED25519 -out /usr/local/etc/freebsd-laboratory/evidence-ed25519.pem
openssl pkey -in /usr/local/etc/freebsd-laboratory/evidence-ed25519.pem -pubout -out /usr/local/etc/freebsd-laboratory/evidence-ed25519.pub.pem
```

Then enable `evidence.signing` in `lab.yaml`:

```yaml
evidence:
  signing:
    enabled: true
    algorithm: ed25519
    key_id: software-cloud-lab
    private_key: /usr/local/etc/freebsd-laboratory/evidence-ed25519.pem
```

Verify an export against a trusted institutional public key:

```sh
freebsd-lab-verify-evidence .freebsd-lab/evidence/<session-id> \
  --public-key /usr/local/etc/freebsd-laboratory/evidence-ed25519.pub.pem
```

Without `--public-key`, the verifier can prove that the embedded key signed the manifest, but it cannot establish that the embedded key belongs to a trusted runner or institution.

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

Linux CI validates the portable evidence/state model, Ed25519 signing and verification, runtime reconciliation logic, address allocation, provisioner helpers, and TypeScript compilation. Actual VNET/epair, ZFS, jail and bhyve lifecycles require a FreeBSD runner and are not simulated by the standard Linux CI job.
