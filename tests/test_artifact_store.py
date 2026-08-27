import hashlib
import json
from pathlib import Path
import pytest

from freebsd_laboratory.artifact_store import (
    ArtifactCapabilityError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactValidationError,
    validate_artifact_manifest,
)


@pytest.fixture
def sample_jail_manifest():
    return {
        "schema": "softcloud.artifact/v1",
        "artifact": {
            "id": "freebsd-jupyter-amd64-20260827",
            "os": "freebsd",
            "profile": "jupyter",
            "architecture": "amd64",
            "format": "zfs-snapshot",
            "source": {
                "repository": "soft-cloud-dev/os-freebsd",
                "commit": "d6a3ac4b3c459cd973a724691021c2c77260b649"
            },
            "runtime": {
                "target": "jail",
                "effective_kernel": "freebsd"
            },
            "capabilities": ["ssh", "jupyter-kernel"],
            "guest": {
                "user": "freebsd",
                "kernel_transport": "ssh",
                "init": "freebsd-rc",
                "cloud_init": "nuageinit"
            },
            "integrity": {
                "digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "size_bytes": 1024
            }
        }
    }


@pytest.fixture
def sample_bhyve_manifest():
    return {
        "schema": "softcloud.artifact/v1",
        "artifact": {
            "id": "linux-alpine-jupyter-amd64-20260827",
            "os": "linux",
            "profile": "alpine-jupyter",
            "architecture": "amd64",
            "format": "raw-disk-image",
            "source": {
                "repository": "soft-cloud-dev/os-linux",
                "commit": "d6a3ac4b3c459cd973a724691021c2c77260b649"
            },
            "runtime": {
                "target": "bhyve",
                "effective_kernel": "linux"
            },
            "capabilities": ["ssh", "jupyter-kernel"],
            "integrity": {
                "digest": "sha256:5aa39a9bd555133ad741058f9908a277e6b36bb928481e747d885b50aaaa93ed",
                "size_bytes": 4294967296
            }
        }
    }


def test_validate_valid_manifest(sample_jail_manifest):
    res = validate_artifact_manifest(sample_jail_manifest)
    assert res["artifact"]["id"] == "freebsd-jupyter-amd64-20260827"


def test_validate_rejects_invalid_schema(sample_jail_manifest):
    sample_jail_manifest["schema"] = "invalid.schema/v2"
    with pytest.raises(ArtifactValidationError, match="Invalid schema"):
        validate_artifact_manifest(sample_jail_manifest)


def test_validate_rejects_bool_in_size_bytes(sample_jail_manifest):
    sample_jail_manifest["artifact"]["integrity"]["size_bytes"] = True
    with pytest.raises(ArtifactValidationError, match="must be a non-negative integer"):
        validate_artifact_manifest(sample_jail_manifest)


def test_validate_rejects_malformed_digest(sample_jail_manifest):
    sample_jail_manifest["artifact"]["integrity"]["digest"] = "sha256:short"
    with pytest.raises(ArtifactValidationError, match="Invalid sha256 digest format"):
        validate_artifact_manifest(sample_jail_manifest)


def test_artifact_store_registration_and_get(sample_jail_manifest, sample_bhyve_manifest):
    store = ArtifactStore()
    id1 = store.register(sample_jail_manifest)
    id2 = store.register(sample_bhyve_manifest)

    assert id1 == "freebsd-jupyter-amd64-20260827"
    assert id2 == "linux-alpine-jupyter-amd64-20260827"

    art1 = store.get(id1)
    assert art1["os"] == "freebsd"
    assert art1["format"] == "zfs-snapshot"

    all_arts = store.list_artifacts()
    assert len(all_arts) == 2


def test_artifact_store_get_unknown_raises():
    store = ArtifactStore()
    with pytest.raises(ArtifactNotFoundError):
        store.get("non-existent-artifact")


def test_resolve_storage_zfs_snapshot(sample_jail_manifest):
    store = ArtifactStore(zfs_pool="zroot", dataset_parent="zroot/jails/templates")
    art_id = store.register(sample_jail_manifest)

    coords = store.resolve_storage(art_id)
    assert coords["format"] == "zfs-snapshot"
    assert coords["dataset"] == "zroot/jails/templates/freebsd-jupyter-amd64-20260827"
    assert coords["snapshot"] == "zroot/jails/templates/freebsd-jupyter-amd64-20260827@clean"


def test_resolve_storage_raw_disk_image(sample_bhyve_manifest):
    store = ArtifactStore(image_dir="/var/db/freebsd-laboratory/images", zfs_pool="zroot")
    art_id = store.register(sample_bhyve_manifest)

    coords = store.resolve_storage(art_id)
    assert coords["format"] == "raw-disk-image"
    assert coords["file_path"] == "/var/db/freebsd-laboratory/images/linux-alpine-jupyter-amd64-20260827.raw"
    assert coords["zvol_name"] == "zroot/vm/.zvol/linux-alpine-jupyter-amd64-20260827@ready"


def test_verify_integrity_bytes(sample_jail_manifest):
    store = ArtifactStore()
    payload = b""  # sha256 for empty bytes is e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
    art_id = store.register(sample_jail_manifest)

    assert store.verify_integrity(art_id, actual_bytes=payload) is True

    with pytest.raises(ArtifactValidationError, match="Digest mismatch"):
        store.verify_integrity(art_id, actual_bytes=b"corrupted payload")


def test_verify_integrity_file(tmp_path, sample_jail_manifest):
    store = ArtifactStore()
    art_id = store.register(sample_jail_manifest)

    valid_file = tmp_path / "valid.img"
    valid_file.write_bytes(b"")
    assert store.verify_integrity(art_id, actual_path=valid_file) is True

    invalid_file = tmp_path / "invalid.img"
    invalid_file.write_bytes(b"tampered")
    with pytest.raises(ArtifactValidationError, match="Digest mismatch"):
        store.verify_integrity(art_id, actual_path=invalid_file)


def test_symlink_resistance(tmp_path, sample_jail_manifest):
    store = ArtifactStore()
    art_id = store.register(sample_jail_manifest)

    target_file = tmp_path / "real.img"
    target_file.write_bytes(b"")
    symlink_file = tmp_path / "symlink.img"
    symlink_file.symlink_to(target_file)

    with pytest.raises(ArtifactValidationError, match="Refusing to verify symlinked"):
        store.verify_integrity(art_id, actual_path=symlink_file)

    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(sample_jail_manifest))
    symlink_manifest = tmp_path / "symlink_manifest.json"
    symlink_manifest.symlink_to(manifest_file)

    with pytest.raises(ArtifactValidationError, match="Refusing to load symlinked manifest"):
        store.register(symlink_manifest)


def test_check_capabilities(sample_jail_manifest):
    store = ArtifactStore()
    art_id = store.register(sample_jail_manifest)

    ok, missing = store.check_capabilities(art_id, ["ssh", "jupyter-kernel"])
    assert ok is True
    assert missing == []

    ok, missing = store.check_capabilities(art_id, ["ssh", "jupyter-kernel", "systemd"])
    assert ok is False
    assert missing == ["systemd"]
