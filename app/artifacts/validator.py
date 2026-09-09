"""Robustness lints for artifacts.

Hard schema/semantic violations are rejected by the models themselves (``schema.py``). This
module reports *soft* findings a reviewer should know about before approving a capability for
unattended replay: brittle targets, missing checkpoints, unbounded steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.artifacts.schema import (
    SEMANTIC_STRATEGIES,
    ActionType,
    CapabilityArtifact,
    RiskClass,
    Step,
    Strategy,
    TargetSpec,
)


class Severity(StrEnum):
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    location: str
    message: str

    def render(self) -> str:
        return f"[{self.severity.upper()}] {self.location}: {self.message}"


def lint_artifact(artifact: CapabilityArtifact) -> list[Finding]:
    findings: list[Finding] = []
    for step in artifact.steps:
        findings.extend(_lint_step(step))
    if not any(s.action is ActionType.EXTRACT for s in artifact.steps):
        findings.append(
            Finding(Severity.INFO, "steps", "capability extracts no values (navigation-only)")
        )
    if not artifact.conditions:
        findings.append(
            Finding(
                Severity.WARNING,
                "conditions",
                "no runtime-condition rules: business outcomes will surface as checkpoint failures",
            )
        )
    return findings


def _lint_step(step: Step) -> list[Finding]:
    findings: list[Finding] = []
    where = f"step {step.id}"
    if step.action is not ActionType.EXTRACT and not step.postconditions:
        findings.append(
            Finding(Severity.WARNING, where, "no postcondition; success would be assumed")
        )
    if step.target is not None:
        findings.extend(_lint_target(step.target, where))
    if step.risk_class is RiskClass.IRREVERSIBLE and step.retry_policy.max_attempts > 1:
        findings.append(
            Finding(Severity.WARNING, where, "irreversible step has retries > 1 (double-submit)")
        )
    return findings


def _lint_target(target: TargetSpec, where: str) -> list[Finding]:
    findings: list[Finding] = []
    strategies = set(target.available_strategies())
    if not strategies & SEMANTIC_STRATEGIES:
        findings.append(
            Finding(
                Severity.WARNING,
                where,
                "target has only structural strategies (css/xpath/coordinates); brittle",
            )
        )
    if Strategy.COORDINATES in strategies:
        findings.append(Finding(Severity.INFO, where, "target carries a coordinate fallback"))
    if target.frame is not None:
        findings.append(
            Finding(Severity.WARNING, where, "target uses frames, unsupported by the v1 resolver")
        )
    for fallback in target.fallbacks:
        findings.extend(_lint_target(fallback, f"{where} (fallback)"))
    return findings
