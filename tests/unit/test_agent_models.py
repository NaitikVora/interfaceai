"""LLM decision contract, response parsing and observation rendering."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.agent.models import AgentAction, ControlRef, Decision, TableCellRef
from app.agent.observation import AgentContext, render_observation
from app.agent.planner import parse_decision
from app.automation.surface import (
    ContainerDescriptor,
    ControlDescriptor,
    Observation,
    TableDescriptor,
)
from app.safety.redaction import Redactor


def test_decision_validation_rules() -> None:
    Decision(reasoning="r", action=AgentAction.CLICK, target=ControlRef(ref="c1"), confidence=0.9)
    with pytest.raises(ValueError, match="requires a target"):
        Decision(reasoning="r", action=AgentAction.CLICK, confidence=0.9)
    with pytest.raises(ValueError, match="must not have a target"):
        Decision(
            reasoning="r", action=AgentAction.FINISH, target=ControlRef(ref="c1"), confidence=1
        )
    with pytest.raises(ValueError, match="requires a value"):
        Decision(reasoning="r", action=AgentAction.TYPE, target=ControlRef(ref="c1"), confidence=1)
    with pytest.raises(ValueError, match="output_name"):
        Decision(
            reasoning="r",
            action=AgentAction.EXTRACT,
            target=TableCellRef(row_match="Savings", column_header="Balance"),
            confidence=1,
        )
    with pytest.raises(ValueError, match="requires a reason"):
        Decision(reasoning="r", action=AgentAction.ESCALATE, confidence=1)
    with pytest.raises(ValueError, match="input"):
        Decision(reasoning="r", action=AgentAction.FINISH, outputs={"x": "literal"}, confidence=1)
    with pytest.raises(ValueError):
        Decision(reasoning="r", action=AgentAction.WAIT, confidence=1.5)
    with pytest.raises(ValueError):
        Decision(reasoning="r", action=AgentAction.WAIT, confidence=1, extra_field=1)  # type: ignore[call-arg]


def test_parse_decision_tolerates_fences_but_not_garbage() -> None:
    raw = '```json\n{"reasoning":"go","action":"press","key":"Enter","confidence":0.8}\n```'
    assert parse_decision(raw).action is AgentAction.PRESS
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_decision("click the button")
    with pytest.raises(ValueError, match="single JSON object"):
        parse_decision("[1,2]")
    with pytest.raises(ValueError, match="failed validation"):
        parse_decision('{"reasoning":"x","action":"fly","confidence":1}')


def _observation() -> Observation:
    return Observation(
        url="http://h/members/12345",
        title="Member Details",
        headings=["Member Details"],
        messages=["Welcome"],
        text="Member Number 12345 Name Demo Member acct 8801238765 code teller-pass",
        controls=[
            ControlDescriptor(
                ref="c1",
                tag="a",
                role="link",
                name="Member Search",
                attributes={"href": "/members/search"},
                container=ContainerDescriptor(tag="td", attributes={"class": "nav"}, css="td"),
                css="a",
                xpath="/a",
                bbox=(0, 0, 10, 10),
            ),
            ControlDescriptor(
                ref="c2",
                tag="input",
                role="textbox",
                name="Member Number",
                attributes={"name": "member_no", "current_value": "12345"},
                css="input",
                xpath="/input",
                bbox=(0, 0, 10, 10),
                enabled=False,
            ),
        ],
        tables=[
            TableDescriptor(
                ref="t1", headers=["A", "B"], rows=[["Savings", "$1.00"]], total_rows=1, css="table"
            )
        ],
        captured_at=datetime.now(UTC),
    )


def test_render_observation_is_bounded_and_redacted() -> None:
    ctx = AgentContext(
        step=3,
        max_steps=25,
        inputs={"member_id": "12345", "access_code": "teller-pass"},
        sensitive=frozenset({"access_code"}),
        extracted={"savings_balance": "8432.17"},
        history=["1. navigate x -> ok"],
        notes=["previous action rejected"],
    )
    text = render_observation(_observation(), ctx, Redactor(["teller-pass"]), max_chars=5000)
    assert (
        "STEP 3/25" in text and 'c1 link "Member Search" href=/members/search [in td.nav]' in text
    )
    assert 'c2 textbox "Member Number" name=member_no value="12345" (disabled)' in text
    assert "t1 (1 rows)" in text and "headers: A | B" in text and "r1: Savings | $1.00" in text
    assert 'access_code = <secret> (type it as "${access_code}")' in text
    assert 'member_id = "12345"' in text and "savings_balance = 8432.17" in text
    assert "teller-pass" not in text and "[REDACTED:account]" in text
    assert "! previous action rejected" in text
    short = render_observation(_observation(), ctx, Redactor(), max_chars=300)
    assert len(short) <= 300 and short.endswith("[observation truncated]")


def test_fingerprint_changes_when_form_values_change() -> None:
    obs = _observation()
    before = obs.fingerprint()
    obs.controls[1].attributes["current_value"] = "99999"
    assert obs.fingerprint() != before
