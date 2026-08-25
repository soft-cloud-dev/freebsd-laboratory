from pathlib import Path

KERNEL_CONFIG = Path("deploy/freebsd/images/linux-kernel.config")
KERNEL_BUILDER = Path("deploy/freebsd/images/build-linux-kernel.sh")
IMAGE_BUILDER = Path("deploy/freebsd/images/build-linux-bhyve-image.sh")
VM_TEMPLATE = Path("freebsd_laboratory/vm-bhyve/linux-lab.conf")
VM_MEMDISK_TEMPLATE = Path("freebsd_laboratory/vm-bhyve/linux-lab-memdisk.conf")
IMPORT_SCRIPT = Path("deploy/freebsd/import-bhyve-zvol.sh")
INSTALL_TEMPLATE_SCRIPT = Path("deploy/freebsd/install-vm-bhyve-template.sh")


def test_linux_kernel_config_contains_mandatory_virtio_and_seed_drivers() -> None:
    assert KERNEL_CONFIG.is_file()
    config_text = KERNEL_CONFIG.read_text(encoding="utf-8")

    # VirtIO drivers
    assert "CONFIG_VIRTIO=y" in config_text
    assert "CONFIG_VIRTIO_PCI=y" in config_text
    assert "CONFIG_VIRTIO_BLK=y" in config_text
    assert "CONFIG_VIRTIO_NET=y" in config_text
    assert "CONFIG_VIRTIO_BALLOON=y" in config_text
    assert "CONFIG_VIRTIO_CONSOLE=y" in config_text

    # AHCI and ISO9660 for vm-bhyve seed.iso
    assert "CONFIG_ATA=y" in config_text
    assert "CONFIG_SATA_AHCI=y" in config_text
    assert "CONFIG_SCSI=y" in config_text
    assert "CONFIG_BLK_DEV_SR=y" in config_text
    assert "CONFIG_ISO9660_FS=y" in config_text

    # EFI Stub and Serial Console
    assert "CONFIG_EFI=y" in config_text
    assert "CONFIG_EFI_STUB=y" in config_text
    assert "CONFIG_SERIAL_8250=y" in config_text
    assert "CONFIG_SERIAL_8250_CONSOLE=y" in config_text

    # Deterministic PARTUUID Root
    assert "CONFIG_CMDLINE_BOOL=y" in config_text
    assert "root=/dev/vda2" in config_text or "root=PARTUUID=" in config_text
    assert "net.ifnames=0" in config_text

    # Ext4 & pseudo filesystems
    assert "CONFIG_EXT4_FS=y" in config_text
    assert "CONFIG_DEVTMPFS=y" in config_text
    assert "CONFIG_DEVTMPFS_MOUNT=y" in config_text


def test_linux_kernel_builder_uses_gmake_and_llvm() -> None:
    assert KERNEL_BUILDER.is_file()
    builder_text = KERNEL_BUILDER.read_text(encoding="utf-8")

    assert "gmake" in builder_text
    assert "LLVM=1" in builder_text
    assert "ARCH=x86_64" in builder_text
    assert "LINUX_SHA256" in builder_text
    assert "bzImage" in builder_text
    assert "sha256 -q" in builder_text


def test_linux_bhyve_image_builder_contract() -> None:
    assert IMAGE_BUILDER.is_file()
    image_text = IMAGE_BUILDER.read_text(encoding="utf-8")

    assert "gpart create -s gpt" in image_text
    assert "gpart add -t efi" in image_text
    assert "gpart add -t linux-data" in image_text
    assert "BOOTX64.EFI" in image_text
    assert "ROOT_PARTUUID=" in image_text
    assert "freebsd:x:1001:1001" in image_text
    assert "AllowTcpForwarding local" in image_text
    assert "Subsystem sftp /usr/lib/ssh/sftp-server" in image_text
    assert "seed.iso" in image_text
    assert "/dev/sr0" in image_text
    assert "/meta-data" in image_text
    assert "/user-data" in image_text
    assert "authorized_keys" in image_text


def test_linux_vm_bhyve_templates_use_uefi() -> None:
    assert VM_TEMPLATE.is_file()
    template_text = VM_TEMPLATE.read_text(encoding="utf-8")
    assert 'loader="uefi"' in template_text
    assert 'network0_switch="freebsdlab"' in template_text
    assert 'disk0_type="virtio-blk"' in template_text

    assert VM_MEMDISK_TEMPLATE.is_file()
    memdisk_text = VM_MEMDISK_TEMPLATE.read_text(encoding="utf-8")
    assert 'loader="uefi"' in memdisk_text
    assert 'disk0_name="/dev/md0"' in memdisk_text


def test_import_bhyve_zvol_supports_parameterized_image() -> None:
    assert IMPORT_SCRIPT.is_file()
    import_text = IMPORT_SCRIPT.read_text(encoding="utf-8")
    assert "IMAGE_BASE=$(basename \"$RAW_IMAGE\" .raw)" in import_text
    assert 'ZVOL_NAME="${ZVOL_PARENT}/${IMAGE_BASE}"' in import_text


def test_install_vm_bhyve_template_includes_linux_templates() -> None:
    assert INSTALL_TEMPLATE_SCRIPT.is_file()
    install_text = INSTALL_TEMPLATE_SCRIPT.read_text(encoding="utf-8")
    assert "linux-lab.conf" in install_text
    assert "linux-lab-memdisk.conf" in install_text
