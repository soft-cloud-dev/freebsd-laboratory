# FreeBSD host deployment

This directory contains host-side deployment references for the privileged runtime daemon and the private laboratory firewall policy.

## rc.d service

Install the service template and enable it:

```sh
install -m 0555 deploy/freebsd/rc.d/freebsd_lab_daemon /usr/local/etc/rc.d/freebsd_lab_daemon
pw groupshow freebsdlab >/dev/null 2>&1 || pw groupadd freebsdlab
sysrc freebsd_lab_daemon_enable=YES
service freebsd_lab_daemon start
service freebsd_lab_daemon status
```

The default service configuration matches the repository runtime defaults:

```text
socket:        /var/run/freebsd-laboratory/runtime.sock
group:         freebsdlab
network:       172.31.254.0/24
host address:  172.31.254.1
address pool:  172.31.254.10-172.31.254.199
bridge:        labbridge0
```

Override settings in `/etc/rc.conf` when required. For example:

```sh
sysrc freebsd_lab_daemon_bridge=labbridge0
sysrc freebsd_lab_daemon_network=172.31.254.0/24
sysrc freebsd_lab_daemon_host_address=172.31.254.1
```

Additional daemon arguments can be supplied through `freebsd_lab_daemon_runtime_args`. Keep this root-controlled because those arguments define privileged runtime resources.

The rc.d script uses FreeBSD `daemon(8)` with a supervisor pidfile and syslog output. Stopping the service sends SIGTERM to the supervisor, which forwards it to `freebsd-lab-runtime-daemon`; the daemon removes its Unix socket during normal shutdown.

## PF host-isolation policy

The laboratory bridge deliberately has no physical uplink, but the host owns `172.31.254.1` on `labbridge0`. The reference PF policy allows host-originated connections to runtimes and blocks new runtime-originated IPv4 connections entering through the laboratory bridge.

Install the anchor:

```sh
install -d -m 0755 /usr/local/etc/pf.anchors
install -m 0644 deploy/freebsd/pf.anchors/freebsd-lab /usr/local/etc/pf.anchors/freebsd-lab
```

Merge the two lines from `deploy/freebsd/pf.conf.snippet` into `/etc/pf.conf`. The anchor should appear before any broad `quick` pass rule that would accept traffic on `labbridge0`.

PF filtering on a FreeBSD bridge is controlled by bridge pfil sysctls. Append the settings from `deploy/freebsd/sysctl.conf.snippet` to `/etc/sysctl.conf`, then apply them immediately:

```sh
sysctl net.link.bridge.pfil_bridge=1
sysctl net.link.bridge.pfil_member=0
```

Validate the complete PF configuration before loading it:

```sh
pfctl -nf /etc/pf.conf
pfctl -f /etc/pf.conf
pfctl -a freebsd-lab -sr
```

If PF is not already enabled on the host, enable it through the normal FreeBSD service configuration before relying on this policy:

```sh
sysrc pf_enable=YES
service pf start
```

### Policy behavior

The anchor contains:

```text
host 172.31.254.1 -> runtime network: pass, stateful
runtime -> host/new destinations:      block inbound on labbridge0
return traffic for host-created state: pass by PF state lookup
```

This permits the Jupyter host to establish SSH and kernel connections to jails and bhyve guests without granting those runtimes the ability to initiate connections back to host services. Add explicit `pass in quick` exceptions above the final block only for host services a laboratory is intentionally allowed to consume.

The sample covers the current IPv4-only laboratory network. An IPv6 runtime transport requires a separate IPv6 policy.
