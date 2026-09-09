"""Error taxonomy and the replay result contract.

Three categories, deliberately distinct (see REPORT.md section 3):

* **business_outcome** — a legitimate answer the caller needs ("no such member"). Not an error.
* **recoverable** — a runtime condition replay handles under policy (dismiss a known notice,
  retry a transient core outage). Surfaces as ``RECOVERABLE_EXHAUSTED`` only when the bounded
  recovery budget is spent.
* **hard_failure** — stop and report exactly what step, what was expected and what was observed.

Business and recoverable codes are defined by the artifact's condition rules. Hard-failure codes
are system-defined below.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.artifacts.schema import OutcomeCategory


class HardFailureCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    ARTIFACT_INCOMPATIBLE = "ARTIFACT_INCOMPATIBLE"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    UNSUPPORTED_TARGET = "UNSUPPORTED_TARGET"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    ACTION_FAILED = "ACTION_FAILED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    UNEXPECTED_STATE = "UNEXPECTED_STATE"
    SESSION_LOST = "SESSION_LOST"
    REPLAY_TIMEOUT = "REPLAY_TIMEOUT"


class EscalationCode(StrEnum):
    """Why a run ended with status ESCALATED instead of completing."""

    NO_OPERATOR = "NO_OPERATOR"
    INTERVENTION_TIMEOUT = "INTERVENTION_TIMEOUT"
    ABORTED_BY_OPERATOR = "ABORTED_BY_OPERATOR"
    UNRESOLVED_AFTER_HANDOFF = "UNRESOLVED_AFTER_HANDOFF"


class ReplayStatus(StrEnum):
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    RECOVERABLE_EXHAUSTED = "RECOVERABLE_EXHAUSTED"
    HARD_FAILURE = "HARD_FAILURE"
    ESCALATED = "ESCALATED"


class Failure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: OutcomeCategory
    code: str
    message: str
    expected: str | None = None
    observed: str | None = None
    step_id: str | None = None
    screenshot_ref: str | None = None
    timestamp: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class ReplayResult(BaseModel):
    """What the calling agent receives. Exactly one of ``outputs``/``failure`` is primary."""

    model_config = ConfigDict(extra="forbid")

    status: ReplayStatus
    run_id: str
    capability_id: str
    capability_name: str
    capability_version: int
    outputs: dict[str, Any] = Field(default_factory=dict)
    failure: Failure | None = None
    step_id: str | None = Field(default=None, description="Step at which the run stopped")
    steps_completed: int = 0
    steps_total: int = 0
    evidence_dir: str
    evidence_refs: list[str] = Field(default_factory=list)
    strategies_used: dict[str, str] = Field(
        default_factory=dict, description="step id -> locator strategy that resolved the target"
    )
    drift_signals: list[str] = Field(
        default_factory=list,
        description="Steps whose semantic strategies failed and a structural fallback was used",
    )
    recoveries_applied: int = 0
    human_interventions: int = 0
    llm_calls: int = Field(default=0, description="Always 0 for replay; asserted by tests")
    started_at: datetime
    finished_at: datetime

    @property
    def duration_s(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def render(self) -> str:
        lines = [f"{self.status.value}"]
        if self.failure is not None:
            f = self.failure
            lines.append(f"  {f.category.value} / {f.code}: {f.message}")
            if f.expected:
                lines.append(f"  expected: {f.expected}")
            if f.observed:
                lines.append(f"  observed: {f.observed}")
            if f.step_id:
                lines.append(f"  step: {f.step_id}")
            if f.screenshot_ref:
                lines.append(f"  screenshot: {self.evidence_dir}/{f.screenshot_ref}")
        if self.outputs:
            lines.append("  outputs:")
            lines.extend(f"    {k}: {v!r}" for k, v in self.outputs.items())
        lines.append(
            f"  steps: {self.steps_completed}/{self.steps_total}  "
            f"recoveries: {self.recoveries_applied}  interventions: {self.human_interventions}  "
            f"llm_calls: {self.llm_calls}  duration: {self.duration_s:.1f}s"
        )
        lines.append(f"  evidence: {self.evidence_dir}")
        return "\n".join(lines)


class ReplayAbortError(Exception):
    """Internal control flow: unwinds step execution with a terminal status."""

    def __init__(
        self, status: ReplayStatus, failure: Failure, outputs: dict[str, Any] | None = None
    ) -> None:
        self.status = status
        self.failure = failure
        self.outputs = outputs or {}
        super().__init__(f"{status}: {failure.code}")
