from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from freebsd_laboratory.service import LabService


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


def make_service(tmp_path: Path) -> LabService:
    (tmp_path / "lab.yaml").write_text(LAB_YAML, encoding="utf-8")
    return LabService(
        root_dir=tmp_path,
        lab_path="lab.yaml",
        evidence_dir=".evidence",
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

    for line in sums:
        expected, filename = line.split("  ", maxsplit=1)
        actual = hashlib.sha256((export_dir / filename).read_bytes()).hexdigest()
        assert actual == expected
