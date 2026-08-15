#!/bin/sh
set -eu

PF_CONF=${PF_CONF:-/etc/pf.conf}
ANCHOR_NAME=${ANCHOR_NAME:-freebsd-lab}
ANCHOR_FILE=${ANCHOR_FILE:-/usr/local/etc/pf.anchors/freebsd-lab}
MODE=${1:-check}

if [ "$(uname -s)" != "FreeBSD" ]; then
    echo "validate-pf.sh requires FreeBSD" >&2
    exit 1
fi
if [ ! -r "$PF_CONF" ]; then
    echo "PF configuration is not readable: $PF_CONF" >&2
    exit 1
fi
if [ ! -r "$ANCHOR_FILE" ]; then
    echo "PF anchor file is not readable: $ANCHOR_FILE" >&2
    exit 1
fi
if ! grep -Eq '^[[:space:]]*anchor[[:space:]]+"freebsd-lab"([[:space:]]|$)' "$PF_CONF"; then
    echo "Missing anchor \"freebsd-lab\" reference in $PF_CONF" >&2
    exit 1
fi
if ! grep -Eq '^[[:space:]]*load[[:space:]]+anchor[[:space:]]+"freebsd-lab"[[:space:]]+from[[:space:]]+"/usr/local/etc/pf.anchors/freebsd-lab"' "$PF_CONF"; then
    echo "Missing freebsd-lab load anchor line in $PF_CONF" >&2
    exit 1
fi

pfctl -nf "$PF_CONF"

case "$MODE" in
    check)
        echo "PF syntax and freebsd-lab anchor references are valid."
        ;;
    --reload|reload)
        pfctl -f "$PF_CONF"
        if ! pfctl -a "$ANCHOR_NAME" -sr | grep -q .; then
            echo "The $ANCHOR_NAME anchor has no active filter rules after reload" >&2
            exit 1
        fi
        echo "PF reloaded and $ANCHOR_NAME anchor is active."
        ;;
    *)
        echo "Usage: $0 [check|--reload]" >&2
        exit 2
        ;;
esac
