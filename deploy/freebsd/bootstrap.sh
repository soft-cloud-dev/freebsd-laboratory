#!/bin/sh
set -eu

log()
{
    printf '\n==> %s\n' "$*"
}

fail()
{
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

is_yes()
{
    case "$1" in
        YES|yes|Y|y|1|true|TRUE) return 0 ;;
        *) return 1 ;;
    esac
}

if [ "$(uname -s)" != "FreeBSD" ]; then
    fail "bootstrap.sh requires FreeBSD"
fi
if [ "$(id -u)" -ne 0 ]; then
    fail "bootstrap.sh must run as root"
fi

LAB_REPO_URL=${LAB_REPO_URL:-https://github.com/soft-cloud-dev/freebsd-laboratory.git}
LAB_REPO_REF=${LAB_REPO_REF:-main}
LAB_REPO_DIR=${LAB_REPO_DIR:-}
LAB_UPDATE_REPO=${LAB_UPDATE_REPO:-NO}
LAB_JUPYTER_USER=${LAB_JUPYTER_USER:-}
LAB_GROUP=${LAB_GROUP:-freebsdlab}
LAB_DAEMON_VENV=${LAB_DAEMON_VENV:-/usr/local/libexec/freebsd-laboratory/daemon-venv}
LAB_SRC_DIR=${LAB_SRC_DIR:-/usr/src}
LAB_SRC_BRANCH=${LAB_SRC_BRANCH:-}
LAB_BUILD_JAIL_IMAGE=${LAB_BUILD_JAIL_IMAGE:-YES}
LAB_REBUILD_JAIL_IMAGE=${LAB_REBUILD_JAIL_IMAGE:-NO}
LAB_SMOKE_TEST=${LAB_SMOKE_TEST:-YES}
LAB_ZFS_POOL=${LAB_ZFS_POOL:-}
LAB_BRIDGE_CLONE=${LAB_BRIDGE_CLONE:-bridge0}
LAB_BRIDGE_NAME=${LAB_BRIDGE_NAME:-labbridge0}
LAB_NETWORK=${LAB_NETWORK:-172.31.254.0/24}
LAB_HOST_ADDRESS=${LAB_HOST_ADDRESS:-172.31.254.1}
LAB_ADDRESS_START=${LAB_ADDRESS_START:-172.31.254.10}
LAB_ADDRESS_END=${LAB_ADDRESS_END:-172.31.254.199}
LAB_JAIL_MOUNT_ROOT=${LAB_JAIL_MOUNT_ROOT:-/usr/local/jails/containers}
LAB_CONFIGURE_PF=${LAB_CONFIGURE_PF:-YES}
LAB_PF_CONF=${LAB_PF_CONF:-/etc/pf.conf}
LAB_SSH_KEY=${LAB_SSH_KEY:-/usr/local/etc/freebsd-laboratory/id_ed25519}

if [ -z "$LAB_JUPYTER_USER" ]; then
    if pw usershow freebsd >/dev/null 2>&1; then
        LAB_JUPYTER_USER=freebsd
    else
        LAB_JUPYTER_USER=root
    fi
fi
if ! pw usershow "$LAB_JUPYTER_USER" >/dev/null 2>&1; then
    fail "Jupyter user does not exist: $LAB_JUPYTER_USER"
fi

JUPYTER_HOME=$(pw usershow -n "$LAB_JUPYTER_USER" -7 | awk -F: '{print $6}')
[ -n "$JUPYTER_HOME" ] || fail "Unable to determine home for $LAB_JUPYTER_USER"
JUPYTER_GROUP=$(id -gn "$LAB_JUPYTER_USER")

if [ -z "$LAB_REPO_DIR" ]; then
    LAB_REPO_DIR="${JUPYTER_HOME%/}/freebsd-laboratory"
fi
case "$LAB_REPO_DIR" in
    /|"") fail "LAB_REPO_DIR must not be / or empty" ;;
    *[!A-Za-z0-9_./-]*) fail "LAB_REPO_DIR contains unsupported characters: $LAB_REPO_DIR" ;;
esac
case "$LAB_DAEMON_VENV" in
    /usr/local/libexec/freebsd-laboratory/*) ;;
    *) fail "LAB_DAEMON_VENV must stay below /usr/local/libexec/freebsd-laboratory" ;;
esac

log "Bootstrapping package manager and base tools"
env ASSUME_ALWAYS_YES=yes pkg bootstrap >/dev/null 2>&1 || true
pkg update -f
pkg install -y git python3 npm

PYTHON=/usr/local/bin/python3
[ -x "$PYTHON" ] || fail "python3 was not installed at $PYTHON"
PY_TAG=$($PYTHON -c 'import sys; print(f"py{sys.version_info.major}{sys.version_info.minor}")')
PY_NUM=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")')

log "Installing FreeBSD binary Python/Jupyter dependencies for $PY_TAG"
pkg install -y \
    "${PY_TAG}-pip" \
    "${PY_TAG}-setuptools" \
    "${PY_TAG}-wheel" \
    "${PY_TAG}-jupyterlab" \
    "${PY_TAG}-ipykernel" \
    "${PY_TAG}-cryptography" \
    "${PY_TAG}-pytest" \
    "${PY_TAG}-pyyaml"

log "Preparing repository at $LAB_REPO_DIR"
if [ -d "$LAB_REPO_DIR/.git" ]; then
    if is_yes "$LAB_UPDATE_REPO"; then
        git -C "$LAB_REPO_DIR" fetch origin "$LAB_REPO_REF"
        git -C "$LAB_REPO_DIR" merge --ff-only FETCH_HEAD
    else
        printf 'Repository already exists; leaving checkout unchanged (LAB_UPDATE_REPO=NO).\n'
    fi
elif [ -e "$LAB_REPO_DIR" ]; then
    fail "Repository destination exists but is not a Git checkout: $LAB_REPO_DIR"
else
    install -d -m 0755 "$(dirname "$LAB_REPO_DIR")"
    git clone --depth 1 --branch "$LAB_REPO_REF" "$LAB_REPO_URL" "$LAB_REPO_DIR"
fi

[ -f "$LAB_REPO_DIR/pyproject.toml" ] || fail "Repository pyproject.toml is missing"
[ -f "$LAB_REPO_DIR/lab.yaml" ] || fail "Repository lab.yaml is missing"

log "Creating operator group, SSH transport key, and host state"
pw groupshow "$LAB_GROUP" >/dev/null 2>&1 || pw groupadd "$LAB_GROUP"
pw groupmod "$LAB_GROUP" -m "$LAB_JUPYTER_USER"

install -d -o root -g "$LAB_GROUP" -m 0750 /usr/local/etc/freebsd-laboratory
if [ ! -f "$LAB_SSH_KEY" ]; then
    ssh-keygen -q -t ed25519 -N '' -f "$LAB_SSH_KEY"
fi
[ -f "${LAB_SSH_KEY}.pub" ] || fail "SSH public key is missing: ${LAB_SSH_KEY}.pub"
chown root:"$LAB_GROUP" "$LAB_SSH_KEY"
chmod 0640 "$LAB_SSH_KEY"
chown root:wheel "${LAB_SSH_KEY}.pub"
chmod 0644 "${LAB_SSH_KEY}.pub"

if [ -z "$LAB_ZFS_POOL" ]; then
    if zpool list -H -o name zroot >/dev/null 2>&1; then
        LAB_ZFS_POOL=zroot
    else
        POOLS=$(zpool list -H -o name)
        POOL_COUNT=$(printf '%s\n' "$POOLS" | awk 'NF {count++} END {print count+0}')
        if [ "$POOL_COUNT" -eq 1 ]; then
            LAB_ZFS_POOL=$(printf '%s\n' "$POOLS" | awk 'NF {print; exit}')
        else
            fail "Set LAB_ZFS_POOL explicitly; unable to select one ZFS pool"
        fi
    fi
fi
zpool list "$LAB_ZFS_POOL" >/dev/null 2>&1 || fail "ZFS pool does not exist: $LAB_ZFS_POOL"

JAIL_TEMPLATE_PARENT="${LAB_ZFS_POOL}/jails/templates"
JAIL_DATASET_PARENT="${LAB_ZFS_POOL}/jails/containers"
for dataset in "$JAIL_TEMPLATE_PARENT" "$JAIL_DATASET_PARENT"; do
    if ! zfs list -H -o name "$dataset" >/dev/null 2>&1; then
        zfs create -p "$dataset"
    fi
done
install -d -m 0755 /usr/local/jails/templates "$LAB_JAIL_MOUNT_ROOT"

log "Installing project into isolated host environments"
rm -rf "$LAB_DAEMON_VENV"
install -d -o root -g wheel -m 0755 "$(dirname "$LAB_DAEMON_VENV")"
$PYTHON -m venv --system-site-packages "$LAB_DAEMON_VENV"
"$LAB_DAEMON_VENV/bin/python" -m pip install \
    --no-deps --no-build-isolation "$LAB_REPO_DIR"
chown -R root:wheel "$LAB_DAEMON_VENV"
chmod -R go-w "$LAB_DAEMON_VENV"

JUPYTER_VENV="${LAB_REPO_DIR%/}/.venv"
rm -rf "$JUPYTER_VENV"
$PYTHON -m venv --system-site-packages "$JUPYTER_VENV"
"$JUPYTER_VENV/bin/python" -m pip install \
    --no-deps --no-build-isolation -e "$LAB_REPO_DIR"

log "Building and registering the JupyterLab extension"
(
    cd "$LAB_REPO_DIR/labextension"
    npm install --no-audit --no-fund
    npm run build
)
HOME="$JUPYTER_HOME" "$JUPYTER_VENV/bin/jupyter" server extension enable \
    --py freebsd_laboratory --sys-prefix
HOME="$JUPYTER_HOME" "$JUPYTER_VENV/bin/jupyter" labextension develop \
    "$LAB_REPO_DIR/labextension" --overwrite
HOME="$JUPYTER_HOME" "$JUPYTER_VENV/bin/freebsd-lab-install-kernel"

if is_yes "$LAB_BUILD_JAIL_IMAGE"; then
    RELEASE=$(freebsd-version -u 2>/dev/null || uname -r)
    RELEASE_SERIES=$(printf '%s\n' "$RELEASE" | sed -E 's/^([0-9]+\.[0-9]+).*/\1/')
    case "$RELEASE_SERIES" in
        [0-9]*.[0-9]*) ;;
        *) fail "Unable to derive a RELEASE series from: $RELEASE" ;;
    esac
    if [ -z "$LAB_SRC_BRANCH" ]; then
        LAB_SRC_BRANCH="releng/$RELEASE_SERIES"
    fi
    case "$LAB_SRC_BRANCH" in
        releng/*) ;;
        *) fail "LAB_SRC_BRANCH must be an explicit releng/* branch for golden images" ;;
    esac

    log "Preparing FreeBSD source tree $LAB_SRC_BRANCH in $LAB_SRC_DIR"
    if [ -d "$LAB_SRC_DIR/.git" ]; then
        git -C "$LAB_SRC_DIR" fetch --depth 1 origin "$LAB_SRC_BRANCH"
        git -C "$LAB_SRC_DIR" checkout -B "$LAB_SRC_BRANCH" FETCH_HEAD
    elif [ -e "$LAB_SRC_DIR" ]; then
        if [ -z "$(ls -A "$LAB_SRC_DIR" 2>/dev/null)" ]; then
            rmdir "$LAB_SRC_DIR"
            git clone --depth 1 --branch "$LAB_SRC_BRANCH" \
                https://git.FreeBSD.org/src.git "$LAB_SRC_DIR"
        else
            fail "$LAB_SRC_DIR exists but is not a Git source checkout"
        fi
    else
        git clone --depth 1 --branch "$LAB_SRC_BRANCH" \
            https://git.FreeBSD.org/src.git "$LAB_SRC_DIR"
    fi
    [ -f "$LAB_SRC_DIR/Makefile" ] || fail "FreeBSD source Makefile is missing"

    EXISTING_SNAPSHOT=$(zfs list -H -t snapshot -o name -s creation -r "$JAIL_TEMPLATE_PARENT" 2>/dev/null \
        | grep "/freebsd-python-.*@clean$" | tail -1 || true)

    if [ -n "$EXISTING_SNAPSHOT" ] && ! is_yes "$LAB_REBUILD_JAIL_IMAGE"; then
        ACTIVE_SNAPSHOT=$EXISTING_SNAPSHOT
        printf 'Using existing jail template snapshot: %s\n' "$ACTIVE_SNAPSHOT"
    else
        JOBS=$(sysctl -n hw.ncpu)
        log "Building FreeBSD world with $JOBS jobs; this is the long bootstrap phase"
        make -C "$LAB_SRC_DIR" -j"$JOBS" buildworld

        log "Building versioned VNET jail golden image"
        env \
            SRC_DIR="$LAB_SRC_DIR" \
            JAIL_DATASET_PREFIX="${JAIL_TEMPLATE_PARENT}/freebsd-python" \
            JAIL_MOUNT_ROOT=/usr/local/jails/templates \
            LAB_JAIL_PACKAGES="python${PY_NUM} ${PY_TAG}-ipykernel" \
            "$LAB_REPO_DIR/deploy/freebsd/images/build-jail-template.sh"

        ACTIVE_SNAPSHOT=$(zfs list -H -t snapshot -o name -s creation -r "$JAIL_TEMPLATE_PARENT" \
            | grep "/freebsd-python-.*@clean$" | tail -1 || true)
        [ -n "$ACTIVE_SNAPSHOT" ] || fail "Golden-image builder produced no @clean snapshot"
    fi
else
    ACTIVE_SNAPSHOT=""
fi

log "Installing FreeBSD host service and packet-filter assets"
install -m 0555 "$LAB_REPO_DIR/deploy/freebsd/rc.d/freebsd_lab_daemon" \
    /usr/local/etc/rc.d/freebsd_lab_daemon
install -d -m 0755 /etc/sysctl.kld.d /usr/local/etc/pf.anchors
install -m 0644 "$LAB_REPO_DIR/deploy/freebsd/sysctl.kld.d/if_bridge.conf" \
    /etc/sysctl.kld.d/if_bridge.conf

PF_ANCHOR_TMP=$(mktemp /tmp/freebsd-lab-anchor.XXXXXX)
trap 'rm -f "$PF_ANCHOR_TMP"' EXIT HUP INT TERM
sed \
    -e "s/^lab_if = .*/lab_if = \"$LAB_BRIDGE_NAME\"/" \
    -e "s#^lab_net = .*#lab_net = \"$LAB_NETWORK\"#" \
    -e "s/^lab_host = .*/lab_host = \"$LAB_HOST_ADDRESS\"/" \
    "$LAB_REPO_DIR/deploy/freebsd/pf.anchors/freebsd-lab" > "$PF_ANCHOR_TMP"
install -m 0644 "$PF_ANCHOR_TMP" /usr/local/etc/pf.anchors/freebsd-lab
rm -f "$PF_ANCHOR_TMP"
trap - EXIT HUP INT TERM

install -m 0555 "$LAB_REPO_DIR/deploy/freebsd/validate-pf.sh" \
    /usr/local/sbin/freebsd-lab-validate-pf

CLONED=$(sysrc -n cloned_interfaces 2>/dev/null || true)
case " $CLONED " in
    *" $LAB_BRIDGE_CLONE "*) ;;
    *) sysrc cloned_interfaces="${CLONED:+$CLONED }$LAB_BRIDGE_CLONE" ;;
esac
sysrc "ifconfig_${LAB_BRIDGE_CLONE}_name=$LAB_BRIDGE_NAME"
sysrc "ifconfig_${LAB_BRIDGE_NAME}=inet ${LAB_HOST_ADDRESS}/${LAB_NETWORK#*/} up"
sysrc freebsd_lab_daemon_enable=YES
sysrc "freebsd_lab_daemon_runtime_program=$LAB_DAEMON_VENV/bin/freebsd-lab-runtime-daemon"
sysrc "freebsd_lab_daemon_group=$LAB_GROUP"
sysrc "freebsd_lab_daemon_network=$LAB_NETWORK"
sysrc "freebsd_lab_daemon_host_address=$LAB_HOST_ADDRESS"
sysrc "freebsd_lab_daemon_address_start=$LAB_ADDRESS_START"
sysrc "freebsd_lab_daemon_address_end=$LAB_ADDRESS_END"
sysrc "freebsd_lab_daemon_bridge=$LAB_BRIDGE_NAME"

RUNTIME_ARGS="--jail-dataset-parent=$JAIL_DATASET_PARENT --jail-mount-root=$LAB_JAIL_MOUNT_ROOT --ssh-public-key=${LAB_SSH_KEY}.pub"
if [ -n "$ACTIVE_SNAPSHOT" ]; then
    RUNTIME_ARGS="--jail-template=$ACTIVE_SNAPSHOT $RUNTIME_ARGS"
fi
sysrc "freebsd_lab_daemon_runtime_args=$RUNTIME_ARGS"

kldload if_bridge >/dev/null 2>&1 || true
kldload if_epair >/dev/null 2>&1 || true
if ! ifconfig "$LAB_BRIDGE_NAME" >/dev/null 2>&1; then
    ifconfig bridge create name "$LAB_BRIDGE_NAME" >/dev/null
fi
if ! ifconfig "$LAB_BRIDGE_NAME" | grep -q "inet $LAB_HOST_ADDRESS "; then
    ifconfig "$LAB_BRIDGE_NAME" inet "${LAB_HOST_ADDRESS}/${LAB_NETWORK#*/}" up
else
    ifconfig "$LAB_BRIDGE_NAME" up
fi
sysctl net.link.bridge.pfil_bridge=1 >/dev/null
sysctl net.link.bridge.pfil_member=0 >/dev/null

if is_yes "$LAB_CONFIGURE_PF"; then
    log "Installing and validating the PF anchor"
    PF_ANCHOR="anchor \"freebsd-lab\" on $LAB_BRIDGE_NAME"
    PF_LOAD='load anchor "freebsd-lab" from "/usr/local/etc/pf.anchors/freebsd-lab"'
    PF_TMP=$(mktemp /tmp/freebsd-lab-pf.XXXXXX)
    trap 'rm -f "$PF_TMP"' EXIT HUP INT TERM
    if [ -f "$LAB_PF_CONF" ]; then
        PF_EXISTING="$LAB_PF_CONF"
    else
        PF_EXISTING=/dev/null
    fi
    {
        if ! grep -Fqx "$PF_ANCHOR" "$PF_EXISTING" 2>/dev/null; then
            printf '%s\n' "$PF_ANCHOR"
        fi
        if ! grep -Fqx "$PF_LOAD" "$PF_EXISTING" 2>/dev/null; then
            printf '%s\n' "$PF_LOAD"
        fi
        cat "$PF_EXISTING"
    } > "$PF_TMP"
    pfctl -nf "$PF_TMP"
    if [ -f "$LAB_PF_CONF" ]; then
        PF_BACKUP="${LAB_PF_CONF}.freebsd-lab.$(date -u +%Y%m%dT%H%M%SZ).bak"
        cp -p "$LAB_PF_CONF" "$PF_BACKUP"
        printf 'Saved previous PF ruleset to %s\n' "$PF_BACKUP"
    fi
    install -m 0600 "$PF_TMP" "$LAB_PF_CONF"
    rm -f "$PF_TMP"
    trap - EXIT HUP INT TERM
    sysrc pf_enable=YES
    if service pf status >/dev/null 2>&1; then
        pfctl -f "$LAB_PF_CONF"
    else
        service pf onestart
    fi
    pfctl -a freebsd-lab -sr >/dev/null
fi

log "Starting privileged runtime daemon"
if service freebsd_lab_daemon status >/dev/null 2>&1; then
    service freebsd_lab_daemon restart
else
    service freebsd_lab_daemon start
fi
"$JUPYTER_VENV/bin/python" -c \
    'from freebsd_laboratory.runtime_client import RuntimeClient; print(RuntimeClient().ping())'

if is_yes "$LAB_SMOKE_TEST" && [ -n "$ACTIVE_SNAPSHOT" ]; then
    log "Running a real VNET jail smoke test"
    SMOKE_SUFFIX="bs$$"
    SMOKE_NAME="freebsd-lab-$SMOKE_SUFFIX"
    cleanup_smoke()
    {
        "$JUPYTER_VENV/bin/python" - "$SMOKE_NAME" <<'PY' >/dev/null 2>&1 || true
import sys
from freebsd_laboratory.runtime_client import RuntimeClient
RuntimeClient().destroy(sys.argv[1])
PY
    }
    trap cleanup_smoke EXIT HUP INT TERM
    "$JUPYTER_VENV/bin/python" - "$SMOKE_NAME" <<'PY'
import os
import sys
from freebsd_laboratory.runtime_client import RuntimeClient
print(RuntimeClient().create_jail(sys.argv[1], os.getpid()))
PY
    JAILED=$(jexec "$SMOKE_NAME" sysctl -n security.jail.jailed)
    [ "$JAILED" = "1" ] || fail "Smoke runtime is not jailed: security.jail.jailed=$JAILED"
    jexec "$SMOKE_NAME" ifconfig vnet0 >/dev/null
    cleanup_smoke
    trap - EXIT HUP INT TERM
fi

log "Assigning the Jupyter checkout to $LAB_JUPYTER_USER"
chown -R "$LAB_JUPYTER_USER:$JUPYTER_GROUP" "$LAB_REPO_DIR"
for kernel_dir in freebsd-python freebsd-python-bhyve; do
    if [ -d "$JUPYTER_HOME/.local/share/jupyter/kernels/$kernel_dir" ]; then
        chown -R "$LAB_JUPYTER_USER:$JUPYTER_GROUP" \
            "$JUPYTER_HOME/.local/share/jupyter/kernels/$kernel_dir"
    fi
done

log "Bootstrap complete"
printf 'Repository:       %s\n' "$LAB_REPO_DIR"
printf 'Jupyter user:     %s\n' "$LAB_JUPYTER_USER"
printf 'Jupyter venv:     %s\n' "$JUPYTER_VENV"
printf 'Runtime daemon:   %s\n' "$LAB_DAEMON_VENV/bin/freebsd-lab-runtime-daemon"
printf 'Runtime socket:   %s\n' /var/run/freebsd-laboratory/runtime.sock
if [ -n "$ACTIVE_SNAPSHOT" ]; then
    printf 'Jail snapshot:    %s\n' "$ACTIVE_SNAPSHOT"
fi
printf '\nA new login is required before a non-root Jupyter user inherits the %s group.\n' "$LAB_GROUP"
printf 'Start JupyterLab as %s with:\n' "$LAB_JUPYTER_USER"
printf '  cd %s && %s/bin/jupyter lab --ServerApp.root_dir=%s --ip=0.0.0.0 --no-browser\n' \
    "$LAB_REPO_DIR" "$JUPYTER_VENV" "$LAB_REPO_DIR"
