"""Policy engine: allowlists, action permissions, risk classification, model cannot override."""

from __future__ import annotations

import pytest
from app.artifacts.schema import ActionType, RiskClass, TargetSpec
from app.automation.actions import ActionRequest
from app.safety.policy import PolicyConfig, PolicyEngine, ViolationCode

BASE = "http://localhost:8000"


def click(name: str) -> ActionRequest:
    return ActionRequest(action=ActionType.CLICK, target=TargetSpec(role="button", name=name))


def type_into(kind: str) -> ActionRequest:
    return ActionRequest(
        action=ActionType.TYPE,
        target=TargetSpec(role="textbox", name="f", attributes={"tag": "input", "type": kind}),
        value="x",
    )


def test_url_allowlist_and_denylist(policy: PolicyEngine) -> None:
    assert policy.url_allowed(f"{BASE}/members/search")
    assert policy.url_allowed("http://127.0.0.1:9999/")
    assert not policy.url_allowed(f"{BASE}/__admin")
    assert not policy.url_allowed(f"{BASE}/__admin/inject")
    assert not policy.url_allowed("https://evil.example/localhost:8000/")
    assert not policy.url_allowed("http://localhost.evil.example/")


def test_navigation_outside_allowlist_is_a_violation(policy: PolicyEngine) -> None:
    decision = policy.evaluate(
        ActionRequest(action=ActionType.NAVIGATE, url="https://example.com"), current_url=BASE
    )
    assert not decision.allowed and decision.violation is ViolationCode.URL_NOT_ALLOWED


def test_actions_on_a_disallowed_page_are_violations(policy: PolicyEngine) -> None:
    decision = policy.evaluate(click("Apply"), current_url=f"{BASE}/__admin")
    assert decision.violation is ViolationCode.URL_NOT_ALLOWED


def test_irreversible_controls_require_confirmation(policy: PolicyEngine) -> None:
    decision = policy.evaluate(
        click("Confirm and Open Account"), current_url=f"{BASE}/members/1/subaccounts/review"
    )
    assert decision.allowed and decision.requires_confirmation
    assert decision.risk_class is RiskClass.IRREVERSIBLE and decision.rule_id == "commit-controls"
    # anything but Back/Cancel on a review screen commits the request
    decision = policy.evaluate(click("Submit"), current_url=f"{BASE}/members/1/subaccounts/review")
    assert decision.requires_confirmation
    assert not policy.evaluate(
        click("Cancel"), current_url=f"{BASE}/members/1/subaccounts/review"
    ).requires_confirmation


def test_safe_actions_pass_without_confirmation(policy: PolicyEngine) -> None:
    decision = policy.evaluate(click("Search"), current_url=f"{BASE}/members/search")
    assert decision.allowed and not decision.requires_confirmation
    assert decision.risk_class is RiskClass.SAFE


def test_credentials_are_sensitive_and_only_go_into_masked_fields(policy: PolicyEngine) -> None:
    ok = policy.evaluate(type_into("password"), current_url=f"{BASE}/login", value_is_secret=True)
    assert ok.allowed and ok.risk_class is RiskClass.SENSITIVE and ok.rule_id == "credentials"
    leak = policy.evaluate(type_into("text"), current_url=f"{BASE}/login", value_is_secret=True)
    assert not leak.allowed and leak.violation is ViolationCode.SECRET_TO_UNMASKED_FIELD


def test_model_can_raise_but_never_lower_risk(policy: PolicyEngine) -> None:
    raised = policy.evaluate(
        click("Search"), current_url=f"{BASE}/members/search", proposed_risk=RiskClass.IRREVERSIBLE
    )
    assert raised.requires_confirmation and raised.rule_id == "proposed"
    lowered = policy.evaluate(
        click("Confirm and Open Account"),
        current_url=f"{BASE}/members/1/subaccounts/review",
        proposed_risk=RiskClass.SAFE,
    )
    assert lowered.risk_class is RiskClass.IRREVERSIBLE and lowered.requires_confirmation


def test_restrictive_policy_modes() -> None:
    config = PolicyConfig(
        name="strict",
        allowed_url_patterns=[r"^http://localhost(:\d+)?(/.*)?$"],
        allowed_actions=[ActionType.CLICK, ActionType.NAVIGATE],
        sensitive_actions="block",
        irreversible_actions="block",
        risk_rules=[
            {
                "id": "confirm",
                "description": "",
                "risk_class": "irreversible",
                "target_name_pattern": "(?i)confirm",
            },
            {
                "id": "pw",
                "description": "",
                "risk_class": "sensitive",
                "target_attribute_type": "password",
            },
        ],
    )
    engine = PolicyEngine(config)
    assert engine.evaluate(type_into("text"), current_url=BASE).violation is (
        ViolationCode.ACTION_NOT_ALLOWED
    )
    blocked = engine.evaluate(click("Confirm"), current_url=BASE)
    assert blocked.violation is ViolationCode.IRREVERSIBLE_BLOCKED and not blocked.allowed
    permissive = PolicyEngine(
        config.model_copy(
            update={"allowed_actions": [ActionType.TYPE], "sensitive_actions": "block"}
        )
    )
    assert permissive.evaluate(type_into("password"), current_url=BASE).violation is (
        ViolationCode.SENSITIVE_BLOCKED
    )


def test_coordinate_fallback_gating(policy: PolicyEngine) -> None:
    assert policy.coordinates_allowed(RiskClass.SAFE) is False  # demo policy: never
    engine = PolicyEngine(policy.config.model_copy(update={"coordinate_fallback": "safe_only"}))
    assert engine.coordinates_allowed(RiskClass.SAFE) is True
    assert engine.coordinates_allowed(RiskClass.SENSITIVE) is False


def test_policy_file_errors_are_explicit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.safety.policy import PolicyLoadError

    bad = tmp_path / "p.json"
    bad.write_text('{"name": "x"}')
    with pytest.raises(PolicyLoadError):
        PolicyEngine.from_file(bad)
