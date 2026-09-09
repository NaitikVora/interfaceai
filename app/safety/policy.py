"""Policy engine: allowlists, action permissions and risk classification.

The engine is deterministic and runs *before* every action in both discovery and replay. The
LLM (or an artifact) may propose a risk class, but it can only raise the classification, never
lower it. Nothing in model output can bypass a decision made here.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.artifacts.schema import ActionType, RiskClass
from app.automation.actions import ActionRequest

RISK_ORDER: dict[RiskClass, int] = {
    RiskClass.SAFE: 0,
    RiskClass.SENSITIVE: 1,
    RiskClass.IRREVERSIBLE: 2,
}


class ViolationCode(StrEnum):
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    URL_NOT_ALLOWED = "URL_NOT_ALLOWED"
    SENSITIVE_BLOCKED = "SENSITIVE_BLOCKED"
    IRREVERSIBLE_BLOCKED = "IRREVERSIBLE_BLOCKED"
    SECRET_TO_UNMASKED_FIELD = "SECRET_TO_UNMASKED_FIELD"


class RiskRule(BaseModel):
    """Declarative classification rule. All present criteria must match."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    risk_class: RiskClass
    actions: list[ActionType] | None = None
    target_name_pattern: str | None = Field(
        default=None, description="Regex over the target's name/label/text/description"
    )
    target_attribute_type: str | None = Field(
        default=None, description="Matches the target's `type` attribute, e.g. password"
    )
    url_pattern: str | None = Field(default=None, description="Regex over the current URL")


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    allowed_url_patterns: list[str] = Field(min_length=1)
    denied_url_patterns: list[str] = Field(default_factory=list)
    allowed_actions: list[ActionType] = Field(min_length=1)
    sensitive_actions: Literal["allow", "block"] = "block"
    irreversible_actions: Literal["require_confirmation", "block"] = "require_confirmation"
    coordinate_fallback: Literal["never", "safe_only"] = "never"
    secrets_only_into_masked_fields: bool = True
    risk_rules: list[RiskRule] = Field(default_factory=list)


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    risk_class: RiskClass
    requires_confirmation: bool = False
    reason: str
    rule_id: str | None = None
    violation: ViolationCode | None = None

    def as_event_data(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude_none=True)


class PolicyLoadError(ValueError):
    pass


class PolicyEngine:
    def __init__(self, config: PolicyConfig) -> None:
        self.config = config
        self._allowed = [re.compile(p) for p in config.allowed_url_patterns]
        self._denied = [re.compile(p) for p in config.denied_url_patterns]
        self._rules = [
            (
                rule,
                re.compile(rule.target_name_pattern) if rule.target_name_pattern else None,
                re.compile(rule.url_pattern) if rule.url_pattern else None,
            )
            for rule in config.risk_rules
        ]

    @classmethod
    def from_file(cls, path: Path) -> PolicyEngine:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(PolicyConfig.model_validate(raw))
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise PolicyLoadError(f"cannot load policy {path}: {exc}") from exc

    # ------------------------------------------------------------------ URL allowlist
    def url_allowed(self, url: str) -> bool:
        if any(p.search(url) for p in self._denied):
            return False
        return any(p.fullmatch(url) for p in self._allowed)

    # ------------------------------------------------------------------ risk
    def classify(self, request: ActionRequest, current_url: str) -> tuple[RiskClass, str | None]:
        """Highest risk class among matching rules; SAFE when no rule matches."""
        best: tuple[RiskClass, str | None] = (RiskClass.SAFE, None)
        haystack = _target_haystack(request)
        target_type = (request.target.attributes or {}).get("type") if request.target else None
        for rule, name_re, url_re in self._rules:
            if rule.actions is not None and request.action not in rule.actions:
                continue
            if name_re is not None and not name_re.search(haystack):
                continue
            if rule.target_attribute_type is not None and target_type != rule.target_attribute_type:
                continue
            if url_re is not None and not url_re.search(current_url):
                continue
            if RISK_ORDER[rule.risk_class] > RISK_ORDER[best[0]]:
                best = (rule.risk_class, rule.id)
        return best

    # ------------------------------------------------------------------ decision
    def evaluate(
        self,
        request: ActionRequest,
        *,
        current_url: str,
        proposed_risk: RiskClass | None = None,
        value_is_secret: bool = False,
    ) -> PolicyDecision:
        if request.action not in self.config.allowed_actions:
            return PolicyDecision(
                allowed=False,
                risk_class=RiskClass.SAFE,
                reason=f"action {request.action} is not in the allowed action list",
                violation=ViolationCode.ACTION_NOT_ALLOWED,
            )

        checked_url = request.url if request.action is ActionType.NAVIGATE else current_url
        if not self.url_allowed(checked_url or ""):
            return PolicyDecision(
                allowed=False,
                risk_class=RiskClass.SAFE,
                reason=f"URL {checked_url!r} is outside the allowlist",
                violation=ViolationCode.URL_NOT_ALLOWED,
            )

        if value_is_secret and self.config.secrets_only_into_masked_fields:
            target_type = (request.target.attributes or {}).get("type") if request.target else None
            if request.action is ActionType.TYPE and target_type != "password":
                return PolicyDecision(
                    allowed=False,
                    risk_class=RiskClass.SENSITIVE,
                    reason="a secret input may only be typed into a masked (password) field",
                    violation=ViolationCode.SECRET_TO_UNMASKED_FIELD,
                )

        risk, rule_id = self.classify(request, current_url)
        if proposed_risk is not None and RISK_ORDER[proposed_risk] > RISK_ORDER[risk]:
            risk, rule_id = proposed_risk, rule_id or "proposed"

        if risk is RiskClass.SENSITIVE and self.config.sensitive_actions == "block":
            return PolicyDecision(
                allowed=False,
                risk_class=risk,
                reason="sensitive actions are not permitted by this policy",
                rule_id=rule_id,
                violation=ViolationCode.SENSITIVE_BLOCKED,
            )
        if risk is RiskClass.IRREVERSIBLE:
            if self.config.irreversible_actions == "block":
                return PolicyDecision(
                    allowed=False,
                    risk_class=risk,
                    reason="irreversible actions are blocked by this policy",
                    rule_id=rule_id,
                    violation=ViolationCode.IRREVERSIBLE_BLOCKED,
                )
            return PolicyDecision(
                allowed=True,
                risk_class=risk,
                requires_confirmation=True,
                reason="irreversible action requires human confirmation",
                rule_id=rule_id,
            )
        return PolicyDecision(
            allowed=True,
            risk_class=risk,
            reason=f"{risk} action permitted",
            rule_id=rule_id,
        )

    def coordinates_allowed(self, risk: RiskClass) -> bool:
        return self.config.coordinate_fallback == "safe_only" and risk is RiskClass.SAFE


def _target_haystack(request: ActionRequest) -> str:
    target = request.target
    if target is None:
        return ""
    parts = [target.name, target.label, target.text, target.description]
    if target.attributes:
        parts.append(target.attributes.get("value"))
    return " | ".join(p for p in parts if p)
