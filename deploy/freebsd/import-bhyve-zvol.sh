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
ZVOL_NAME="${ZVOL_PARENT}/freebsd-python"
ZVOL_SIZE=${3:-8G}
SNAPSHOT_NAME="${ZVOL_NAME}@ready"

[ -f "$RAW_IMAGE" ] || fail "Golden raw image not found: $RAW_IMAGE"

if ! zfs list -H -o name "$ZVOL_PARENT" >/dev/null 2>&1; then
    printf 'Creating parent dataset: %s\n' "$ZVOL_PARENT"
    zfs create -p "$ZVOL_PARENT"
fi

if zfs list -H -o name "$ZVOL_NAME" >/dev/null 2>&1; then
    printf 'Destroying existing zvol: %s\n' "$ZVOL_NAME"
    zfs destroy -r -f "$ZVOL_NAME"
fi

printf 'Creating zvol %s (%s)...\n' "$ZVOL_NAME" "$ZVOL_SIZE"
zfs create -V "$ZVOL_SIZE" -s "$ZVOL_NAME"

printf 'Populating zvol from %s...\n' "$RAW_IMAGE"
dd if="$RAW_IMAGE" of="/dev/zvol/${ZVOL_NAME}" bs=1M status=none

printf 'Creating ready snapshot: %s\n' "$SNAPSHOT_NAME"
zfs snapshot "$SNAPSHOT_NAME"

printf 'Successfully imported golden image into %s\n' "$SNAPSHOT_NAME"
