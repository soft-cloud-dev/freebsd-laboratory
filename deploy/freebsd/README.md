# FreeBSD host deployment

This directory contains host-side deployment references for the privileged runtime daemon, persistent laboratory bridge, and PF host-isolation policy.

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

Merge the relevant settings from `deploy/freebsd/rc.conf.snippet` into `/etc/rc.conf`. Do not overwrite an existing `cloned_interfaces` value; append `labbridge0` to it. Creating the bridge through the normal network startup path makes `labbridge0` available before PF loads its interface-bound anchor.

Service-specific values can be overridden through `/etc/rc.conf`. For example:

```sh
sysrc freebsd_lab_daemon_bridge=labbridge0
sysrc freebsd_lab_daemon_network=172.31.254.0/24
sysrc freebsd_lab_daemon_host_address=172.31.254.1
```

Additional daemon arguments can be supplied through `freebsd_lab_daemon_runtime_args`. Keep that variable root-controlled because those arguments define privileged runtime resources.

Start and inspect the service with the normal FreeBSD service interface:

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
```

The rc.d service reasserts the same values after loading `if_bridge`, so the protection does not depend on whether the module happened to be present when the ordinary sysctl files were processed.

To apply the values immediately on an already running host:

```sh
sysctl net.link.bridge.pfil_bridge=1
sysctl net.link.bridge.pfil_member=0
```

## PF host-isolation policy

The laboratory bridge has no physical uplink, but the host intentionally owns `172.31.254.1` on `labbridge0` for runtime management. The reference PF policy allows host-originated stateful connections to runtimes and blocks new runtime-originated IPv4 connections entering through the laboratory bridge.

Install the anchor:

```sh
install -d -m 0755 /usr/local/etc/pf.anchors
install -m 0644 deploy/freebsd/pf.anchors/freebsd-lab /usr/local/etc/pf.anchors/freebsd-lab
```

Merge the two lines from `deploy/freebsd/pf.conf.snippet` into `/etc/pf.conf`. Keep the laboratory anchor before any broad `quick` pass rule that would otherwise accept traffic on `labbridge0`.

Validate the complete PF configuration before replacing the active ruleset:

```sh
pfctl -nf /etc/pf.conf
pfctl -f /etc/pf.conf
pfctl -a freebsd-lab -sr
```

If PF is not already enabled, enable it through the normal FreeBSD service configuration before relying on this isolation boundary:

```sh
sysrc pf_enable=YES
service pf start
```

### Policy behavior

The anchor implements this directionality:

```text
host 172.31.254.1 -> runtime network: pass, stateful
runtime -> host or routed destinations: block new inbound IPv4 on labbridge0
return traffic for host-created state:    pass through PF state
```

This permits Jupyter to establish SSH and kernel connections to VNET jails and bhyve guests without granting those runtimes permission to initiate arbitrary IPv4 sessions toward host services or through the host. Add narrowly scoped `pass in quick` exceptions above the final block only for host services a laboratory is intentionally allowed to consume.

The sample covers the current IPv4-only laboratory transport. An IPv6 runtime transport requires a separate IPv6 policy.

## Deployment files

```text
deploy/freebsd/rc.d/freebsd_lab_daemon
    rc.d service installed as /usr/local/etc/rc.d/freebsd_lab_daemon

deploy/freebsd/rc.conf.snippet
    persistent labbridge0, daemon enablement, and PF enablement reference

deploy/freebsd/sysctl.kld.d/if_bridge.conf
    module-specific bridge pfil defaults

deploy/freebsd/pf.anchors/freebsd-lab
    laboratory host-isolation rules

deploy/freebsd/pf.conf.snippet
    anchor declarations for /etc/pf.conf
```

The Linux GitHub Actions workflow can syntax-check the shell service template, but final validation of `rcorder`, `service`, bridge pfil behavior, and `pfctl -nf` must run on a FreeBSD host or FreeBSD CI runner.
