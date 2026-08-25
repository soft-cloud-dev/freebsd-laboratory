#!/bin/sh
set -eu

if [ "$(uname -s)" != "FreeBSD" ]; then
    echo "build-linux-bhyve-image.sh requires FreeBSD" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "build-linux-bhyve-image.sh must run as root" >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BUILD_ID=${BUILD_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
OUTPUT_DIR=${OUTPUT_DIR:-/var/db/freebsd-laboratory/images}
IMAGE_SIZE=${IMAGE_SIZE:-4g}

ALPINE_VERSION=${ALPINE_VERSION:-3.20.3}
ALPINE_TARBALL="alpine-minirootfs-${ALPINE_VERSION}-x86_64.tar.gz"
ALPINE_URL=${ALPINE_URL:-https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/x86_64/${ALPINE_TARBALL}}
ALPINE_SHA256=${ALPINE_SHA256:-d4e6fd67dcf75e40c451560ac7265166c2b72a0f38ddc9aae756a7de3d1efa0c}

KERNEL_EFI=${KERNEL_EFI:-}
ROOT_PARTUUID="4b786f1e-0000-0000-0000-000000000002"

for cmd in truncate mdconfig gpart newfs_msdos sha256 fetch tar gzip; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Required command is unavailable: $cmd" >&2
        exit 1
    fi
done

mkdir -p "$OUTPUT_DIR"
WORK_DIR="/var/tmp/freebsd-laboratory-linux-img-${BUILD_ID}"
mkdir -p "$WORK_DIR"

cleanup()
{
    if [ -n "${MD_DEV:-}" ] && [ -e "/dev/${MD_DEV}" ]; then
        mdconfig -d -u "${MD_DEV#md}" 2>/dev/null || true
    fi
    rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

# Ensure kernel EFI binary exists
if [ -z "$KERNEL_EFI" ] || [ ! -f "$KERNEL_EFI" ]; then
    KERNEL_EFI=$(find "$OUTPUT_DIR" -name 'vmlinuz-*-bhyve.efi' -print -quit 2>/dev/null || true)
fi

if [ -z "$KERNEL_EFI" ] || [ ! -f "$KERNEL_EFI" ]; then
    echo "Compiled Linux EFI kernel not found. Building kernel first..." >&2
    "${SCRIPT_DIR}/build-linux-kernel.sh"
    KERNEL_EFI=$(find "$OUTPUT_DIR" -name 'vmlinuz-*-bhyve.efi' -print -quit 2>/dev/null || true)
fi

[ -f "$KERNEL_EFI" ] || { echo "Unable to resolve Linux EFI kernel" >&2; exit 1; }
KERNEL_SHA256=$(sha256 -q "$KERNEL_EFI")

# Fetch Alpine mini-rootfs
cd "$WORK_DIR"
if [ ! -f "$ALPINE_TARBALL" ]; then
    printf 'Fetching Alpine mini-rootfs %s...\n' "$ALPINE_VERSION"
    fetch -o "$ALPINE_TARBALL" "$ALPINE_URL"
fi

ACTUAL_ALPINE_SHA256=$(sha256 -q "$ALPINE_TARBALL")
if [ "$ACTUAL_ALPINE_SHA256" != "$ALPINE_SHA256" ]; then
    echo "Alpine rootfs checksum mismatch: expected $ALPINE_SHA256 but got $ACTUAL_ALPINE_SHA256" >&2
    exit 1
fi

RAW_IMAGE="${OUTPUT_DIR}/linux-python-${BUILD_ID}.raw"
printf 'Creating raw disk image (%s): %s\n' "$IMAGE_SIZE" "$RAW_IMAGE"
truncate -s "$IMAGE_SIZE" "$RAW_IMAGE"

MD_DEV=$(mdconfig -a -t vnode -f "$RAW_IMAGE")

printf 'Partitioning disk with GPT (ESP + EXT4 root)...\n'
gpart create -s gpt "$MD_DEV"
# Partition 1: ESP (64MB)
gpart add -t efi -s 64M -l efi-boot "$MD_DEV"
# Partition 2: Root (ext4)
gpart add -t linux-data -l linux-root -i 2 "$MD_DEV"
gpart modify -i 2 -u "$ROOT_PARTUUID" "$MD_DEV" 2>/dev/null || true

# Format ESP as FAT32
newfs_msdos -F 32 -c 1 "/dev/${MD_DEV}p1"

ESP_MOUNT="${WORK_DIR}/mnt_esp"
mkdir -p "$ESP_MOUNT"
mount -t msdosfs "/dev/${MD_DEV}p1" "$ESP_MOUNT"
mkdir -p "${ESP_MOUNT}/EFI/BOOT"
install -m 0644 "$KERNEL_EFI" "${ESP_MOUNT}/EFI/BOOT/BOOTX64.EFI"
umount "$ESP_MOUNT"

# Populate rootfs
ROOT_MOUNT="${WORK_DIR}/mnt_root"
mkdir -p "$ROOT_MOUNT"
ALPINE_TARBALL="${WORK_DIR}/alpine-minirootfs-${ALPINE_VERSION}-x86_64.tar.gz"
printf 'Fetching Alpine mini-rootfs %s...\n' "$ALPINE_VERSION"
fetch -o "$ALPINE_TARBALL" \
    "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION%.*}/releases/x86_64/alpine-minirootfs-${ALPINE_VERSION}-x86_64.tar.gz"

# Stage root filesystem
ROOT_STAGE="${WORK_DIR}/root_stage"
mkdir -p "$ROOT_STAGE"
tar -xzf "$ALPINE_TARBALL" -C "$ROOT_STAGE"

# Configure nameserver & apk repositories
cat > "${ROOT_STAGE}/etc/resolv.conf" <<'EOF'
nameserver 1.1.1.1
nameserver 8.8.8.8
EOF

cat > "${ROOT_STAGE}/etc/apk/repositories" <<EOF
https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION%.*}/main
https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION%.*}/community
EOF

# Bootstrap packages into staging rootfs via apk.static
kldload linux64 >/dev/null 2>&1 || true
APK_STATIC="${WORK_DIR}/apk.static"
if [ ! -f "$APK_STATIC" ]; then
    printf 'Fetching apk.static for package bootstrapping...\n'
    fetch -o "$APK_STATIC" "https://gitlab.alpinelinux.org/api/v4/projects/5/packages/generic/v2.14.4/x86_64/apk.static"
    chmod +x "$APK_STATIC"
    brandelf -t Linux "$APK_STATIC" >/dev/null 2>&1 || true
fi

printf 'Installing packages into Linux rootfs...\n'
"$APK_STATIC" --root "$ROOT_STAGE" --initdb add --update-cache \
    --repository "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION%.*}/main" \
    --repository "https://dl-cdn.alpinelinux.org/alpine/v${ALPINE_VERSION%.*}/community" \
    --allow-untrusted \
    alpine-base openrc openssh python3 py3-pip py3-ipykernel

# Inittab and basic init
cat > "${ROOT_STAGE}/etc/inittab" <<'EOF'
::sysinit:/sbin/openrc sysinit
::sysinit:/sbin/openrc boot
::wait:/sbin/openrc default
ttyS0::respawn:/bin/sh
::ctrlaltdel:/sbin/reboot
::shutdown:/sbin/openrc shutdown
EOF

# Root account without password and unprivileged user freebsd
mkdir -p "${ROOT_STAGE}/home/freebsd/.ssh"
echo "freebsd:x:1001:1001:FreeBSD Laboratory Guest:/home/freebsd:/bin/sh" >> "${ROOT_STAGE}/etc/passwd"
echo "freebsd:x:1001:" >> "${ROOT_STAGE}/etc/group"
echo "root::19800:0:99999:7:::" > "${ROOT_STAGE}/etc/shadow"
echo "freebsd:*::0:::::" >> "${ROOT_STAGE}/etc/shadow"

# Ensure valid shells
mkdir -p "${ROOT_STAGE}/etc"
printf '/bin/sh\n/bin/ash\n/bin/bash\n' >> "${ROOT_STAGE}/etc/shells"
sort -u -o "${ROOT_STAGE}/etc/shells" "${ROOT_STAGE}/etc/shells"

# Hardened SSH daemon config
mkdir -p "${ROOT_STAGE}/etc/ssh"
cat > "${ROOT_STAGE}/etc/ssh/sshd_config" <<'EOF'
Port 22
ListenAddress 0.0.0.0
PermitRootLogin no
PasswordAuthentication no
ChallengeResponseAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
StrictModes no
AllowTcpForwarding local
X11Forwarding no
AllowAgentForwarding no
GatewayPorts no
PermitTunnel no
EOF

# Default network interfaces config
mkdir -p "${ROOT_STAGE}/etc/network"
cat > "${ROOT_STAGE}/etc/network/interfaces" <<'EOF'
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet static
    address 172.31.254.10
    netmask 255.255.255.0
EOF

# First-boot vm-bhyve NoCloud seed parser service (/etc/init.d/freebsd-lab-seed)
mkdir -p "${ROOT_STAGE}/etc/init.d"
cat > "${ROOT_STAGE}/etc/init.d/freebsd-lab-seed" <<'EOF'
#!/sbin/openrc-run

description="Parse vm-bhyve NoCloud seed.iso configuration"

depend() {
    before sshd networking
}

start() {
    ebegin "Checking for vm-bhyve NoCloud seed device"
    mkdir -p /mnt/seed /home/freebsd/.ssh
    chmod 0700 /home/freebsd /home/freebsd/.ssh
    chown -R freebsd:freebsd /home/freebsd

    # Poll for CD-ROM / seed device (/dev/sr0, /dev/cdrom, /dev/sda, /dev/sdb)
    SEED_DEV=""
    for i in $(seq 1 40); do
        for d in /dev/sr0 /dev/cdrom /dev/sda /dev/sdb; do
            if [ -b "$d" ] || [ -e "$d" ]; then
                SEED_DEV="$d"
                break 2
            fi
        done
        sleep 0.1
    done

    if [ -n "$SEED_DEV" ] && mount -t iso9660 -o ro "$SEED_DEV" /mnt/seed 2>/dev/null; then
        # Parse network configuration from network-config or meta-data
        IFACE="eth0"
        if [ -f /mnt/seed/network-config ]; then
            SET_IFACE=$(grep -E 'set-name:' /mnt/seed/network-config 2>/dev/null | awk '{print $2}' | tr -d ' "' || true)
            [ -n "$SET_IFACE" ] && IFACE="$SET_IFACE"
            IP=$(grep -E 'addresses:' /mnt/seed/network-config 2>/dev/null | sed -E 's/.*\[([^]/]+(\/[0-9]+)?)\].*/\1/' | tr -d ' "[]' || true)
            if [ -z "$IP" ]; then
                IP=$(grep -E '^[[:space:]]*-[[:space:]]*[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' /mnt/seed/network-config 2>/dev/null | awk '{print $2}' | tr -d ' "' || true)
            fi
        fi

        if [ -z "${IP:-}" ] && [ -f /mnt/seed/meta-data ]; then
            IP=$(grep -E '^[[:space:]]*ip:' /mnt/seed/meta-data 2>/dev/null | awk '{print $2}' | tr -d ' "' || true)
            META_IFACE=$(grep -E '^[[:space:]]*interface:' /mnt/seed/meta-data 2>/dev/null | awk '{print $2}' | tr -d ' "' || true)
            [ -n "$META_IFACE" ] && IFACE="$META_IFACE"
        fi

        if [ -n "${IP:-}" ]; then
            ip link set dev "$IFACE" up 2>/dev/null || ifconfig "$IFACE" up 2>/dev/null || true
            ip addr flush dev "$IFACE" 2>/dev/null || true
            ip addr add "$IP" dev "$IFACE" 2>/dev/null || ifconfig "$IFACE" "${IP%/*}" netmask 255.255.255.0 2>/dev/null || true
        fi

        # Parse user-data for SSH authorized keys
        if [ -f /mnt/seed/user-data ]; then
            awk '/ssh-ed25519|ssh-rsa/ {for(i=1;i<=NF;i++) if($i ~ /^ssh-/) {print substr($0, index($0, $i)); break}}' /mnt/seed/user-data > /home/freebsd/.ssh/authorized_keys 2>/dev/null || true
            chmod 0600 /home/freebsd/.ssh/authorized_keys
            chown freebsd:freebsd /home/freebsd/.ssh/authorized_keys
        fi
        umount /mnt/seed 2>/dev/null || true
    fi

    # Ensure host keys exist
    if [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
        ssh-keygen -A 2>/dev/null || true
    fi
    eend 0
}
EOF
chmod 0755 "${ROOT_STAGE}/etc/init.d/freebsd-lab-seed"

# Enable OpenRC services in runlevels
mkdir -p "${ROOT_STAGE}/etc/runlevels/sysinit" "${ROOT_STAGE}/etc/runlevels/boot" "${ROOT_STAGE}/etc/runlevels/default"
ln -sf /etc/init.d/devfs "${ROOT_STAGE}/etc/runlevels/sysinit/devfs" 2>/dev/null || true
ln -sf /etc/init.d/mdev "${ROOT_STAGE}/etc/runlevels/sysinit/mdev" 2>/dev/null || true
ln -sf /etc/init.d/hwdrivers "${ROOT_STAGE}/etc/runlevels/sysinit/hwdrivers" 2>/dev/null || true
ln -sf /etc/init.d/freebsd-lab-seed "${ROOT_STAGE}/etc/runlevels/boot/freebsd-lab-seed" 2>/dev/null || true
ln -sf /etc/init.d/networking "${ROOT_STAGE}/etc/runlevels/boot/networking" 2>/dev/null || true
ln -sf /etc/init.d/sshd "${ROOT_STAGE}/etc/runlevels/default/sshd" 2>/dev/null || true

# Write root filesystem into partition 2 using mke2fs (e2fsprogs) or makefs (ext2fs)
if command -v mke2fs >/dev/null 2>&1; then
    mke2fs -t ext4 -d "$ROOT_STAGE" "/dev/${MD_DEV}p2"
elif command -v makefs >/dev/null 2>&1 && makefs -h 2>&1 | grep -q 'ext2fs'; then
    makefs -t ext2fs -M 2g "${WORK_DIR}/root.ext4" "$ROOT_STAGE"
    dd if="${WORK_DIR}/root.ext4" of="/dev/${MD_DEV}p2" bs=1M status=none
else
    kldload ext2fs >/dev/null 2>&1 || true
    if command -v mkfs.ext4 >/dev/null 2>&1; then
        mkfs.ext4 -F "/dev/${MD_DEV}p2"
        mount -t ext2fs "/dev/${MD_DEV}p2" "$ROOT_MOUNT"
        cp -a "${ROOT_STAGE}/." "$ROOT_MOUNT/"
        umount "$ROOT_MOUNT"
    fi
fi

VERSIONED_IMAGE="${OUTPUT_DIR}/linux-python-${BUILD_ID}.raw"
CURRENT_IMAGE="${OUTPUT_DIR}/linux-python.raw"
ln -sfn "$(basename "$VERSIONED_IMAGE")" "$CURRENT_IMAGE"
sha256 -q "$RAW_IMAGE" > "${VERSIONED_IMAGE}.sha256"

cat > "${OUTPUT_DIR}/linux-python-${BUILD_ID}.manifest" <<EOF
schema=softcloud.freebsd-golden-image/v1
type=bhyve-raw-linux
build_id=${BUILD_ID}
kernel_version=${LINUX_VERSION:-6.6.78}
kernel_sha256=${KERNEL_SHA256}
rootfs_version=alpine-${ALPINE_VERSION}
rootfs_sha256=${ALPINE_SHA256}
root_partuuid=${ROOT_PARTUUID}
packages=python3;py3-ipykernel;openssh-server
sha256=$(cat "${VERSIONED_IMAGE}.sha256")
EOF

printf 'Successfully built Linux bhyve image:\n'
printf '  Image:    %s\n' "$VERSIONED_IMAGE"
printf '  SHA256:   %s\n' "$(cat "${VERSIONED_IMAGE}.sha256")"
printf '  Manifest: %s\n' "${OUTPUT_DIR}/linux-python-${BUILD_ID}.manifest"
