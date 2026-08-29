"""Artifact Store and Resolver for FreeBSD Laboratory.

Consumes declarative `softcloud.artifact/v1` manifests produced by os-* distribution
repositories and resolves them into host-specific storage targets (ZFS datasets,
raw disk images, zvols) while verifying integrity and capabilities.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SCHEMA_NAME = "softcloud.artifact/v1"
SUPPORTED_FORMATS = {"zfs-snapshot", "raw-disk-image", "rootfs-tar", "oci-image", "kernel-binary"}
SUPPORTED_TARGETS = {"jail", "bhyve", "jail-linuxulator", "containerd", "hosted", "bare-metal"}


class ArtifactValidationError(ValueError):
    """Raised when an artifact manifest fails schema or integrity validation."""


class ArtifactNotFoundError(KeyError):
    """Raised when a requested artifact ID is not registered in the store."""


class ArtifactCapabilityError(ValueError):
    """Raised when an artifact lacks required capabilities for the requested runtime."""


def validate_artifact_manifest(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a parsed artifact manifest against softcloud.artifact/v1 invariants.

    Ensures bool-before-int type checking and strict structure.
    """
    if not isinstance(data, dict):
        raise ArtifactValidationError(f"Manifest must be a JSON object, got {type(data).__name__}")

    if data.get("schema") != SCHEMA_NAME:
        raise ArtifactValidationError(
            f"Invalid schema: expected '{SCHEMA_NAME}', got '{data.get('schema')}'"
        )

    art = data.get("artifact")
    if not isinstance(art, dict):
        raise ArtifactValidationError("Missing or invalid 'artifact' mapping in manifest")

    # Required string fields
    for field in ("id", "os", "profile", "architecture", "format"):
        val = art.get(field)
        if not isinstance(val, str) or not val.strip():
            raise ArtifactValidationError(f"Artifact field '{field}' must be a non-empty string")

    if art["format"] not in SUPPORTED_FORMATS:
        raise ArtifactValidationError(
            f"Unsupported artifact format '{art['format']}'. Supported: {sorted(SUPPORTED_FORMATS)}"
        )

    # Source mapping
    src = art.get("source")
    if not isinstance(src, dict) or not isinstance(src.get("repository"), str):
        raise ArtifactValidationError("Artifact 'source.repository' must be specified")

    # Runtime mapping
    runtime = art.get("runtime")
    if not isinstance(runtime, dict):
        raise ArtifactValidationError("Artifact 'runtime' must be an object")
    target = runtime.get("target")
    eff_kernel = runtime.get("effective_kernel")
    if not isinstance(target, str) or target not in SUPPORTED_TARGETS:
        raise ArtifactValidationError(
            f"Invalid runtime target '{target}'. Supported: {sorted(SUPPORTED_TARGETS)}"
        )
    if not isinstance(eff_kernel, str) or not eff_kernel.strip():
        raise ArtifactValidationError("Artifact 'runtime.effective_kernel' must be specified")

    # Capabilities
    caps = art.get("capabilities")
    if not isinstance(caps, list):
        raise ArtifactValidationError("Artifact 'capabilities' must be a list of strings")
    for c in caps:
        if not isinstance(c, str) or not c.strip():
            raise ArtifactValidationError("Capabilities must be non-empty strings")

    # Integrity
    integrity = art.get("integrity")
    if not isinstance(integrity, dict):
        raise ArtifactValidationError("Artifact 'integrity' must be an object")
    digest = integrity.get("digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise ArtifactValidationError("Artifact 'integrity.digest' must be a 'sha256:<hex>' string")
    hex_part = digest.split(":", 1)[1]
    if len(hex_part) != 64 or not all(c in "0123456789abcdefABCDEF" for c in hex_part):
        raise ArtifactValidationError(f"Invalid sha256 digest format: {digest}")

    # Optional size_bytes check with bool-before-int
    if "size_bytes" in integrity:
        size = integrity["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ArtifactValidationError(
                f"Artifact 'integrity.size_bytes' must be a non-negative integer, got {size!r}"
            )

    return data


class ArtifactStore:
    """Registry and storage resolver for laboratory OS distribution artifacts."""

    def __init__(
        self,
        zfs_pool: str = "zroot",
        dataset_parent: str = "zroot/jails/templates",
        image_dir: str = "/var/db/freebsd-laboratory/images",
    ) -> None:
        self.zfs_pool = zfs_pool
        self.dataset_parent = dataset_parent.rstrip("/")
        self.image_dir = Path(image_dir)
        self._artifacts: Dict[str, Dict[str, Any]] = {}

    def register(self, manifest_data_or_path: Any) -> str:
        """Register an artifact manifest from dict, JSON string, or Path.

        Returns the artifact ID.
        """
        if isinstance(manifest_data_or_path, (str, Path)):
            path = Path(manifest_data_or_path)
            if path.exists():
                if path.is_symlink():
                    raise ArtifactValidationError(f"Refusing to load symlinked manifest: {path}")
                content = path.read_text(encoding="utf-8")
                data = json.loads(content)
            else:
                data = json.loads(str(manifest_data_or_path))
        elif isinstance(manifest_data_or_path, dict):
            data = manifest_data_or_path
        else:
            raise ArtifactValidationError(
                f"Unsupported manifest input type: {type(manifest_data_or_path).__name__}"
            )

        validated = validate_artifact_manifest(data)
        art_id = validated["artifact"]["id"]
        self._artifacts[art_id] = validated["artifact"]
        return art_id

    def get(self, artifact_id: str) -> Dict[str, Any]:
        """Retrieve registered artifact manifest by ID."""
        if artifact_id not in self._artifacts:
            raise ArtifactNotFoundError(f"Artifact '{artifact_id}' is not registered in store")
        return self._artifacts[artifact_id]

    def list_artifacts(self) -> List[Dict[str, Any]]:
        """List all registered artifacts."""
        return list(self._artifacts.values())

    def resolve_storage(self, artifact_id: str) -> Dict[str, Any]:
        """Resolve an artifact ID to host storage coordinates."""
        art = self.get(artifact_id)
        fmt = art["format"]

        if fmt == "zfs-snapshot":
            dataset_name = f"{self.dataset_parent}/{art['id']}"
            snapshot_name = f"{dataset_name}@clean"
            return {
                "format": fmt,
                "dataset": dataset_name,
                "snapshot": snapshot_name,
                "zpool": self.zfs_pool,
            }
        elif fmt in ("raw-disk-image", "kernel-binary", "rootfs-tar"):
            ext = ".raw" if fmt == "raw-disk-image" else (".efi" if fmt == "kernel-binary" else ".tar.gz")
            image_path = self.image_dir / f"{art['id']}{ext}"
            return {
                "format": fmt,
                "file_path": str(image_path),
                "zvol_name": f"{self.zfs_pool}/vm/.zvol/{art['id']}@ready",
            }
        elif fmt == "oci-image":
            return {
                "format": fmt,
                "image_ref": f"softcloud/{art['os']}:{art['profile']}",
            }
        else:
            raise ArtifactValidationError(f"Unknown format: {fmt}")

    def verify_integrity(
        self,
        artifact_id: str,
        actual_path: Optional[Path | str] = None,
        actual_bytes: Optional[bytes] = None,
    ) -> bool:
        """Verify the integrity of a stored artifact against its declared SHA-256 digest."""
        art = self.get(artifact_id)
        expected_digest = art["integrity"]["digest"]
        expected_hex = expected_digest.split(":", 1)[1].lower()

        hasher = hashlib.sha256()

        if actual_bytes is not None:
            hasher.update(actual_bytes)
        elif actual_path is not None:
            path = Path(actual_path)
            if path.is_symlink():
                raise ArtifactValidationError(f"Refusing to verify symlinked artifact: {path}")
            if not path.is_file():
                raise ArtifactValidationError(f"Artifact file not found: {path}")
            with open(path, "rb") as f:
                while chunk := f.read(65536):
                    hasher.update(chunk)
        else:
            raise ArtifactValidationError("Either actual_path or actual_bytes must be provided")

        actual_hex = hasher.hexdigest().lower()
        if actual_hex != expected_hex:
            raise ArtifactValidationError(
                f"Digest mismatch for artifact '{artifact_id}': expected {expected_hex}, got {actual_hex}"
            )
        return True

    def check_capabilities(
        self, artifact_id: str, required_capabilities: List[str] | Set[str]
    ) -> Tuple[bool, List[str]]:
        """Verify that an artifact provides all required capabilities."""
        art = self.get(artifact_id)
        provided = set(art.get("capabilities", []))
        missing = [cap for cap in required_capabilities if cap not in provided]
        if missing:
            return False, missing
        return True, []
