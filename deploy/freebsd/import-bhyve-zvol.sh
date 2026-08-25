#!/bin/sh
set -eu

fail()
{
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail "must run as root"

RAW_IMAGE=${1:-/var/db/freebsd-laboratory/images/freebsd-python.raw}
ZVOL_PARENT=${2:-zroot/vm/.zvol}
IMAGE_BASE=$(basename "$RAW_IMAGE" .raw)
ZVOL_NAME="${ZVOL_PARENT}/${IMAGE_BASE}"

[ -f "$RAW_IMAGE" ] || fail "Golden raw image not found: $RAW_IMAGE"

IMAGE_BYTES=$(stat -L -f %z "$RAW_IMAGE" 2>/dev/null || stat -L -c %s "$RAW_IMAGE" 2>/dev/null || wc -c < "$RAW_IMAGE")
ZVOL_SIZE=${3:-${IMAGE_BYTES}}
SNAPSHOT_NAME="${ZVOL_NAME}@ready"


if ! zfs list -H -o name "$ZVOL_PARENT" >/dev/null 2>&1; then
    printf 'Creating parent dataset: %s\n' "$ZVOL_PARENT"
    zfs create -p "$ZVOL_PARENT"
fi

if zfs list "$ZVOL_NAME" >/dev/null 2>&1; then
    printf 'Destroying existing zvol: %s\n' "$ZVOL_NAME"
    zfs destroy -R -f "$ZVOL_NAME"
fi

printf 'Creating zvol %s (%s)...\n' "$ZVOL_NAME" "$ZVOL_SIZE"
zfs create -V "$ZVOL_SIZE" -s "$ZVOL_NAME"

# Wait for devfs device node to appear
for _ in $(seq 1 50); do
    if [ -e "/dev/zvol/${ZVOL_NAME}" ] || [ -c "/dev/zvol/${ZVOL_NAME}" ]; then
        break
    fi
    sleep 0.1
done
sleep 0.5

printf 'Populating zvol from %s...\n' "$RAW_IMAGE"
sysctl kern.geom.debugflags=16 >/dev/null 2>&1 || true
dd if="$RAW_IMAGE" of="/dev/zvol/${ZVOL_NAME}" bs=1M status=none
sysctl kern.geom.debugflags=0 >/dev/null 2>&1 || true

printf 'Creating ready snapshot: %s\n' "$SNAPSHOT_NAME"
zfs snapshot "$SNAPSHOT_NAME"

printf 'Successfully imported golden image into %s\n' "$SNAPSHOT_NAME"
