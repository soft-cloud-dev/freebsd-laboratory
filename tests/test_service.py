from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import freebsd_laboratory.service as service_module
from freebsd_laboratory.service import (
    EvidenceEventLimitReached,
    EvidencePayloadTooLarge,
    LabService,
)


LAB_YAML = """\
schema: softcloud.lab/v1
id: test-lab
title: Test Laboratory
runtime:
  os: freebsd
executor:
  type: jail
notebook: notebooks/Test.ipynb
"""


def make_service(tmp_path: Path, **kwargs: Any) -> LabService:
    (tmp_path / "lab.yaml").write_text(LAB_YAML, encoding="utf-8")
    return LabService(
        root_dir=tmp_path,
        lab_path="lab.yaml",
        evidence_dir=".evidence",
        **kwargs,
    )


def test_progression_is_derived_from_recorded_events(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    initial = service.state()
    assert initial["evidence"]["events"] == 0
    assert not any(stage["completed"] for stage in initial["stages"])

    service.record_client_event(
        "notebook-context",
        {"notebook": "Test.ipynb", "markdown_cells": 2, "code_cells": 1},
    )
    service.record_client_event(
        "cell-executed",
        {"notebook": "Test.ipynb", "cell_id": "abc", "success": True},
    )

    state = service.state()
    completed = {stage["id"] for stage in state["stages"] if stage["completed"]}
    assert completed == {"observed", "explained"}
    assert state["evidence"]["events"] == 2
    assert state["evidence"]["attestation"] == "self-recorded"


def test_client_cannot_assert_machine_trust_stages(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    with pytest.raises(ValueError):
        service.record_client_event("verification-complete", {"passed": True})


def test_sensitive_payload_keys_are_redacted_before_hash_and_persistence(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)

    event = service.record_client_event(
        "cell-executed",
        {
            "notebook": "Test.ipynb",
            "authorization": "Bearer secret",
            "nested": {"api_token": "secret", "safe": "retained"},
        },
    )

    assert event.payload["authorization"] == "[REDACTED]"
    assert event.payload["nested"] == {
        "api_token": "[REDACTED]",
        "safe": "retained",
    }
    persisted = service.events_file.read_text(encoding="utf-8")
    assert "Bearer secret" not in persisted
    assert '"api_token":"[REDACTED]"' in persisted


def test_event_payload_size_is_bounded_after_redaction(tmp_path: Path) -> None:
    service = make_service(tmp_path, max_event_payload_bytes=1024)

    with pytest.raises(EvidencePayloadTooLarge):
        service.record_client_event("cell-executed", {"output": "x" * 2048})

    assert service.state()["evidence"]["events"] == 0


def test_event_count_is_bounded(tmp_path: Path) -> None:
    service = make_service(tmp_path, max_events=1)
    service.record_client_event("cell-executed", {"cell_id": "first"})

    with pytest.raises(EvidenceEventLimitReached):
        service.record_client_event("cell-executed", {"cell_id": "second"})

    assert service.state()["evidence"]["events"] == 1


def test_event_append_is_fsynced_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(service_module.os, "fsync", lambda fd: calls.append(fd))
    service = make_service(tmp_path, fsync_events=True)

    service.record_client_event("cell-executed", {"cell_id": "abc"})

    assert calls


def test_event_append_can_disable_fsync_explicitly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(service_module.os, "fsync", lambda fd: calls.append(fd))
    service = make_service(tmp_path, fsync_events=False)

    service.record_client_event("cell-executed", {"cell_id": "abc"})

    assert calls == []


def test_export_writes_verifiable_checksums(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.record_client_event(
        "cell-executed",
        {"notebook": "Test.ipynb", "cell_id": "abc", "success": True},
    )

    result = service.export()
    export_dir = Path(result["path"])
    sums = (export_dir / "SHA256SUMS").read_text(encoding="utf-8").splitlines()

    assert (export_dir / "evidence.json").is_file()
    assert (export_dir / "environment.json").is_file()
    assert (export_dir / "events.jsonl").is_file()
    assert (export_dir / "manifest.json").is_file()

    evidence = json.loads((export_dir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["events"][0]["payload"]["cell_id"] == "abc"

    for line in sums:
        expected, filename = line.split("  ", maxsplit=1)
        actual = hashlib.sha256((export_dir / filename).read_bytes()).hexdigest()
        assert actual == expected
