# FreeBSD Laboratory

Executable FreeBSD documentation for JupyterLab. The project separates the notebook UI, server-owned evidence, and the runtime that actually executes a kernel.

## Current implementation

The prototype contains four executable boundaries:

1. **JupyterLab extension** — adds the `Lab progression` right sidebar, records notebook execution observations, and adds `Export evidence` to notebook toolbars.
2. **Jupyter Server extension** — owns the evidence session, validates client event types, derives trust stages, and exports hashed evidence files.
3. **FreeBSD jail kernel provisioner** — clones a ZFS template snapshot, creates a disposable jail, mirrors the Jupyter connection file into it, and launches the kernel with `jexec`.
4. **FreeBSD bhyve kernel provisioner** — creates an ephemeral vm-bhyve guest, assigns it a private transport address, stages the connection file over SSH, runs ipykernel inside the VM, and destroys the VM after the kernel stops.

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

`freebsd-lab-install-kernel` installs both runtime choices:

```text
FreeBSD (Python 3)          -> disposable jail
FreeBSD (Python 3, bhyve)  -> disposable bhyve VM
```

The sample notebook remains jail-backed by default. Select the bhyve kernelspec from the JupyterLab kernel picker for experiments that require a separate kernel, stronger isolation, boot behavior, virtual hardware, or privileged guest networking.

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

Each jail kernel launch follows this lifecycle:

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

The initial jail prototype uses `ip4=inherit` so the jailed kernel can communicate with the local Jupyter Server. That is a functional bridge, not the final network-isolation design. A dedicated private loopback/VNET transport remains the jail hardening target.

## bhyve prerequisites

The bhyve provisioner uses `vm-bhyve` as a lifecycle manager around FreeBSD bhyve. It fails closed unless the Jupyter Server is running on FreeBSD with root privileges and the required `vm`, `ssh`, `scp`, and SSH-key files exist.

A minimal host configuration needs:

- initialized vm-bhyve storage;
- a `freebsdlab` vm-bhyve switch reachable from the host;
- the bundled `freebsd-lab.conf` template copied into the vm-bhyve `.templates` directory;
- a prepared vm-bhyve image named `freebsd-python.raw` by default;
- a guest account named `freebsd` by default with SSH and `/usr/local/bin/python3` + `ipykernel`;
- cloud-init support in the prepared image so vm-bhyve can inject the SSH key and per-kernel static address;
- an Ed25519 key pair at `/usr/local/etc/freebsd-laboratory/id_ed25519{,.pub}` by default.

The bundled template is:

```text
freebsd_laboratory/vm-bhyve/freebsd-lab.conf
```

It uses a virtio network interface attached to the `freebsdlab` switch and a sparse ZFS-backed disk. The default Jupyter transport network is host-only:

```text
host/switch: 172.31.254.1/24
lease pool:  172.31.254.100-172.31.254.199
```

A representative vm-bhyve host setup is:

```sh
pkg install vm-bhyve
sysrc vm_enable=YES
sysrc vm_dir="zfs:zroot/vm"
vm init
vm switch create -a 172.31.254.1/24 freebsdlab
install -d -m 0700 /usr/local/etc/freebsd-laboratory
ssh-keygen -t ed25519 -N '' -f /usr/local/etc/freebsd-laboratory/id_ed25519
```

Copy `freebsd_laboratory/vm-bhyve/freebsd-lab.conf` to `<vm-dir-mountpoint>/.templates/freebsd-lab.conf`, and import or build the prepared `freebsd-python.raw` image in the configured vm-bhyve datastore before selecting the bhyve kernelspec.

Each bhyve kernel launch follows this lifecycle:

```text
kernel requested
  -> reserve private IPv4 address
  -> vm create -t freebsd-lab -i <image> -C -k <pubkey> -n <netconfig>
  -> vm start
  -> wait for authenticated SSH
  -> rewrite Jupyter connection IP to the VM address
  -> copy connection file to guest
  -> SSH /usr/local/bin/python3 -m ipykernel_launcher ...
  -> kernel shutdown
  -> vm poweroff -f
  -> vm destroy -f
  -> release address lease
```

The address allocator is host-side and file locked so concurrent kernels do not receive the same transport address. Each VM also gets an isolated `known_hosts` file. The provisioner checks for an existing VM of the generated name before creation and will not intentionally replace it.

The bhyve kernelspec uses Jupyter message-based interrupts because the kernel process is remote from the Jupyter Server process.

## bhyve configuration

The default bhyve settings are declared in `freebsd-python-bhyve/kernel.json` and can be overridden through Jupyter provisioner configuration:

```text
vm_template        freebsd-lab
vm_image           freebsd-python.raw
network_cidr       172.31.254.0/24
address_start      172.31.254.100
address_end        172.31.254.199
network_interface  vtnet0
ssh_user           freebsd
startup_timeout    90 seconds
```

`gateway4`, `nameservers`, and `user_data_file` are optional. With no gateway configured, the default transport network is intended only for host-to-VM kernel traffic.

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

Linux CI tests the portable evidence/state model, bhyve address/command helpers, and TypeScript compilation. Real jail and bhyve lifecycles require a FreeBSD runner and are intentionally not simulated by the standard Linux CI job.
