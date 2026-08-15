from __future__ import annotations

import hashlib
import json
import platform
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


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


@dataclass(frozen=True)
class EvidenceEvent:
    sequence: int
    recorded_at: str
    kind: str
    source: str
    payload_sha256: str
    payload: dict[str, Any]


class LabService:
    """Owns the server-side evidence stream and machine-derived lab state."""

    def __init__(self, root_dir: Path, lab_path: str, evidence_dir: str) -> None:
        self.root_dir = root_dir.resolve()
        self.lab_file = self._resolve_inside_root(lab_path)
        self.spec = self._load_spec(self.lab_file)
        self.session_id = uuid.uuid4().hex
        self.started_at = utc_now()
        self._lock = threading.RLock()
        self._events: list[EvidenceEvent] = []

        evidence_root = Path(evidence_dir)
        if not evidence_root.is_absolute():
            evidence_root = self.root_dir / evidence_root
        self.session_dir = evidence_root.resolve() / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=False)
        self.events_file = self.session_dir / "events.jsonl"
        self.events_file.touch(mode=0o600)

    def _resolve_inside_root(self, value: str) -> Path:
        path = (self.root_dir / value).resolve()
        if path != self.root_dir and self.root_dir not in path.parents:
            raise ValueError(f"Path escapes Jupyter root: {value}")
        return path

    @staticmethod
    def _load_spec(path: Path) -> dict[str, Any]:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
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

    def record_client_event(self, kind: str, payload: dict[str, Any]) -> EvidenceEvent:
        if kind not in CLIENT_EVENT_KINDS:
            raise ValueError(f"Client event kind is not allowed: {kind}")
        return self._record(kind=kind, payload=payload, source="jupyterlab-observer")

    def record_machine_event(self, kind: str, payload: dict[str, Any]) -> EvidenceEvent:
        if kind not in MACHINE_EVENT_KINDS:
            raise ValueError(f"Machine event kind is not allowed: {kind}")
        return self._record(kind=kind, payload=payload, source="laboratory-server")

    def _record(self, kind: str, payload: dict[str, Any], source: str) -> EvidenceEvent:
        if not isinstance(payload, dict):
            raise ValueError("Event payload must be a mapping")

        with self._lock:
            event = EvidenceEvent(
                sequence=len(self._events) + 1,
                recorded_at=utc_now(),
                kind=kind,
                source=source,
                payload_sha256=sha256_bytes(canonical_json(payload)),
                payload=payload,
            )
            self._events.append(event)
            with self.events_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(asdict(event), sort_keys=True, ensure_ascii=False))
                stream.write("\n")
            return event

    def _completed_stages(self) -> set[str]:
        completed: set[str] = set()
        kinds = {event.kind for event in self._events}

        if "cell-executed" in kinds:
            completed.add("observed")

        if any(
            event.kind == "notebook-context"
            and int(event.payload.get("markdown_cells", 0)) > 0
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
                    "attestation": "self-recorded",
                },
                "stages": stages,
            }

    def export(self) -> dict[str, Any]:
        with self._lock:
            runtime = self.runtime_identity()
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
            environment_path.write_bytes(canonical_json(environment) + b"\n")

            manifest = {
                "schema": "softcloud.lab-evidence-manifest/v1",
                "lab_id": self.spec["id"],
                "session_id": self.session_id,
                "event_count": len(self._events),
                "attestation": "self-recorded",
                "generated_at": utc_now(),
            }
            manifest_path.write_bytes(canonical_json(manifest) + b"\n")

            hashed_paths = [
                evidence_path,
                environment_path,
                self.events_file,
                manifest_path,
            ]
            sums = "".join(
                f"{sha256_file(path)}  {path.name}\n"
                for path in sorted(hashed_paths, key=lambda item: item.name)
            )
            sums_path.write_text(sums, encoding="utf-8")

            return {
                "session_id": self.session_id,
                "path": str(self.session_dir),
                "files": [path.name for path in hashed_paths] + [sums_path.name],
            }
