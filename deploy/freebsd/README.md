# FreeBSD host deployment

This directory contains host-side deployment references for the privileged runtime daemon, persistent laboratory bridge, PF host-isolation policy, collision-safe SSH tunnel ports, and rebuildable FreeBSD golden images.

## rc.d service

Install the service template and create the operator group:

```sh
install -m 0555 deploy/freebsd/rc.d/freebsd_lab_daemon /usr/local/etc/rc.d/freebsd_lab_daemon
pw groupshow freebsdlab >/dev/null 2>&1 || pw groupadd freebsdlab
```

The service uses the standard FreeBSD `rc.subr` interface and `daemon(8)` supervision. It loads `if_bridge` and `if_epair`, creates the runtime state directories, starts `freebsd-lab-runtime-daemon` as root, writes a supervisor pidfile, and sends daemon output to syslog.

The defaults match the repository runtime configuration:

```text
socket:        /var/run/freebsd-laboratory/runtime.sock
group:         freebsdlab
network:       172.31.254.0/24
host address:  172.31.254.1
address pool:  172.31.254.10-172.31.254.199
bridge:        labbridge0
```

The service also creates the shared tunnel-port lease directory:

```text
/var/run/freebsd-laboratory/tunnel-port-leases
owner: root
 group: freebsdlab
 mode: 2770
```

The setgid directory gives every authorized Jupyter process the same host-wide coordination namespace without granting access outside the existing `freebsdlab` operator group.

Merge the relevant settings from `deploy/freebsd/rc.conf.snippet` into `/etc/rc.conf`. The sample creates an ordinary `bridge0` clone and immediately renames it to the stable name `labbridge0`; if `bridge0` is already in use, select another unused `bridge<N>` clone and adjust the corresponding variable names. Creating the named bridge through the normal network startup path makes `labbridge0` available before PF loads its interface-bound anchor.

Service-specific values can be overridden through `/etc/rc.conf`. Additional daemon arguments can be supplied through `freebsd_lab_daemon_runtime_args`; keep that variable root-controlled because it selects privileged runtime resources such as the active jail snapshot.

```sh
sysrc freebsd_lab_daemon_enable=YES
service freebsd_lab_daemon start
service freebsd_lab_daemon status
```

The service also sets the bridge packet-filter controls described below after startup unless `freebsd_lab_daemon_configure_bridge_filter=NO` is explicitly configured.

## Bridge packet-filter controls

The PF policy is evaluated on `labbridge0`, not separately on every epair or bhyve tap member. The required bridge pfil settings are:

```text
net.link.bridge.pfil_bridge=1
net.link.bridge.pfil_member=0
```

For persistent module-specific configuration, install the supplied `if_bridge` settings:

```sh
install -d -m 0755 /etc/sysctl.kld.d
install -m 0644 deploy/freebsd/sysctl.kld.d/if_bridge.conf /etc/sysctl.kld.d/if_bridge.conf
sysctl net.link.bridge.pfil_bridge=1
sysctl net.link.bridge.pfil_member=0
```

The rc.d service reasserts the same values after loading `if_bridge`.

## PF host-isolation policy

The laboratory bridge has no physical uplink, but the host intentionally owns `172.31.254.1` on `labbridge0`. The transport is SSH-only at the L3/L4 boundary: Jupyter's shell, IOPub, stdin, control and heartbeat TCP channels bind to loopback in each runtime and are carried through SSH local forwards.

Install the anchor file:

```sh
install -d -m 0755 /usr/local/etc/pf.anchors
install -m 0644 deploy/freebsd/pf.anchors/freebsd-lab /usr/local/etc/pf.anchors/freebsd-lab
```

**Installing the anchor file alone does not activate it.** The host's main `/etc/pf.conf` must contain both declarations from `deploy/freebsd/pf.conf.snippet`:

```pf
anchor "freebsd-lab" on labbridge0
load anchor "freebsd-lab" from "/usr/local/etc/pf.anchors/freebsd-lab"
```

The `anchor` rule is the point at which PF evaluates the laboratory filter rules; the `load anchor` line populates that anchor from the repository policy file. Keep these declarations before any broad `quick` pass rule that would otherwise accept traffic on `labbridge0`.

Validate the complete configuration first, then explicitly reload the main ruleset so the anchor becomes active:

```sh
pfctl -nf /etc/pf.conf
pfctl -f /etc/pf.conf
pfctl -a freebsd-lab -sr
```

The repository also provides a fail-closed helper. Its default mode checks syntax and both required main-ruleset references without changing the live firewall; `--reload` performs the reload and then verifies that the active anchor contains filter rules:

```sh
deploy/freebsd/validate-pf.sh check
deploy/freebsd/validate-pf.sh --reload
```

If PF is not already enabled, enable it before relying on this boundary:

```sh
sysrc pf_enable=YES
service pf start
```

### Policy behavior

The reference anchor implements:

```text
host 172.31.254.1 -> runtime TCP/22: pass, stateful
host -> runtime any other IPv4 flow:   block + log
runtime -> host/routed IPv4:           block + log
SSH reply traffic:                     allowed by PF state
```

Consequently, a runtime cannot initiate a connection to the host's SSH daemon, database, NFS service, Jupyter HTTP endpoint, or another routed destination through this interface. The host also cannot silently start depending on direct Jupyter ZMQ ports because only runtime TCP/22 is allowed outbound on `labbridge0`.

Add a narrowly scoped exception only when a laboratory has an explicit host-service dependency. The sample covers IPv4; an IPv6 runtime transport needs a separate policy.

## SSH tunnel port allocation

The provisioners do not reuse Jupyter's original random connection ports directly. Before staging the connection file, they allocate five host-side ports from a shared configurable pool. Defaults are:

```text
lease directory: /var/run/freebsd-laboratory/tunnel-port-leases
port range:      30000-44999
ports/session:   5
bind address:    127.0.0.1
```

Allocation is serialized by `flock(2)` on the shared lease directory. Each candidate port is also actually bound on `127.0.0.1` before it is accepted, so ports already owned by the host are skipped. The five reservation sockets stay open throughout runtime creation, SSH readiness checks, connection-file rewriting, and SCP staging.

Immediately before `LocalProvisioner.launch_kernel()` starts OpenSSH, those reservation sockets are closed so `ssh -L` can acquire the listeners. The lease files remain owned by that kernel until cleanup. This prevents a second FreeBSD Laboratory kernel—whether launched concurrently in the same Jupyter process or by another authorized Jupyter process—from selecting any of the five ports during the handoff or while the SSH tunnel is alive.

The guarantee covers cooperating FreeBSD Laboratory sessions that share this lease directory. An unrelated host process can still race to bind a port after the reservation socket is handed to OpenSSH; `ExitOnForwardFailure=yes` makes that exceptional race fail kernel startup rather than silently attaching to the wrong listener.

The port range and lease directory are traitlet-backed provisioner settings (`tunnel_port_start`, `tunnel_port_end`, `tunnel_lease_dir`) and can be overridden per kernelspec for a host with a reserved local service range.

## Golden image lifecycle

The runtime artifacts are not treated as hand-maintained snowflakes. `deploy/freebsd/images/` contains FreeBSD-native builders for both execution paths:

```text
build-golden-images.sh   common source/revision gate and one-command rebuild
build-jail-template.sh   versioned ZFS jail template + @clean snapshot
build-bhyve-image.sh     FreeBSD release vm-image -> versioned raw disk
vmimage.conf             release image customization hook
sshd-freebsd-lab.conf    restricted SSH policy shared by both images
```

The wrapper requires a FreeBSD Git source tree and, by default, a `releng/*` branch. It builds world/kernel, produces both artifacts with the same build id, validates `ipykernel`, runs `pkg audit -F`, records source provenance, and keeps activation separate from construction.

For a local Poudriere package repository, set `LAB_PKG_REPOS_DIR` to a root-controlled pkg repository-configuration directory. Package names can be overridden with `LAB_JAIL_PACKAGES` and `LAB_VM_PACKAGES`.

```sh
sudo env SRC_DIR=/usr/src LAB_PKG_REPOS_DIR=/usr/local/etc/pkg/lab-repos \
  deploy/freebsd/images/build-golden-images.sh
```

See `deploy/freebsd/images/README.md` for versioned activation and rollback procedures.

## SSH transport resiliency

Both kernel provisioners use the same SSH policy:

```text
ConnectTimeout=5
ConnectionAttempts=3
ServerAliveInterval=15
ServerAliveCountMax=4
TCPKeepAlive=yes
ExitOnForwardFailure=yes
```

The five Jupyter TCP channels are local forwards on the attached kernel SSH process. A transient period of packet loss therefore gets several SSH server-alive intervals before the transport is declared dead. If the SSH process ultimately exits, Jupyter observes the kernel process failure rather than leaving an apparently live kernel with unreachable direct TCP sockets.

These values are provisioner configuration, not protocol constants, and can be overridden per kernelspec if a deployment needs a different failure-detection window.

## Deployment files

```text
deploy/freebsd/rc.d/freebsd_lab_daemon
    rc.d service installed as /usr/local/etc/rc.d/freebsd_lab_daemon

deploy/freebsd/rc.conf.snippet
    persistent bridge clone/rename, daemon enablement, and PF enablement reference

deploy/freebsd/sysctl.kld.d/if_bridge.conf
    module-specific bridge pfil defaults

deploy/freebsd/pf.anchors/freebsd-lab
    SSH-only laboratory host-isolation rules

deploy/freebsd/pf.conf.snippet
    mandatory anchor declarations for /etc/pf.conf

deploy/freebsd/validate-pf.sh
    syntax/reference check and explicit PF reload verification helper

deploy/freebsd/images/
    reproducible jail and bhyve golden-image builders
```

The Linux GitHub Actions workflow syntax-checks the rc.d, PF helper, and image-builder shell assets and runs portable unit tests. Real `rcorder`, `pfctl`, ZFS, VNET, bhyve and golden-image build validation must execute on a dedicated FreeBSD environment; GitHub's official Actions runner application does not currently support FreeBSD as a self-hosted runner OS.
