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

install_python_entrypoint()
{
    entrypoint_path=$1
    entrypoint_module=$2
    entrypoint_callable=$3

    {
        printf '#!%s\n' "$JUPYTER_VENV/bin/python"
        printf 'from %s import %s\n' "$entrypoint_module" "$entrypoint_callable"
        printf '%s()\n' "$entrypoint_callable"
    } > "$entrypoint_path"
    chmod 0755 "$entrypoint_path"
}

install_sentry_sdk()
{
    _sentry_venv=$1
    "$_sentry_venv/bin/python" -m pip install \
        --no-deps --ignore-installed "sentry-sdk==$LAB_SENTRY_SDK_VERSION"
    "$_sentry_venv/bin/python" - "$_sentry_venv" "$LAB_SENTRY_SDK_VERSION" <<'PY'
from importlib.metadata import version
from pathlib import Path
import sentry_sdk
import sys

venv = Path(sys.argv[1]).resolve()
sdk_path = Path(sentry_sdk.__file__).resolve()
if venv not in sdk_path.parents:
    raise SystemExit(f"sentry-sdk resolved outside venv: {sdk_path}")
actual = version("sentry-sdk")
if actual != sys.argv[2]:
    raise SystemExit(f"unexpected sentry-sdk version: {actual}")
PY
}

wait_for_runtime_daemon()
{
    attempt=0
    last_error="runtime daemon did not answer"
    while [ "$attempt" -lt "$LAB_DAEMON_READY_TIMEOUT" ]; do
        if output=$("$JUPYTER_VENV/bin/python" -c \
            'from freebsd_laboratory.runtime_client import RuntimeClient; print(RuntimeClient(timeout=1.0).ping())' \
            2>&1); then
            printf '%s\n' "$output"
            return 0
        fi
        last_error=$output
        attempt=$((attempt + 1))
        sleep 1
    done

    service freebsd_lab_daemon status >&2 || true
    if [ -f /var/log/messages ]; then
        tail -n 100 /var/log/messages \
            | grep -E 'freebsd_lab_daemon|freebsd-lab-runtime-daemon|Traceback|RuntimeError' \
            >&2 || true
    fi
    fail "Runtime daemon was not ready after ${LAB_DAEMON_READY_TIMEOUT}s: $last_error"
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
LAB_DAEMON_READY_TIMEOUT=${LAB_DAEMON_READY_TIMEOUT:-30}
LAB_SENTRY_SDK_VERSION=${LAB_SENTRY_SDK_VERSION:-2.66.1}
LAB_INSTALL_BHYVE_BACKEND=${LAB_INSTALL_BHYVE_BACKEND:-NO}
LAB_VM_DATASET=${LAB_VM_DATASET:-}
LAB_JAIL_IMAGE_MODE=${LAB_JAIL_IMAGE_MODE:-release}
LAB_SRC_DIR=${LAB_SRC_DIR:-/usr/src}
LAB_SRC_BRANCH=${LAB_SRC_BRANCH:-}
LAB_RELEASE=${LAB_RELEASE:-}
LAB_RELEASE_BASE_URL=${LAB_RELEASE_BASE_URL:-https://download.freebsd.org/releases}
LAB_RELEASE_TARGET=${LAB_RELEASE_TARGET:-}
LAB_RELEASE_TARGET_ARCH=${LAB_RELEASE_TARGET_ARCH:-}
LAB_RELEASE_CACHE_DIR=${LAB_RELEASE_CACHE_DIR:-/var/cache/freebsd-laboratory/releases}
LAB_BUILD_JAIL_IMAGE=${LAB_BUILD_JAIL_IMAGE:-YES}
LAB_REBUILD_JAIL_IMAGE=${LAB_REBUILD_JAIL_IMAGE:-NO}
LAB_FAIL_ON_PKG_AUDIT=${LAB_FAIL_ON_PKG_AUDIT:-YES}
LAB_PKG_AUDIT_ALLOWED_VULN_IDS=${LAB_PKG_AUDIT_ALLOWED_VULN_IDS-}
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

case "$LAB_JAIL_IMAGE_MODE" in
    release|source) ;;
    *) fail "LAB_JAIL_IMAGE_MODE must be release or source" ;;
esac
case "$LAB_DAEMON_READY_TIMEOUT" in
    ''|*[!0-9]*|0) fail "LAB_DAEMON_READY_TIMEOUT must be a positive integer" ;;
esac
case "$LAB_SENTRY_SDK_VERSION" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) fail "LAB_SENTRY_SDK_VERSION must be an explicit X.Y.Z version" ;;
esac

if [ -z "$LAB_JUPYTER_USER" ]; then
    if pw usershow freebsd >/dev/null 2>&1; then
        LAB_JUPYTER_USER=freebsd
    else
        fail "Set LAB_JUPYTER_USER to an existing non-root account; freebsd does not exist"
    fi
fi
if ! pw usershow "$LAB_JUPYTER_USER" >/dev/null 2>&1; then
    fail "Jupyter user does not exist: $LAB_JUPYTER_USER"
fi
JUPYTER_UID=$(pw usershow -n "$LAB_JUPYTER_USER" -7 | awk -F: '{print $3}')
case "$JUPYTER_UID" in
    ''|*[!0-9]*) fail "Unable to determine uid for $LAB_JUPYTER_USER" ;;
esac
if [ "$JUPYTER_UID" -eq 0 ]; then
    fail "Jupyter must run as a non-root account: $LAB_JUPYTER_USER"
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
if is_yes "$LAB_INSTALL_BHYVE_BACKEND"; then
    pkg install -y vm-bhyve qemu-tools
fi

PYTHON=/usr/local/bin/python3
[ -x "$PYTHON" ] || fail "python3 was not installed at $PYTHON"
PY_TAG=$($PYTHON -c 'import sys; print(f"py{sys.version_info.major}{sys.version_info.minor}")')

log "Installing FreeBSD binary Python/Jupyter dependencies for $PY_TAG"
pkg install -y \
    "${PY_TAG}-pip" \
    "${PY_TAG}-setuptools" \
    "${PY_TAG}-wheel" \
    "${PY_TAG}-jupyterlab" \
    "${PY_TAG}-ipykernel" \
    "${PY_TAG}-cryptography" \
    "${PY_TAG}-pytest" \
    "${PY_TAG}-pyyaml" \
    "${PY_TAG}-llama-cpp-python" \
    "${PY_TAG}-certifi" \
    "${PY_TAG}-urllib3"

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

log "Creating operator group and host state"
pw groupshow "$LAB_GROUP" >/dev/null 2>&1 || pw groupadd "$LAB_GROUP"
pw groupmod "$LAB_GROUP" -m "$LAB_JUPYTER_USER"
install -d -o root -g "$LAB_GROUP" -m 0750 /usr/local/etc/freebsd-laboratory

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

if is_yes "$LAB_INSTALL_BHYVE_BACKEND"; then
    if [ -z "$LAB_VM_DATASET" ]; then
        LAB_VM_DATASET="${LAB_ZFS_POOL}/vm"
    fi

    log "Configuring vm-bhyve datastore at $LAB_VM_DATASET"

    if ! zfs list -H -o name "$LAB_VM_DATASET" >/dev/null 2>&1; then
        zfs create -p "$LAB_VM_DATASET"
    fi

    sysrc vm_enable=YES
    sysrc "vm_dir=zfs:${LAB_VM_DATASET}"
    vm init

    VM_DIR_CONFIG=$(sysrc -n vm_dir 2>/dev/null || true)
    [ "$VM_DIR_CONFIG" = "zfs:${LAB_VM_DATASET}" ] || \
        fail "vm-bhyve vm_dir is ${VM_DIR_CONFIG:-unset}; expected zfs:${LAB_VM_DATASET}"
fi

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
    --no-deps "$LAB_REPO_DIR"
install_sentry_sdk "$LAB_DAEMON_VENV"
"$LAB_DAEMON_VENV/bin/python" -c 'import freebsd_laboratory, sentry_sdk'
chown -R root:wheel "$LAB_DAEMON_VENV"
chmod -R go-w "$LAB_DAEMON_VENV"

JUPYTER_VENV="${LAB_REPO_DIR%/}/.venv"
rm -rf "$JUPYTER_VENV"
$PYTHON -m venv --system-site-packages "$JUPYTER_VENV"
"$JUPYTER_VENV/bin/python" -m pip install \
    --no-deps -e "$LAB_REPO_DIR"
"$JUPYTER_VENV/bin/python" -m pip install \
    --no-deps --ignore-installed "jupyter_builder>=1.2,<2"
install_sentry_sdk "$JUPYTER_VENV"

"$JUPYTER_VENV/bin/python" -c \
    'import freebsd_laboratory, jupyter_builder, jupyter_core, jupyter_server, jupyterlab, sentry_sdk'
[ -x "$JUPYTER_VENV/bin/jupyter-builder" ] || fail "jupyter-builder entrypoint is missing"
install_python_entrypoint "$JUPYTER_VENV/bin/jupyter" jupyter_core.command main
install_python_entrypoint "$JUPYTER_VENV/bin/jupyter-server" jupyter_server.serverapp main
install_python_entrypoint "$JUPYTER_VENV/bin/jupyter-lab" jupyterlab.labapp main
install_python_entrypoint "$JUPYTER_VENV/bin/jupyter-labextension" jupyterlab.labextensions main
ln -sf "$JUPYTER_VENV/bin/freebsd-lab-agent" /usr/local/bin/freebsd-lab-agent

log "Building and registering the JupyterLab extension"
(
    cd "$LAB_REPO_DIR/labextension"
    npm install --no-audit --no-fund
    PATH="$JUPYTER_VENV/bin:$PATH" npm run build
)

LABEXT_OUTPUT="$LAB_REPO_DIR/labextension/labextension"
[ -f "$LABEXT_OUTPUT/package.json" ] || fail "Prebuilt JupyterLab extension package.json is missing"
[ -d "$LABEXT_OUTPUT/static" ] || fail "Prebuilt JupyterLab extension static bundle is missing"
LABEXT_NAME=$("$JUPYTER_VENV/bin/python" -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["name"])' \
    "$LABEXT_OUTPUT/package.json")
case "$LABEXT_NAME" in
    @*/*|[A-Za-z0-9_-]*) ;;
    *) fail "Unsupported JupyterLab extension package name: $LABEXT_NAME" ;;
esac
LABEXT_DEST="$JUPYTER_VENV/share/jupyter/labextensions/$LABEXT_NAME"
install -d -m 0755 "$(dirname "$LABEXT_DEST")"
rm -rf "$LABEXT_DEST"
ln -s "$LABEXT_OUTPUT" "$LABEXT_DEST"

SERVER_EXTENSION_CONFIG_DIR="$JUPYTER_VENV/etc/jupyter/jupyter_server_config.d"
install -d -m 0755 "$SERVER_EXTENSION_CONFIG_DIR"
cat > "$SERVER_EXTENSION_CONFIG_DIR/freebsd_laboratory.json" <<'EOF'
{
  "ServerApp": {
    "jpserver_extensions": {
      "freebsd_laboratory": true
    }
  }
}
EOF
"$JUPYTER_VENV/bin/python" - <<'PY'
from jupyter_server.extension.manager import ExtensionPackage

extension = ExtensionPackage(name="freebsd_laboratory", enabled=True)
if not extension.validate():
    raise SystemExit("freebsd_laboratory server extension validation failed")
PY

for path in \
    "$JUPYTER_HOME/.local" \
    "$JUPYTER_HOME/.local/share" \
    "$JUPYTER_HOME/.local/share/jupyter" \
    "$JUPYTER_HOME/.local/share/jupyter/kernels"
do
    install -d -o "$LAB_JUPYTER_USER" -g "$JUPYTER_GROUP" -m 0755 "$path"
done
HOME="$JUPYTER_HOME" "$JUPYTER_VENV/bin/freebsd-lab-install-kernel"

if is_yes "$LAB_BUILD_JAIL_IMAGE"; then
    SNAPSHOT_PREFIX="${JAIL_TEMPLATE_PARENT}/freebsd-python-${LAB_JAIL_IMAGE_MODE}"
    EXISTING_SNAPSHOT=$(zfs list -H -t snapshot -o name -s creation -r "$JAIL_TEMPLATE_PARENT" 2>/dev/null \
        | awk -v prefix="${SNAPSHOT_PREFIX}-" 'index($0, prefix) == 1 && $0 ~ /@clean$/ {print}' \
        | tail -1 || true)

    if [ -n "$EXISTING_SNAPSHOT" ] && ! is_yes "$LAB_REBUILD_JAIL_IMAGE"; then
        ACTIVE_SNAPSHOT=$EXISTING_SNAPSHOT
        printf 'Using existing %s jail template snapshot: %s\n' "$LAB_JAIL_IMAGE_MODE" "$ACTIVE_SNAPSHOT"
    else
        case "$LAB_JAIL_IMAGE_MODE" in
            release)
                if [ -z "$LAB_RELEASE" ]; then
                    LAB_RELEASE=$(freebsd-version -u 2>/dev/null || uname -r)
                    LAB_RELEASE=$(printf '%s\n' "$LAB_RELEASE" | sed -E 's/-p[0-9]+$//')
                fi
                case "$LAB_RELEASE" in
                    *-RELEASE) ;;
                    *) fail "Release image mode requires LAB_RELEASE=<X.Y-RELEASE>; got $LAB_RELEASE" ;;
                esac

                log "Building VNET jail image from official FreeBSD $LAB_RELEASE base.txz"
                env \
                    LAB_JAIL_IMAGE_MODE=release \
                    LAB_RELEASE="$LAB_RELEASE" \
                    LAB_RELEASE_BASE_URL="$LAB_RELEASE_BASE_URL" \
                    LAB_RELEASE_TARGET="$LAB_RELEASE_TARGET" \
                    LAB_RELEASE_TARGET_ARCH="$LAB_RELEASE_TARGET_ARCH" \
                    LAB_RELEASE_CACHE_DIR="$LAB_RELEASE_CACHE_DIR" \
                    LAB_FAIL_ON_PKG_AUDIT="$LAB_FAIL_ON_PKG_AUDIT" \
                    LAB_PKG_AUDIT_ALLOWED_VULN_IDS="$LAB_PKG_AUDIT_ALLOWED_VULN_IDS" \
                    JAIL_DATASET_PREFIX="$SNAPSHOT_PREFIX" \
                    JAIL_MOUNT_ROOT=/usr/local/jails/templates \
                    LAB_JAIL_PACKAGES="python3 ${PY_TAG}-ipykernel" \
                    "$LAB_REPO_DIR/deploy/freebsd/images/build-jail-template.sh"
                ;;
            source)
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
                    *) fail "LAB_SRC_BRANCH must be an explicit releng/* branch for source images" ;;
                esac

                log "Preparing FreeBSD source tree $LAB_SRC_BRANCH in $LAB_SRC_DIR"
                if [ -d "$LAB_SRC_DIR/.git" ]; then
                    git -C "$LAB_SRC_DIR" fetch --depth 1 origin "$LAB_SRC_BRANCH"
                    git -C "$LAB_SRC_DIR" checkout -B "$LAB_SRC_BRANCH" FETCH_HEAD
                elif [ -e "$LAB_SRC_DIR" ]; then
                    if [ -d "$LAB_SRC_DIR" ] && [ -z "$(ls -A "$LAB_SRC_DIR" 2>/dev/null)" ]; then
                        git clone --depth 1 --branch "$LAB_SRC_BRANCH" \
                            https://git.FreeBSD.org/src.git "$LAB_SRC_DIR"
                    else
                        fail "$LAB_SRC_DIR exists but is not an empty directory or Git source checkout"
                    fi
                else
                    git clone --depth 1 --branch "$LAB_SRC_BRANCH" \
                        https://git.FreeBSD.org/src.git "$LAB_SRC_DIR"
                fi
                [ -f "$LAB_SRC_DIR/Makefile" ] || fail "FreeBSD source Makefile is missing"

                JOBS=$(sysctl -n hw.ncpu)
                log "Building FreeBSD world with $JOBS jobs for source image mode"
                make -C "$LAB_SRC_DIR" -j"$JOBS" buildworld

                log "Building versioned VNET jail image from source"
                env \
                    LAB_JAIL_IMAGE_MODE=source \
                    SRC_DIR="$LAB_SRC_DIR" \
                    LAB_FAIL_ON_PKG_AUDIT="$LAB_FAIL_ON_PKG_AUDIT" \
                    LAB_PKG_AUDIT_ALLOWED_VULN_IDS="$LAB_PKG_AUDIT_ALLOWED_VULN_IDS" \
                    JAIL_DATASET_PREFIX="$SNAPSHOT_PREFIX" \
                    JAIL_MOUNT_ROOT=/usr/local/jails/templates \
                    LAB_JAIL_PACKAGES="python3 ${PY_TAG}-ipykernel" \
                    "$LAB_REPO_DIR/deploy/freebsd/images/build-jail-template.sh"
                ;;
        esac

        ACTIVE_SNAPSHOT=$(zfs list -H -t snapshot -o name -s creation -r "$JAIL_TEMPLATE_PARENT" 2>/dev/null \
            | awk -v prefix="${SNAPSHOT_PREFIX}-" 'index($0, prefix) == 1 && $0 ~ /@clean$/ {print}' \
            | tail -1 || true)
        [ -n "$ACTIVE_SNAPSHOT" ] || fail "Golden-image builder produced no $LAB_JAIL_IMAGE_MODE @clean snapshot"
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

RUNTIME_ARGS="--jail-dataset-parent=$JAIL_DATASET_PARENT --jail-mount-root=$LAB_JAIL_MOUNT_ROOT"
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

if is_yes "$LAB_INSTALL_BHYVE_BACKEND"; then
    log "Configuring vm-bhyve lab switch"

    if ! vm switch info freebsdlab >/dev/null 2>&1; then
        vm switch create \
            -t manual \
            -b "$LAB_BRIDGE_NAME" \
            freebsdlab
    fi

    vm switch private freebsdlab on
    vm switch info freebsdlab >/dev/null || \
        fail "vm-bhyve switch freebsdlab is unavailable"
fi

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
wait_for_runtime_daemon

if is_yes "$LAB_SMOKE_TEST" && [ -n "$ACTIVE_SNAPSHOT" ]; then
    log "Running a real VNET jail smoke test"
    SMOKE_SUFFIX="bs$$"
    SMOKE_NAME="freebsd-lab-$SMOKE_SUFFIX"
    SMOKE_KEY_DIR=$(mktemp -d /tmp/freebsd-lab-smoke-key.XXXXXX)
    ssh-keygen -q -t ed25519 -N '' -f "$SMOKE_KEY_DIR/id_ed25519"
    chmod 0600 "$SMOKE_KEY_DIR/id_ed25519" "$SMOKE_KEY_DIR/id_ed25519.pub"
    cleanup_smoke()
    {
        "$JUPYTER_VENV/bin/python" - "$SMOKE_NAME" <<'PY' >/dev/null 2>&1 || true
import sys
from freebsd_laboratory.runtime_client import RuntimeClient
RuntimeClient().destroy(sys.argv[1])
PY
        rm -rf "$SMOKE_KEY_DIR"
    }
    trap cleanup_smoke EXIT HUP INT TERM
    "$JUPYTER_VENV/bin/python" - "$SMOKE_NAME" "$SMOKE_KEY_DIR/id_ed25519.pub" <<'PY'
import os
from pathlib import Path
import sys
from freebsd_laboratory.runtime_client import RuntimeClient
public_key = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
print(RuntimeClient().create_jail(sys.argv[1], os.getpid(), public_key))
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
printf 'Jail image mode:  %s\n' "$LAB_JAIL_IMAGE_MODE"
if [ -n "$ACTIVE_SNAPSHOT" ]; then
    printf 'Jail snapshot:    %s\n' "$ACTIVE_SNAPSHOT"
fi
printf '\nA new login is required before a non-root Jupyter user inherits the %s group.\n' "$LAB_GROUP"
printf 'Start JupyterLab as %s with:\n' "$LAB_JUPYTER_USER"
printf '  cd %s && %s/bin/jupyter lab --ServerApp.root_dir=%s --ip=0.0.0.0 --no-browser\n' \
    "$LAB_REPO_DIR" "$JUPYTER_VENV" "$LAB_REPO_DIR"
