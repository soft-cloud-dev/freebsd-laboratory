#!/bin/sh
set -eu

fail()
{
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

REPO_DIR=${LAB_REPO_DIR:-$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)}
TEMPLATE_SOURCE="$REPO_DIR/freebsd_laboratory/vm-bhyve/freebsd-lab.conf"

[ "$(id -u)" -eq 0 ] || fail "must run as root"
[ -f "$TEMPLATE_SOURCE" ] || fail "vm-bhyve template is missing: $TEMPLATE_SOURCE"

VM_DIR_CONFIG=$(sysrc -n vm_dir 2>/dev/null || true)
case "$VM_DIR_CONFIG" in
    zfs:*)
        VM_DATASET=${VM_DIR_CONFIG#zfs:}
        VM_ROOT=$(zfs get -H -o value mountpoint "$VM_DATASET" 2>/dev/null || true)
        ;;
    /*)
        VM_ROOT=$VM_DIR_CONFIG
        ;;
    *)
        fail "vm-bhyve vm_dir is unset or unsupported: ${VM_DIR_CONFIG:-unset}"
        ;;
esac

case "$VM_ROOT" in
    ""|none|legacy|-)
        fail "unable to resolve vm-bhyve datastore mountpoint from $VM_DIR_CONFIG"
        ;;
    /*) ;;
    *) fail "vm-bhyve datastore mountpoint is not absolute: $VM_ROOT" ;;
esac

install -d -o root -g wheel -m 0755 "$VM_ROOT/.templates"
install -o root -g wheel -m 0644 "$TEMPLATE_SOURCE" "$VM_ROOT/.templates/freebsd-lab.conf"

[ -r "$VM_ROOT/.templates/freebsd-lab.conf" ] || fail "installed template is not readable"
printf 'Installed vm-bhyve template: %s\n' "$VM_ROOT/.templates/freebsd-lab.conf"
