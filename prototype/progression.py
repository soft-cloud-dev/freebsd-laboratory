from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Stage(str, Enum):
    OBSERVED = "observed"
    EXPLAINED = "explained"
    REPRODUCED = "reproduced"
    MODIFIED = "modified"
    VERIFIED = "verified"
    RECOVERED = "recovered"
    DESIGNED = "designed"


@dataclass(frozen=True)
class LabState:
    evidence_events: int = 0
    explanation_present: bool = False
    reproduced: bool = False
    modified: bool = False
    assertions_passed: bool = False
    recovered: bool = False
    design_valid: bool = False


def completed_stages(state: LabState) -> set[Stage]:
    completed: set[Stage] = set()
    if state.evidence_events > 0:
        completed.add(Stage.OBSERVED)
    if state.explanation_present:
        completed.add(Stage.EXPLAINED)
    if state.reproduced:
        completed.add(Stage.REPRODUCED)
    if state.modified:
        completed.add(Stage.MODIFIED)
    if state.assertions_passed:
        completed.add(Stage.VERIFIED)
    if state.recovered:
        completed.add(Stage.RECOVERED)
    if state.design_valid:
        completed.add(Stage.DESIGNED)
    return completed
