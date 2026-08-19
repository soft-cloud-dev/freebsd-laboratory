from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .signing import verify_manifest_signature


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_bundle(bundle: Path, trusted_public_key: Path | None = None) -> dict[str, Any]:
    raw_bundle = Path(bundle)
    if raw_bundle.is_symlink():
        raise ValueError("Evidence bundle must not be a symbolic link")
    bundle = raw_bundle.resolve()
    if not bundle.is_dir():
        raise ValueError("Evidence bundle directory does not exist")
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("manifest.json is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid manifest.json: {error}") from error
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json must be an object")
    if manifest.get("schema") != "softcloud.lab-evidence-manifest/v1":
        raise ValueError("Unsupported evidence manifest schema")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("Evidence manifest does not contain artifact hashes")

    verified_artifacts: list[str] = []
    for name, metadata in artifacts.items():
        if not isinstance(name, str) or not name or name in {".", ".."} or Path(name).name != name:
            raise ValueError(f"Invalid artifact path in manifest: {name!r}")
        if not isinstance(metadata, dict):
            raise ValueError(f"Invalid artifact metadata for {name}")
        expected = metadata.get("sha256")
        if not isinstance(expected, str):
            raise ValueError(f"Missing SHA-256 for {name}")
        path = bundle / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Evidence artifact is missing: {name}")
        actual = sha256_file(path)
        if actual != expected:
            raise ValueError(f"Evidence artifact hash mismatch: {name}")
        expected_size = metadata.get("size")
        if isinstance(expected_size, int) and not isinstance(expected_size, bool) and path.stat().st_size != expected_size:
            raise ValueError(f"Evidence artifact size mismatch: {name}")
        verified_artifacts.append(name)

    signature_path = bundle / "manifest.sig.json"
    signature: dict[str, Any] | None = None
    if signature_path.is_symlink():
        raise ValueError("Evidence signature must not be a symbolic link")
    elif signature_path.is_file():
        signature = verify_manifest_signature(
            manifest_path,
            signature_path,
            trusted_public_key=trusted_public_key,
        )
    elif trusted_public_key is not None:
        raise ValueError("A trusted public key was supplied but the bundle is unsigned")

    return {
        "manifest": str(manifest_path),
        "artifacts": sorted(verified_artifacts),
        "signature_verified": signature is not None,
        "key_id": signature.get("key_id") if signature else None,
        "trusted_key_enforced": trusted_public_key is not None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a FreeBSD Laboratory evidence bundle")
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "--public-key",
        type=Path,
        help="Trusted Ed25519 public key. Without this, an embedded key proves integrity but not identity.",
    )
    args = parser.parse_args()
    result = verify_bundle(args.bundle, args.public_key)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
