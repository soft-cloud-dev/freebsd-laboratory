#!/bin/sh
set -eu

if [ "$(uname -s)" != "FreeBSD" ]; then
    echo "build-golden-images.sh requires FreeBSD" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "build-golden-images.sh must run as root" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRC_DIR=${SRC_DIR:-/usr/src}
BUILD_ID=${BUILD_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
JOBS=${JOBS:-$(sysctl -n hw.ncpu)}
OBJ_ROOT=${OBJ_ROOT:-/var/tmp/freebsd-laboratory-vm-${BUILD_ID}}
SKIP_SOURCE_BUILD=${SKIP_SOURCE_BUILD:-NO}
REQUIRE_RELENG_SOURCE=${REQUIRE_RELENG_SOURCE:-YES}
ALLOW_DIRTY_SOURCE=${ALLOW_DIRTY_SOURCE:-NO}

if [ ! -d "${SRC_DIR}/.git" ]; then
    echo "FreeBSD source tree must be a Git checkout: ${SRC_DIR}" >&2
    exit 1
fi

SOURCE_BRANCH=$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD)
SOURCE_REVISION=$(git -C "$SRC_DIR" rev-parse --verify HEAD)

if [ "$REQUIRE_RELENG_SOURCE" = "YES" ]; then
    case "$SOURCE_BRANCH" in
        releng/*) ;;
        *)
            echo "Refusing non-releng source branch: ${SOURCE_BRANCH}" >&2
            echo "Use a FreeBSD releng/* branch for security-patched golden images." >&2
            exit 1
            ;;
    esac
fi

if [ "$ALLOW_DIRTY_SOURCE" != "YES" ]; then
    if ! git -C "$SRC_DIR" diff --quiet || ! git -C "$SRC_DIR" diff --cached --quiet; then
        echo "Refusing dirty FreeBSD source tree: ${SRC_DIR}" >&2
        exit 1
    fi
fi

printf 'Golden image build id: %s\n' "$BUILD_ID"
printf 'FreeBSD source: %s (%s)\n' "$SOURCE_BRANCH" "$SOURCE_REVISION"
printf 'FreeBSD object root: %s\n' "$OBJ_ROOT"

if [ "$SKIP_SOURCE_BUILD" != "YES" ]; then
    env MAKEOBJDIRPREFIX="$OBJ_ROOT" \
        make -C "$SRC_DIR" -j "$JOBS" buildworld buildkernel
fi

BUILD_ID="$BUILD_ID" SRC_DIR="$SRC_DIR" OBJ_ROOT="$OBJ_ROOT" \
    MAKEOBJDIRPREFIX="$OBJ_ROOT" LAB_JAIL_IMAGE_MODE=source \
    "${SCRIPT_DIR}/build-jail-template.sh"

BUILD_ID="$BUILD_ID" SRC_DIR="$SRC_DIR" OBJ_ROOT="$OBJ_ROOT" \
    MAKEOBJDIRPREFIX="$OBJ_ROOT" \
    "${SCRIPT_DIR}/build-bhyve-image.sh"

printf '%s\n' "Golden image rebuild completed."
printf '%s\n' "Review the generated manifests and activate the new jail snapshot/VM artifact deliberately."
