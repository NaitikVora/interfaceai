"""Replay executor against the live demo app: success, business outcomes, recoveries, failures.

Every scenario also proves the replay path never touched an LLM (``llm_calls == 0`` and no
LLM events) and produced evidence.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from app.config import Settings
from app.orchestration import run_replay
from app.replay.errors import ReplayResult, ReplayStatus

from tests.conftest import SUPERVISOR, TELLER, DemoServer
from tests.fixtures.artifacts import savings_lookup_artifact, subaccount_artifact

pytestmark = pytest.mark.browser


def lookup_inputs(member_id: str = "12345", **overrides: str) -> dict[str, str]:
    return {"member_id": member_id, **TELLER, **overrides}


def events_of(result: ReplayResult) -> list[dict]:
    path = Path(result.evidence_dir) / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def assert_no_llm(result: ReplayResult) -> None:
    assert result.llm_calls == 0
    types = {e["event_type"] for e in events_of(result)}
    assert "REPLAY_STARTED" in types and not types & {"LLM_DECISION", "LLM_RESPONSE_REJECTED"}
    assert (Path(result.evidence_dir) / "result.json").exists()


async def replay(settings: Settings, demo: DemoServer, artifact, inputs, **kwargs) -> ReplayResult:
    result = await run_replay(
        settings=settings,
        artifact=artifact,
        inputs=inputs,
        attended=False,
        base_url=demo.base_url,
        console_logging=False,
        **kwargs,
    )
    assert_no_llm(result)
    return result


async def test_success_returns_typed_outputs(settings: Settings, demo: DemoServer) -> None:
    result = await replay(settings, demo, savings_lookup_artifact(demo.base_url), lookup_inputs())
    assert result.status is ReplayStatus.SUCCESS
    assert result.outputs == {
        "status": "success",
        "member_id": "12345",
        "savings_balance": Decimal("8432.17"),
        "message": None,
    }
    assert result.steps_completed == 8 and result.failure is None
    assert result.strategies_used["s08-extract-savings"] == "table_cell"
    assert result.strategies_used["s05-open-search"] == "role_name"
    assert result.drift_signals == []
    events = events_of(result)
    assert not any("teller-pass" in json.dumps(e) for e in events)
    assert sum(1 for e in events if e["event_type"] == "CHECKPOINT_VERIFIED") >= 8


async def test_member_not_found_is_a_business_outcome_not_a_failure(settings, demo) -> None:
    result = await replay(
        settings, demo, savings_lookup_artifact(demo.base_url), lookup_inputs("99999")
    )
    assert result.status is ReplayStatus.BUSINESS_OUTCOME
    assert result.failure is not None and result.failure.code == "MEMBER_NOT_FOUND"
    assert result.failure.category.value == "business_outcome"
    assert result.outputs["status"] == "not_found" and result.outputs["savings_balance"] is None
    assert result.outputs["message"] == "No member found matching member number 99999."
    assert result.step_id == "s07-search" and result.failure.screenshot_ref
    assert result.duration_s < 4  # recognized during the wait, not after the timeout


async def test_invalid_input_is_rejected_before_any_browser_action(settings, demo) -> None:
    result = await replay(
        settings, demo, savings_lookup_artifact(demo.base_url), lookup_inputs("abc")
    )
    assert result.status is ReplayStatus.HARD_FAILURE and result.failure.code == "INVALID_INPUT"
    assert result.steps_completed == 0
    assert not any(e["event_type"] == "ACTION_EXECUTED" for e in events_of(result))
    result = await replay(
        settings, demo, savings_lookup_artifact(demo.base_url), {**lookup_inputs(), "extra": "x"}
    )
    assert result.failure.code == "INVALID_INPUT" and "unexpected input" in result.failure.message


async def test_transient_outage_is_recovered(settings, demo) -> None:
    await demo.inject(transient_search_failures=1)
    result = await replay(settings, demo, savings_lookup_artifact(demo.base_url), lookup_inputs())
    assert result.status is ReplayStatus.SUCCESS and result.recoveries_applied == 1
    events = events_of(result)
    recovery = next(e for e in events if e["event_type"] == "RECOVERY_APPLIED")
    assert recovery["data"]["rule"] == "core-transient" and recovery["step_id"] == "s07-search"


async def test_recovery_budget_is_bounded(settings, demo) -> None:
    await demo.inject(transient_search_failures=5)
    result = await replay(settings, demo, savings_lookup_artifact(demo.base_url), lookup_inputs())
    assert result.status is ReplayStatus.RECOVERABLE_EXHAUSTED
    assert (
        result.failure.code == "TRANSIENT_LOAD" and result.failure.category.value == "recoverable"
    )
    assert result.recoveries_applied == 2 and result.failure.screenshot_ref


@pytest.mark.parametrize("interstitial", ["system_notice", "session_refresh"])
async def test_known_dialogs_are_dismissed(settings, demo, interstitial: str) -> None:
    await demo.inject(interstitial=interstitial)
    result = await replay(settings, demo, savings_lookup_artifact(demo.base_url), lookup_inputs())
    assert result.status is ReplayStatus.SUCCESS and result.recoveries_applied == 1
    recovery = next(e for e in events_of(result) if e["event_type"] == "RECOVERY_APPLIED")
    assert recovery["data"]["recovery"] == "click"


async def test_session_loss_is_a_hard_failure(settings, demo) -> None:
    await demo.inject(expire_session_on_next_request=True)
    result = await replay(settings, demo, savings_lookup_artifact(demo.base_url), lookup_inputs())
    assert result.status is ReplayStatus.HARD_FAILURE and result.failure.code == "SESSION_LOST"


async def test_application_error_is_a_hard_failure_with_evidence(settings, demo) -> None:
    await demo.inject(app_error_on_search=True)
    result = await replay(
        settings,
        demo,
        savings_lookup_artifact(demo.base_url),
        lookup_inputs(),
        evidence_kind="failure",
    )
    assert result.status is ReplayStatus.HARD_FAILURE and result.failure.code == "UNEXPECTED_STATE"
    assert "Application Error" in (result.failure.observed or "")
    evidence = Path(result.evidence_dir)
    assert evidence.parent.name == "failure"
    assert (evidence / result.failure.screenshot_ref).read_bytes().startswith(b"\x89PNG")
    assert any(p.suffix == ".txt" for p in evidence.iterdir())


async def test_bad_credentials_are_classified(settings, demo) -> None:
    result = await replay(
        settings,
        demo,
        savings_lookup_artifact(demo.base_url),
        lookup_inputs(access_code="wrong-pass"),
    )
    assert (
        result.status is ReplayStatus.HARD_FAILURE
        and result.failure.code == "AUTHENTICATION_FAILED"
    )
    assert not any("wrong-pass" in json.dumps(e) for e in events_of(result))


async def test_ambiguous_target_escalates_instead_of_guessing(settings, demo) -> None:
    await demo.inject(duplicate_search_form=True)
    result = await replay(settings, demo, savings_lookup_artifact(demo.base_url), lookup_inputs())
    assert result.status is ReplayStatus.ESCALATED and result.failure.code == "AMBIGUOUS_TARGET"
    assert result.failure.details["escalation"] == "NO_OPERATOR"
    assert result.human_interventions == 1 and result.step_id == "s06-type-member"
    diagnostics = result.failure.details["locator_diagnostics"]["attempts"]
    assert any(a["detail"] == "ambiguous" for a in diagnostics)
    assert not any(
        e["event_type"] == "ACTION_EXECUTED" and e["step_id"] == "s06-type-member"
        for e in events_of(result)
    )
    intervention = json.loads((Path(result.evidence_dir) / "int-001.json").read_text())
    assert intervention["status"] == "unattended" and intervention["kind"] == "target_unresolved"


async def test_slow_application_is_handled_by_state_waits(settings, demo) -> None:
    await demo.inject(response_delay_ms=1200)
    result = await replay(settings, demo, savings_lookup_artifact(demo.base_url), lookup_inputs())
    assert result.status is ReplayStatus.SUCCESS and result.recoveries_applied == 0


async def test_permission_denied_is_a_business_outcome(settings, demo) -> None:
    artifact = subaccount_artifact(demo.base_url)
    result = await replay(
        settings, demo, artifact, {"member_id": "12345", "nickname": "Emergency Fund", **TELLER}
    )
    assert result.status is ReplayStatus.BUSINESS_OUTCOME
    assert result.failure.code == "INSUFFICIENT_PERMISSION"
    assert (
        result.outputs["status"] == "permission_denied"
        and result.outputs["confirmation_id"] is None
    )


async def test_validation_rejection_is_a_business_outcome(settings, demo) -> None:
    artifact = subaccount_artifact(demo.base_url)
    result = await replay(
        settings, demo, artifact, {"member_id": "12345", "nickname": "x" * 25, **SUPERVISOR}
    )
    assert result.status is ReplayStatus.BUSINESS_OUTCOME
    assert result.failure.code == "VALIDATION_REJECTED"
    assert "20 characters" in result.outputs["message"]


async def test_irreversible_step_without_operator_is_escalated(settings, demo) -> None:
    artifact = subaccount_artifact(demo.base_url)
    result = await replay(
        settings, demo, artifact, {"member_id": "12345", "nickname": "Emergency Fund", **SUPERVISOR}
    )
    assert result.status is ReplayStatus.ESCALATED and result.step_id == "s11-confirm"
    assert result.failure.details["escalation"] == "NO_OPERATOR"
    intervention = json.loads((Path(result.evidence_dir) / "int-001.json").read_text())
    assert intervention["kind"] == "approval_required"
    assert intervention["pending_action"] == "click button 'Confirm and Open Account'"
    assert demo.state.created_accounts == []  # nothing was committed


async def test_policy_incompatible_artifact_is_refused_up_front(settings, demo, tmp_path) -> None:
    strict = tmp_path / "strict.json"
    strict.write_text(
        json.dumps(
            {
                "name": "strict",
                "allowed_url_patterns": [r"^http://(localhost|127\.0\.0\.1)(:\d+)?(/.*)?$"],
                "allowed_actions": ["navigate", "click", "type", "select", "press", "extract"],
                "sensitive_actions": "block",
                "risk_rules": [],
            }
        )
    )
    strict_settings = settings.model_copy(update={"policy_file": strict})
    result = await replay(
        strict_settings, demo, savings_lookup_artifact(demo.base_url), lookup_inputs()
    )
    assert result.status is ReplayStatus.HARD_FAILURE
    assert result.failure.code == "ARTIFACT_INCOMPATIBLE" and result.steps_completed == 0
