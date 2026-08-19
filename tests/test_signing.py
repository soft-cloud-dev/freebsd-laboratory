from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from freebsd_laboratory.service import LabService
from freebsd_laboratory.verify import verify_bundle


def write_keys(tmp_path: Path) -> tuple[Path, Path]:
    private_key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "evidence-private.pem"
    public_path = tmp_path / "evidence-public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


def make_signed_service(tmp_path: Path, private_key: Path) -> LabService:
    (tmp_path / "lab.yaml").write_text(
        f"""\
schema: softcloud.lab/v1
id: signed-lab
runtime:
  os: freebsd
executor:
  type: jail
evidence:
  signing:
    enabled: true
    algorithm: ed25519
    key_id: test-runner
    private_key: {private_key}
""",
        encoding="utf-8",
    )
    return LabService(tmp_path, "lab.yaml", ".evidence")


def test_signed_manifest_authenticates_artifact_hashes(tmp_path: Path) -> None:
    private_key, public_key = write_keys(tmp_path)
    service = make_signed_service(tmp_path, private_key)
    service.record_client_event(
        "cell-executed",
        {
            "notebook": "Signed.ipynb",
            "cell_id": "a",
            "success": True,
            "cell": {
                "cell_type": "code",
                "source": "print('signed')\n",
                "execution_count": 1,
                "output_count": 0,
            },
        },
    )

    result = service.export()
    bundle = Path(result["path"])

    assert result["signed"] is True
    assert (bundle / "manifest.sig.json").is_file()
    verified = verify_bundle(bundle, public_key)
    assert verified["signature_verified"] is True
    assert verified["trusted_key_enforced"] is True
    assert verified["key_id"] == "test-runner"


def test_signed_bundle_detects_artifact_tampering(tmp_path: Path) -> None:
    private_key, public_key = write_keys(tmp_path)
    service = make_signed_service(tmp_path, private_key)
    result = service.export()
    bundle = Path(result["path"])

    (bundle / "evidence.json").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        verify_bundle(bundle, public_key)
