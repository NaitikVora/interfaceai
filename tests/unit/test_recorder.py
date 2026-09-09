"""Recorder: canonicalization, verified checkpoints, artifact assembly."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.agent.models import AgentAction, Decision
from app.agent.recorder import DiscoveryInput, Recorder
from app.artifacts.profile import ApplicationProfile
from app.artifacts.schema import (
    ActionType,
    ElementValue,
    HeadingPresent,
    OnError,
    RiskClass,
    TargetSpec,
    UrlMatches,
)
from app.automation.actions import ActionRequest
from app.automation.surface import Observation
from app.config import Settings

BASE = "http://localhost:8000"
PROFILE = Path(__file__).resolve().parents[2] / "profiles" / "demobank_legacycore.json"


def recorder() -> Recorder:
    return Recorder(
        inputs=[
            DiscoveryInput("member_id", "12345", False),
            DiscoveryInput("operator_id", "teller1", False),
            DiscoveryInput("access_code", "teller-pass", True),
            DiscoveryInput("pin", "12", False),
        ],
        base_url=BASE,
        profile=ApplicationProfile.load(PROFILE),
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
    )


def obs(url: str, *headings: str) -> Observation:
    return Observation(url=url, title="t", headings=list(headings), captured_at=datetime.now(UTC))


def test_canonicalize_replaces_values_longest_first_and_skips_short_ones() -> None:
    rec = recorder()
    assert rec.canonicalize("teller1 typed 12345 at http://localhost:8000/x") == (
        "${operator_id} typed ${member_id} at ${base_url}/x"
    )
    assert rec.canonicalize("pin 12 stays") == "pin 12 stays"  # 2 chars: too short
    assert rec.canonicalize("teller-pass") == "${access_code}"


def test_path_pattern_escapes_literals_and_keeps_placeholders() -> None:
    rec = recorder()
    assert rec.path_pattern(f"{BASE}/members/12345?tab=1") == "/members/${member_id}$"
    assert rec.path_pattern(f"{BASE}/a.b/c") == r"/a\.b/c$"


def test_page_identity_uses_only_stable_headings() -> None:
    rec = recorder()
    conds = rec.page_identity(obs(f"{BASE}/members/12345", "Member 12345 Details", "Accounts"))
    assert conds == [UrlMatches(pattern="/members/${member_id}$"), HeadingPresent(text="Accounts")]
    assert rec.page_identity(obs(f"{BASE}/x", "Ref 998877")) == [UrlMatches(pattern="/x$")]


def test_record_type_step_gets_value_postcondition_and_placeholder() -> None:
    rec = recorder()
    field = TargetSpec(role="textbox", name="Member Number")
    decision = Decision(
        reasoning="r",
        action=AgentAction.TYPE,
        target={"kind": "control", "ref": "c1"},
        value="${member_id}",
        confidence=1,
    )
    step = rec.record(
        index=6,
        decision=decision,
        request=ActionRequest(action=ActionType.TYPE, target=field, value="12345"),
        spec=field,
        before=obs(f"{BASE}/members/search", "Member Search"),
        after=obs(f"{BASE}/members/search", "Member Search"),
        risk=RiskClass.SAFE,
        extracted_text=None,
    )
    assert step.id == "s06-type-textbox-member-number"
    assert step.arguments.value == "${member_id}"
    assert step.postconditions == [ElementValue(target=field, value="${member_id}")]
    assert step.preconditions[0] == UrlMatches(pattern="/members/search$")
    assert step.on_error is OnError.ESCALATE and step.retry_policy.max_attempts == 2


def test_record_click_uses_verified_expected_heading_only() -> None:
    rec = recorder()
    button = TargetSpec(role="button", name="Search")
    decision = Decision(
        reasoning="r",
        action=AgentAction.CLICK,
        target={"kind": "control", "ref": "c2"},
        expected_heading="Member Details",
        confidence=1,
    )
    step = rec.record(
        index=7,
        decision=decision,
        request=ActionRequest(action=ActionType.CLICK, target=button),
        spec=button,
        before=obs(f"{BASE}/members/search", "Member Search"),
        after=obs(f"{BASE}/members/12345", "Member Details"),
        risk=RiskClass.SAFE,
        extracted_text=None,
    )
    assert step.postconditions == [
        UrlMatches(pattern="/members/${member_id}$"),
        HeadingPresent(text="Member Details"),
    ]
    wrong = decision.model_copy(update={"expected_heading": "Something Else"})
    step = rec.record(
        index=8,
        decision=wrong,
        request=ActionRequest(action=ActionType.CLICK, target=button),
        spec=button,
        before=obs(f"{BASE}/a", "A"),
        after=obs(f"{BASE}/b", "B"),
        risk=RiskClass.SAFE,
        extracted_text=None,
    )
    assert HeadingPresent(text="Something Else") not in step.postconditions


def test_build_artifact_declares_contract_and_drops_unused_inputs() -> None:
    rec = recorder()
    rec.record(
        index=1,
        decision=Decision(
            reasoning="r", action=AgentAction.NAVIGATE, url=f"{BASE}/login", confidence=1
        ),
        request=ActionRequest(action=ActionType.NAVIGATE, url=f"{BASE}/login"),
        spec=None,
        before=obs("about:blank"),
        after=obs(f"{BASE}/login", "Operator Sign In"),
        risk=RiskClass.SAFE,
        extracted_text=None,
    )
    field = TargetSpec(role="textbox", name="Member Number")
    rec.record(
        index=2,
        decision=Decision(
            reasoning="r",
            action=AgentAction.TYPE,
            target={"kind": "control", "ref": "c1"},
            value="${member_id}",
            confidence=1,
        ),
        request=ActionRequest(action=ActionType.TYPE, target=field, value="12345"),
        spec=field,
        before=obs(f"{BASE}/members/search", "Member Search"),
        after=obs(f"{BASE}/members/search", "Member Search"),
        risk=RiskClass.SAFE,
        extracted_text=None,
    )
    cell = TargetSpec(table_cell={"row_match": "Savings", "column_header": "Current Balance"})
    rec.record(
        index=3,
        decision=Decision(
            reasoning="r",
            action=AgentAction.EXTRACT,
            target={
                "kind": "table_cell",
                "row_match": "Savings",
                "column_header": "Current Balance",
            },
            output_name="savings_balance",
            output_type="decimal",
            confidence=1,
        ),
        request=ActionRequest(
            action=ActionType.EXTRACT, target=cell, output="savings_balance", value_type="decimal"
        ),
        spec=cell,
        before=obs(f"{BASE}/members/12345", "Member Details"),
        after=obs(f"{BASE}/members/12345", "Member Details"),
        risk=RiskClass.SAFE,
        extracted_text="$8,432.17",
    )
    artifact = rec.build_artifact(
        name="member_savings_lookup",
        goal="Look up member 12345",
        description="Read savings for 12345",
        run_id="run-1",
        llm_model="m",
        llm_decisions=3,
        duration_s=1.5,
        final_observation=obs(f"{BASE}/members/12345", "Member Details"),
        echo_outputs={"member_id": "${member_id}"},
    )
    assert set(artifact.inputs) == {"member_id"}
    assert artifact.inputs["member_id"].pattern == r"^\d+$"
    assert any("operator_id" in n for n in artifact.metadata.notes)
    assert artifact.metadata.goal == "Look up member ${member_id}"
    assert artifact.description == "Read savings for ${member_id}"
    assert artifact.outputs["savings_balance"].source.kind == "extract"
    assert artifact.outputs["member_id"].source.kind == "input"
    assert artifact.outputs["status"].enum and "not_found" in artifact.outputs["status"].enum
    assert artifact.checkpoint.conditions[0] == UrlMatches(pattern="/members/${member_id}$")
    assert artifact.policy.requires_human_confirmation is False
    assert artifact.target.entry_path == "/login"
    assert len(artifact.conditions) == 9  # profile rules embedded
