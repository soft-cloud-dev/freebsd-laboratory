from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .signing import sign_manifest


STAGE_ORDER = (
    "observed",
    "explained",
    "reproduced",
    "modified",
    "verified",
    "recovered",
    "designed",
)

CLIENT_EVENT_KINDS = frozenset({"cell-executed", "notebook-context"})
MACHINE_EVENT_KINDS = frozenset(
    {
        "reproduction-complete",
        "mutation-applied",
        "verification-complete",
        "recovery-complete",
        "design-validated",
    }
)
SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(?:authorization|cookie|credential|password|passwd|secret|token|"
    r"api[_-]?key|private[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
REDACTED_VALUE = "[REDACTED]"


class EvidenceLimitError(ValueError):
    """Base class for evidence resource-limit failures."""


class EvidencePayloadTooLarge(EvidenceLimitError):
    pass


class EvidenceEventLimitReached(EvidenceLimitError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                REDACTED_VALUE
                if SENSITIVE_KEY_RE.search(str(key))
                else redact_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [redact_payload(item) for item in sorted(value, key=str)]
    return value


def minimize_persisted_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Remove transient cell source after deriving a reproducible identity."""

    if kind != "cell-executed":
        return payload

    minimized = dict(payload)
    cell_value = minimized.get("cell")
    if not isinstance(cell_value, dict):
        raise ValueError("cell-executed payload requires a cell object")
    cell = dict(cell_value)
    source = cell.pop("source", "")
    if isinstance(source, list):
        if not all(isinstance(part, str) for part in source):
            raise ValueError("Cell source list must contain only strings")
        source_text = "".join(source)
    elif isinstance(source, str):
        source_text = source
    else:
        raise ValueError("Cell source must be a string or string list")
    source_bytes = source_text.encode("utf-8")
    cell["source_sha256"] = sha256_bytes(source_bytes)
    cell["source_bytes"] = len(source_bytes)
    minimized["cell"] = cell
    return minimized


@dataclass(frozen=True)
class EvidenceEvent:
    sequence: int
    recorded_at: str
    kind: str
    source: str
    payload_sha256: str
    payload: dict[str, Any]


class LabService:
    """Owns a bounded, redacted, durable server-side evidence stream."""

    def __init__(
        self,
        root_dir: Path,
        lab_path: str,
        evidence_dir: str,
        *,
        max_events: int = 10_000,
        max_event_payload_bytes: int = 1024 * 1024,
        fsync_events: bool = True,
    ) -> None:
        if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
            raise ValueError("max_events must be positive")
        if (
            not isinstance(max_event_payload_bytes, int)
            or isinstance(max_event_payload_bytes, bool)
            or max_event_payload_bytes < 1024
        ):
            raise ValueError("max_event_payload_bytes must be at least 1024")

        self.root_dir = root_dir.resolve()
        self.lab_file = self._resolve_inside_root(lab_path)
        self.spec = self._load_spec(self.lab_file)
        self.session_id = uuid.uuid4().hex
        self.started_at = utc_now()
        self.max_events = max_events
        self.max_event_payload_bytes = max_event_payload_bytes
        self.fsync_events = fsync_events
        self._lock = threading.RLock()
        self._events: list[EvidenceEvent] = []

        evidence_root = Path(evidence_dir)
        if not evidence_root.is_absolute():
            evidence_root = self.root_dir / evidence_root
        self.session_dir = evidence_root.resolve() / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self.events_file = self.session_dir / "events.jsonl"
        self.events_file.touch(mode=0o600)
        self.events_file.chmod(0o600)

    def _resolve_inside_root(self, value: str) -> Path:
        raw_path = self.root_dir / value
        if raw_path.is_symlink():
            raise ValueError(f"lab_path must not be a symbolic link: {value}")
        path = raw_path.resolve()
        if path != self.root_dir and self.root_dir not in path.parents:
            raise ValueError(f"Path escapes Jupyter root: {value}")
        return path

    @staticmethod
    def _load_spec(path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise ValueError("lab.yaml must be a regular file")
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"Invalid lab.yaml: {error}") from error
        if not isinstance(document, dict):
            raise ValueError("lab.yaml must contain a mapping")
        if document.get("schema") != "softcloud.lab/v1":
            raise ValueError("Unsupported laboratory schema")
        if not isinstance(document.get("id"), str) or not document["id"]:
            raise ValueError("Laboratory id is required")
        return document

    @staticmethod
    def runtime_identity() -> dict[str, Any]:
        system = platform.system()
        return {
            "system": system,
            "release": platform.release(),
            "machine": platform.machine(),
            "is_freebsd": system == "FreeBSD",
        }

    def _signing_config(self) -> dict[str, Any]:
        evidence = self.spec.get("evidence", {})
        if not isinstance(evidence, dict):
            return {"enabled": False}
        signing = evidence.get("signing", {})
        if signing is None:
            return {"enabled": False}
        if not isinstance(signing, dict):
            raise ValueError("evidence.signing must be a mapping")
        enabled = signing.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("evidence.signing.enabled must be boolean")
        if not enabled:
            return {"enabled": False}
        algorithm = signing.get("algorithm", "ed25519")
        if algorithm != "ed25519":
            raise ValueError("Only Ed25519 evidence signing is supported")
        key_path = signing.get("private_key")
        if not isinstance(key_path, str) or not key_path:
            raise ValueError("evidence.signing.private_key is required when signing is enabled")
        path = Path(key_path).expanduser()
        if not path.is_absolute():
            path = (self.root_dir / path).resolve()
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"evidence.signing.private_key is unavailable: {path}")
        key_id = signing.get("key_id", path.name)
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("evidence.signing.key_id must be a non-empty string")
        return {
            "enabled": True,
            "algorithm": "ed25519",
            "private_key": path,
            "key_id": key_id,
        }

    def record_client_event(self, kind: str, payload: dict[str, Any]) -> EvidenceEvent:
        if not isinstance(kind, str) or kind not in CLIENT_EVENT_KINDS:
            raise ValueError(f"Client event kind is not allowed: {kind}")
        return self._record(kind=kind, payload=payload, source="jupyterlab-observer")

    def record_machine_event(self, kind: str, payload: dict[str, Any]) -> EvidenceEvent:
        if not isinstance(kind, str) or kind not in MACHINE_EVENT_KINDS:
            raise ValueError(f"Machine event kind is not allowed: {kind}")
        return self._record(kind=kind, payload=payload, source="laboratory-server")

    def _record(self, kind: str, payload: dict[str, Any], source: str) -> EvidenceEvent:
        if not isinstance(payload, dict):
            raise ValueError("Event payload must be a mapping")

        redacted_payload = redact_payload(payload)
        if not isinstance(redacted_payload, dict):
            raise ValueError("Redacted event payload must be a mapping")
        incoming_bytes = canonical_json(redacted_payload)
        if len(incoming_bytes) > self.max_event_payload_bytes:
            raise EvidencePayloadTooLarge(
                f"Event payload is {len(incoming_bytes)} bytes; "
                f"limit is {self.max_event_payload_bytes}"
            )

        safe_payload = minimize_persisted_payload(kind, redacted_payload)
        payload_bytes = canonical_json(safe_payload)

        with self._lock:
            if len(self._events) >= self.max_events:
                raise EvidenceEventLimitReached(
                    f"Evidence session reached its {self.max_events}-event limit"
                )
            event = EvidenceEvent(
                sequence=len(self._events) + 1,
                recorded_at=utc_now(),
                kind=kind,
                source=source,
                payload_sha256=sha256_bytes(payload_bytes),
                payload=safe_payload,
            )
            serialized = canonical_json(asdict(event)) + b"\n"
            with self.events_file.open("ab", buffering=0) as stream:
                stream.write(serialized)
                if self.fsync_events:
                    os.fsync(stream.fileno())
            self._events.append(event)
            return event

    @staticmethod
    def _is_positive_int(value: Any) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return value > 0
        if isinstance(value, str) and value.isdigit():
            return int(value) > 0
        return False

    def _completed_stages(self) -> set[str]:
        completed: set[str] = set()
        kinds = {event.kind for event in self._events}

        if "cell-executed" in kinds:
            completed.add("observed")

        if any(
            event.kind == "notebook-context"
            and self._is_positive_int(event.payload.get("markdown_cells", 0))
            for event in self._events
        ):
            completed.add("explained")

        mapping = {
            "reproduction-complete": "reproduced",
            "mutation-applied": "modified",
            "verification-complete": "verified",
            "recovery-complete": "recovered",
            "design-validated": "designed",
        }
        for event_kind, stage in mapping.items():
            if event_kind in kinds:
                completed.add(stage)
        return completed

    def state(self) -> dict[str, Any]:
        with self._lock:
            completed = self._completed_stages()
            stages = [
                {
                    "id": stage,
                    "label": stage.capitalize(),
                    "completed": stage in completed,
                }
                for stage in STAGE_ORDER
            ]
            signing = self._signing_config()
            return {
                "schema": "softcloud.lab-state/v1",
                "lab": {
                    "id": self.spec["id"],
                    "title": self.spec.get("title", self.spec["id"]),
                    "notebook": self.spec.get("notebook"),
                },
                "runtime": self.runtime_identity(),
                "evidence": {
                    "session_id": self.session_id,
                    "events": len(self._events),
                    "max_events": self.max_events,
                    "max_event_payload_bytes": self.max_event_payload_bytes,
                    "attestation": "self-recorded",
                    "signing_enabled": bool(signing["enabled"]),
                },
                "stages": stages,
            }

    def export(self) -> dict[str, Any]:
        with self._lock:
            runtime = self.runtime_identity()
            signing = self._signing_config()
            evidence = {
                "schema": "softcloud.lab-evidence/v1",
                "lab": {
                    "id": self.spec["id"],
                    "spec_sha256": sha256_file(self.lab_file),
                },
                "session": {
                    "id": self.session_id,
                    "started_at": self.started_at,
                    "exported_at": utc_now(),
                    "attestation": "self-recorded",
                },
                "runtime": runtime,
                "events": [asdict(event) for event in self._events],
            }
            environment = {
                "schema": "softcloud.lab-environment/v1",
                "runtime": runtime,
                "declared_runtime": self.spec.get("runtime", {}),
                "executor": self.spec.get("executor", {}),
            }

            evidence_path = self.session_dir / "evidence.json"
            environment_path = self.session_dir / "environment.json"
            manifest_path = self.session_dir / "manifest.json"
            sums_path = self.session_dir / "SHA256SUMS"

            evidence_path.write_bytes(canonical_json(evidence) + b"\n")
            os.chmod(evidence_path, 0o600, follow_symlinks=False)
            environment_path.write_bytes(canonical_json(environment) + b"\n")
            os.chmod(environment_path, 0o600, follow_symlinks=False)

            artifacts = {
                path.name: {
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
                for path in (evidence_path, environment_path, self.events_file)
            }
            manifest = {
                "schema": "softcloud.lab-evidence-manifest/v1",
                "lab_id": self.spec["id"],
                "session_id": self.session_id,
                "event_count": len(self._events),
                "attestation": "self-recorded",
                "generated_at": utc_now(),
                "artifacts": artifacts,
                "signature": {
                    "enabled": bool(signing["enabled"]),
                    "algorithm": signing.get("algorithm"),
                    "key_id": signing.get("key_id"),
                },
            }
            manifest_path.write_bytes(canonical_json(manifest) + b"\n")
            os.chmod(manifest_path, 0o600, follow_symlinks=False)

            signature_path: Path | None = None
            if signing["enabled"]:
                signature_path = sign_manifest(
                    manifest_path,
                    Path(signing["private_key"]),
                    str(signing["key_id"]),
                )

            hashed_paths = [
                evidence_path,
                environment_path,
                self.events_file,
                manifest_path,
            ]
            if signature_path is not None:
                hashed_paths.append(signature_path)
            sums = "".join(
                f"{sha256_file(path)}  {path.name}\n"
                for path in sorted(hashed_paths, key=lambda item: item.name)
            )
            sums_path.write_text(sums, encoding="utf-8")
            os.chmod(sums_path, 0o600, follow_symlinks=False)

            return {
                "session_id": self.session_id,
                "path": str(self.session_dir),
                "files": [path.name for path in hashed_paths] + [sums_path.name],
                "signed": signature_path is not None,
            }
