from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any


SIGNATURE_SCHEMA = "softcloud.lab-signature/v1"


def _cryptography() -> tuple[Any, Any, Any]:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as error:
        raise RuntimeError(
            "Ed25519 signing requires the 'signing' extra: pip install 'freebsd-laboratory[signing]'"
        ) from error
    return serialization, Ed25519PrivateKey, Ed25519PublicKey


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _public_pem(public_key: Any) -> bytes:
    serialization, _, _ = _cryptography()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def load_private_key(path: Path) -> Any:
    serialization, Ed25519PrivateKey, _ = _cryptography()
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Evidence signing key must be an Ed25519 private key")
    return key


def load_public_key(path: Path) -> Any:
    serialization, _, Ed25519PublicKey = _cryptography()
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Evidence verification key must be an Ed25519 public key")
    return key


def sign_manifest(manifest_path: Path, private_key_path: Path, key_id: str) -> Path:
    private_key = load_private_key(private_key_path)
    manifest_bytes = manifest_path.read_bytes()
    signature = private_key.sign(manifest_bytes)
    public_pem = _public_pem(private_key.public_key())
    document = {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": "ed25519",
        "key_id": key_id,
        "manifest": manifest_path.name,
        "manifest_sha256": sha256_bytes(manifest_bytes),
        "public_key_sha256": sha256_bytes(public_pem),
        "public_key_pem": public_pem.decode("ascii"),
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    }
    signature_path = manifest_path.with_name("manifest.sig.json")
    signature_path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    signature_path.chmod(0o600)
    return signature_path


def verify_manifest_signature(
    manifest_path: Path,
    signature_path: Path,
    trusted_public_key: Path | None = None,
) -> dict[str, Any]:
    _, _, Ed25519PublicKey = _cryptography()
    document = json.loads(signature_path.read_text(encoding="utf-8"))
    if document.get("schema") != SIGNATURE_SCHEMA or document.get("algorithm") != "ed25519":
        raise ValueError("Unsupported evidence signature format")

    manifest_bytes = manifest_path.read_bytes()
    if document.get("manifest_sha256") != sha256_bytes(manifest_bytes):
        raise ValueError("Manifest hash does not match signature metadata")

    if trusted_public_key is not None:
        public_key = load_public_key(trusted_public_key)
        trusted_pem = _public_pem(public_key)
        if document.get("public_key_sha256") != sha256_bytes(trusted_pem):
            raise ValueError("Signature key does not match the trusted public key")
    else:
        serialization, _, _ = _cryptography()
        embedded = document.get("public_key_pem")
        if not isinstance(embedded, str):
            raise ValueError("Signature metadata does not contain a public key")
        loaded = serialization.load_pem_public_key(embedded.encode("ascii"))
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError("Embedded signature key is not Ed25519")
        public_key = loaded

    signature_raw = document.get("signature_base64")
    if not isinstance(signature_raw, str):
        raise ValueError("Signature metadata does not contain a signature")
    signature = base64.b64decode(signature_raw, validate=True)
    public_key.verify(signature, manifest_bytes)
    return document
