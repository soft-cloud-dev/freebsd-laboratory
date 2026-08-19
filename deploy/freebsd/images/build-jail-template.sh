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
LAB_JAIL_IMAGE_MODE=${LAB_JAIL_IMAGE_MODE:-release}
BUILD_ID=${BUILD_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
JAIL_DATASET_PREFIX=${JAIL_DATASET_PREFIX:-zroot/jails/templates/freebsd-python-${LAB_JAIL_IMAGE_MODE}}
JAIL_MOUNT_ROOT=${JAIL_MOUNT_ROOT:-/usr/local/jails/templates}
LAB_JAIL_PACKAGES=${LAB_JAIL_PACKAGES:-"python311 py311-ipykernel"}
LAB_PKG_REPOS_DIR=${LAB_PKG_REPOS_DIR:-}
LAB_FAIL_ON_PKG_AUDIT=${LAB_FAIL_ON_PKG_AUDIT:-YES}
SRC_DIR=${SRC_DIR:-/usr/src}
LAB_RELEASE=${LAB_RELEASE:-}
LAB_RELEASE_BASE_URL=${LAB_RELEASE_BASE_URL:-https://download.freebsd.org/releases}
LAB_RELEASE_TARGET=${LAB_RELEASE_TARGET:-}
LAB_RELEASE_TARGET_ARCH=${LAB_RELEASE_TARGET_ARCH:-}
LAB_RELEASE_CACHE_DIR=${LAB_RELEASE_CACHE_DIR:-/var/cache/freebsd-laboratory/releases}

case "$LAB_JAIL_IMAGE_MODE" in
    release|source) ;;
    *)
        echo "LAB_JAIL_IMAGE_MODE must be release or source" >&2
        exit 1
        ;;
esac

for command in pkg pw zfs chroot install tar sha256; do
    if ! command -v "$command" >/dev/null 2>&1; then
        echo "Required command is unavailable: $command" >&2
        exit 1
    fi
done

if [ ! -f "$SSHD_POLICY" ]; then
    echo "SSH policy not found: $SSHD_POLICY" >&2
    exit 1
fi

DATASET="${JAIL_DATASET_PREFIX}-${BUILD_ID}"
DATASET_PARENT=${DATASET%/*}
ROOT="${JAIL_MOUNT_ROOT%/}/$(basename "$DATASET")"
SNAPSHOT="${DATASET}@clean"
SSHD_POLICY="${SCRIPT_DIR}/sshd-freebsd-lab.conf"
BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DATASET_CREATED=NO
TMP_DIR=""
SOURCE_BRANCH=""
SOURCE_REVISION=""
RELEASE_URL=""
RELEASE_MANIFEST_SHA256=""
BASE_SHA256=""

if ! zfs list -H -o name "$DATASET_PARENT" >/dev/null 2>&1; then
    echo "ZFS template parent does not exist: $DATASET_PARENT" >&2
    exit 1
fi
if zfs list -H -o name "$DATASET" >/dev/null 2>&1; then
    echo "Refusing to replace existing template dataset: $DATASET" >&2
    exit 1
fi

cleanup_failed_build()
{
    status=$?
    trap - EXIT HUP INT TERM
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
    if [ "$status" -ne 0 ] && [ "$DATASET_CREATED" = "YES" ]; then
        zfs destroy -r "$DATASET" >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup_failed_build EXIT HUP INT TERM

mkdir -p "$JAIL_MOUNT_ROOT"
zfs create -o "mountpoint=${ROOT}" "$DATASET"
DATASET_CREATED=YES

if [ "$LAB_JAIL_IMAGE_MODE" = "release" ]; then
    for command in fetch awk; do
        if ! command -v "$command" >/dev/null 2>&1; then
            echo "Required command is unavailable: $command" >&2
            exit 1
        fi
    done

    if [ -z "$LAB_RELEASE" ]; then
        LAB_RELEASE=$(freebsd-version -u | sed -E 's/-p[0-9]+$//')
    fi
    case "$LAB_RELEASE" in
        *-RELEASE) ;;
        *)
            echo "Release image mode requires an official *-RELEASE value: $LAB_RELEASE" >&2
            exit 1
            ;;
    esac

    if [ -z "$LAB_RELEASE_TARGET" ] || [ -z "$LAB_RELEASE_TARGET_ARCH" ]; then
        HOST_ARCH=$(uname -p)
        case "$HOST_ARCH" in
            amd64)
                LAB_RELEASE_TARGET=amd64
                LAB_RELEASE_TARGET_ARCH=amd64
                ;;
            aarch64|arm64)
                LAB_RELEASE_TARGET=arm64
                LAB_RELEASE_TARGET_ARCH=aarch64
                ;;
            armv7)
                LAB_RELEASE_TARGET=arm
                LAB_RELEASE_TARGET_ARCH=armv7
                ;;
            powerpc64|powerpc64le)
                LAB_RELEASE_TARGET=powerpc
                LAB_RELEASE_TARGET_ARCH=$HOST_ARCH
                ;;
            riscv64)
                LAB_RELEASE_TARGET=riscv
                LAB_RELEASE_TARGET_ARCH=riscv64
                ;;
            *)
                echo "Unsupported FreeBSD release architecture: $HOST_ARCH" >&2
                echo "Set LAB_RELEASE_TARGET and LAB_RELEASE_TARGET_ARCH explicitly." >&2
                exit 1
                ;;
        esac
    fi

    RELEASE_URL="${LAB_RELEASE_BASE_URL%/}/${LAB_RELEASE_TARGET}/${LAB_RELEASE_TARGET_ARCH}/${LAB_RELEASE}"
    CACHE_DIR="${LAB_RELEASE_CACHE_DIR%/}/${LAB_RELEASE_TARGET}-${LAB_RELEASE_TARGET_ARCH}/${LAB_RELEASE}"
    MANIFEST_FILE="$CACHE_DIR/MANIFEST"
    BASE_ARCHIVE="$CACHE_DIR/base.txz"
    install -d -m 0755 "$CACHE_DIR"

    TMP_DIR=$(mktemp -d /tmp/freebsd-lab-release.XXXXXX)
    fetch -q -o "$TMP_DIR/MANIFEST" "$RELEASE_URL/MANIFEST"
    install -m 0644 "$TMP_DIR/MANIFEST" "$MANIFEST_FILE"
    RELEASE_MANIFEST_SHA256=$(sha256 -q "$MANIFEST_FILE")
    EXPECTED_BASE_SHA256=$(awk -F '\t' '$1 == "base.txz" {print $2; exit}' "$MANIFEST_FILE")
    if ! printf '%s\n' "$EXPECTED_BASE_SHA256" | grep -Eq '^[0-9A-Fa-f]{64}$'; then
        echo "Official release MANIFEST does not contain a valid base.txz SHA-256" >&2
        exit 1
    fi

    NEED_BASE=YES
    if [ -f "$BASE_ARCHIVE" ]; then
        CACHED_BASE_SHA256=$(sha256 -q "$BASE_ARCHIVE")
        if [ "$CACHED_BASE_SHA256" = "$EXPECTED_BASE_SHA256" ]; then
            NEED_BASE=NO
        fi
    fi
    if [ "$NEED_BASE" = "YES" ]; then
        fetch -q -o "$TMP_DIR/base.txz" "$RELEASE_URL/base.txz"
        DOWNLOADED_BASE_SHA256=$(sha256 -q "$TMP_DIR/base.txz")
        if [ "$DOWNLOADED_BASE_SHA256" != "$EXPECTED_BASE_SHA256" ]; then
            echo "base.txz SHA-256 mismatch for $RELEASE_URL/base.txz" >&2
            exit 1
        fi
        install -m 0644 "$TMP_DIR/base.txz" "$BASE_ARCHIVE"
    fi

    BASE_SHA256=$(sha256 -q "$BASE_ARCHIVE")
    if [ "$BASE_SHA256" != "$EXPECTED_BASE_SHA256" ]; then
        echo "Cached base.txz SHA-256 mismatch after installation" >&2
        exit 1
    fi

    tar -xpf "$BASE_ARCHIVE" -C "$ROOT"
else
    for command in git make; do
        if ! command -v "$command" >/dev/null 2>&1; then
            echo "Required command is unavailable: $command" >&2
            exit 1
        fi
    done
    if [ ! -f "${SRC_DIR}/Makefile" ] || [ ! -d "${SRC_DIR}/.git" ]; then
        echo "FreeBSD source Git checkout not found at ${SRC_DIR}" >&2
        exit 1
    fi

    SOURCE_REVISION=$(git -C "$SRC_DIR" rev-parse --verify HEAD)
    SOURCE_BRANCH=$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD)
    case "$SOURCE_BRANCH" in
        releng/*) ;;
        *)
            echo "Source image mode requires a releng/* branch: $SOURCE_BRANCH" >&2
            exit 1
            ;;
    esac

    make -C "$SRC_DIR" installworld DESTDIR="$ROOT"
    make -C "$SRC_DIR" distribution DESTDIR="$ROOT"
fi

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
if [ "$LAB_JAIL_IMAGE_MODE" = "release" ]; then
    cat > "$ROOT/usr/local/share/freebsd-laboratory/image-manifest.json" <<EOF
{
  "schema": "softcloud.freebsd-golden-image/v1",
  "type": "jail-template",
  "image_mode": "release",
  "built_at": "${BUILT_AT}",
  "release": "${LAB_RELEASE}",
  "release_target": "${LAB_RELEASE_TARGET}",
  "release_target_arch": "${LAB_RELEASE_TARGET_ARCH}",
  "release_url": "${RELEASE_URL}",
  "release_manifest_sha256": "${RELEASE_MANIFEST_SHA256}",
  "base_sha256": "${BASE_SHA256}",
  "userland": "${USERLAND_VERSION}",
  "packages": "${PACKAGE_LIST}",
  "snapshot": "${SNAPSHOT}"
}
EOF
else
    cat > "$ROOT/usr/local/share/freebsd-laboratory/image-manifest.json" <<EOF
{
  "schema": "softcloud.freebsd-golden-image/v1",
  "type": "jail-template",
  "image_mode": "source",
  "built_at": "${BUILT_AT}",
  "source_branch": "${SOURCE_BRANCH}",
  "source_revision": "${SOURCE_REVISION}",
  "userland": "${USERLAND_VERSION}",
  "packages": "${PACKAGE_LIST}",
  "snapshot": "${SNAPSHOT}"
}
EOF
fi

sync
zfs snapshot "$SNAPSHOT"
zfs set mountpoint=none "$DATASET"

if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
    TMP_DIR=""
fi
trap - EXIT HUP INT TERM

printf '%s\n' "Built jail golden image:"
printf '  mode:     %s\n' "$LAB_JAIL_IMAGE_MODE"
printf '  snapshot: %s\n' "$SNAPSHOT"
if [ "$LAB_JAIL_IMAGE_MODE" = "release" ]; then
    printf '  release:  %s\n' "$LAB_RELEASE"
    printf '  base:     %s/base.txz\n' "$RELEASE_URL"
    printf '  sha256:   %s\n' "$BASE_SHA256"
else
    printf '  source:   %s (%s)\n' "$SOURCE_BRANCH" "$SOURCE_REVISION"
fi
printf '  runtime:  --jail-template=%s\n' "$SNAPSHOT"
