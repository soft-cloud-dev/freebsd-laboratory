#!/bin/sh
set -eu

if [ "$(uname -s)" != "FreeBSD" ]; then
    echo "build-jail-template.sh requires FreeBSD" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "build-jail-template.sh must run as root" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC_DIR=${SRC_DIR:-/usr/src}
BUILD_ID=${BUILD_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
JAIL_DATASET_PREFIX=${JAIL_DATASET_PREFIX:-zroot/jails/templates/freebsd-python}
JAIL_MOUNT_ROOT=${JAIL_MOUNT_ROOT:-/usr/local/jails/templates}
LAB_JAIL_PACKAGES=${LAB_JAIL_PACKAGES:-"python311 py311-ipykernel"}
LAB_PKG_REPOS_DIR=${LAB_PKG_REPOS_DIR:-}
LAB_FAIL_ON_PKG_AUDIT=${LAB_FAIL_ON_PKG_AUDIT:-YES}

DATASET="${JAIL_DATASET_PREFIX}-${BUILD_ID}"
DATASET_PARENT=${DATASET%/*}
ROOT="${JAIL_MOUNT_ROOT%/}/freebsd-python-${BUILD_ID}"
SNAPSHOT="${DATASET}@clean"
SSHD_POLICY="${SCRIPT_DIR}/sshd-freebsd-lab.conf"

for command in git make pkg pw zfs chroot install; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is unavailable: $command" >&2
        exit 1
    fi
done

if [ ! -f "${SRC_DIR}/Makefile" ]; then
    echo "FreeBSD source tree not found at ${SRC_DIR}" >&2
    exit 1
fi
if [ ! -f "$SSHD_POLICY" ]; then
    echo "SSH policy not found: $SSHD_POLICY" >&2
    exit 1
fi
if ! zfs list -H -o name "$DATASET_PARENT" >/dev/null 2>&1; then
    echo "ZFS template parent does not exist: $DATASET_PARENT" >&2
    exit 1
fi
if zfs list -H -o name "$DATASET" >/dev/null 2>&1; then
    echo "Refusing to replace existing template dataset: $DATASET" >&2
    exit 1
fi

SOURCE_REVISION=$(git -C "$SRC_DIR" rev-parse --verify HEAD)
SOURCE_BRANCH=$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD)
BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)

cleanup_failed_build()
{
    status=$?
    if [ "$status" -ne 0 ] && zfs list -H -o name "$DATASET" >/dev/null 2>&1; then
        zfs destroy -r "$DATASET" >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup_failed_build EXIT HUP INT TERM

mkdir -p "$JAIL_MOUNT_ROOT"
zfs create -o "mountpoint=${ROOT}" "$DATASET"

make -C "$SRC_DIR" installworld DESTDIR="$ROOT"
make -C "$SRC_DIR" distribution DESTDIR="$ROOT"

if [ -n "$LAB_PKG_REPOS_DIR" ]; then
    env ASSUME_ALWAYS_YES=yes pkg -r "$ROOT" -R "$LAB_PKG_REPOS_DIR" install -y $LAB_JAIL_PACKAGES
else
    env ASSUME_ALWAYS_YES=yes pkg -r "$ROOT" install -y $LAB_JAIL_PACKAGES
fi

if ! pw -R "$ROOT" usershow freebsd >/dev/null 2>&1; then
    pw -R "$ROOT" useradd freebsd -m -s /bin/sh -w no
fi

install -d -m 0755 "$ROOT/etc/ssh/sshd_config.d"
install -m 0644 "$SSHD_POLICY" "$ROOT/etc/ssh/sshd_config.d/99-freebsd-laboratory.conf"
if ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config.d/\*\.conf' "$ROOT/etc/ssh/sshd_config"; then
    printf '\nInclude /etc/ssh/sshd_config.d/*.conf\n' >> "$ROOT/etc/ssh/sshd_config"
fi
rm -f "$ROOT"/etc/ssh/ssh_host_*_key "$ROOT"/etc/ssh/ssh_host_*_key.pub

touch "$ROOT/etc/rc.conf"
if ! grep -Eq '^sshd_enable=' "$ROOT/etc/rc.conf"; then
    printf 'sshd_enable="YES"\n' >> "$ROOT/etc/rc.conf"
fi

chroot "$ROOT" /usr/local/bin/python3 -c 'import ipykernel; print(ipykernel.__version__)'

if [ "$LAB_FAIL_ON_PKG_AUDIT" = "YES" ]; then
    if [ -n "$LAB_PKG_REPOS_DIR" ]; then
        pkg -r "$ROOT" -R "$LAB_PKG_REPOS_DIR" audit -F
    else
        pkg -r "$ROOT" audit -F
    fi
fi

USERLAND_VERSION=$(chroot "$ROOT" /bin/freebsd-version -u)
PACKAGE_LIST=$(pkg -r "$ROOT" query -a '%n-%v' | sort | tr '\n' ' ')

install -d -m 0755 "$ROOT/usr/local/share/freebsd-laboratory"
cat > "$ROOT/usr/local/share/freebsd-laboratory/image-manifest.json" <<EOF
{
  "schema": "softcloud.freebsd-golden-image/v1",
  "type": "jail-template",
  "built_at": "${BUILT_AT}",
  "source_branch": "${SOURCE_BRANCH}",
  "source_revision": "${SOURCE_REVISION}",
  "userland": "${USERLAND_VERSION}",
  "packages": "${PACKAGE_LIST}",
  "snapshot": "${SNAPSHOT}"
}
EOF

sync
zfs snapshot "$SNAPSHOT"
zfs set mountpoint=none "$DATASET"

trap - EXIT HUP INT TERM

printf '%s\n' "Built jail golden image:"
printf '  snapshot: %s\n' "$SNAPSHOT"
printf '  source:   %s (%s)\n' "$SOURCE_BRANCH" "$SOURCE_REVISION"
printf '  runtime:  --jail-template=%s\n' "$SNAPSHOT"
