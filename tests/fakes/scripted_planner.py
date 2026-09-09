"""A deterministic stand-in for the LLM.

It receives exactly what the real model receives (the rendered observation text) and answers
with ``Decision`` objects by pattern-matching control lines, which is how a competent model
behaves on this application. It exists so the discovery loop, recorder and artifact can be
tested without network access. The real provider path is ``app.agent.planner``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from app.agent.models import AgentAction, ControlRef, Decision, TableCellRef
from app.agent.planner import PlannerCall
from app.artifacts.schema import ValueType

_CONTROL_RE = re.compile(r'^\s+(c\d+) (\S+)(?: "([^"]*)")?(.*)$')


@dataclass(frozen=True)
class ObservedControl:
    ref: str
    role: str
    name: str
    rest: str


def parse_controls(observation_text: str) -> list[ObservedControl]:
    controls: list[ObservedControl] = []
    in_controls = False
    for line in observation_text.splitlines():
        if line.startswith("CONTROLS"):
            in_controls = True
            continue
        if in_controls:
            if not line.startswith("  "):
                in_controls = False
                continue
            if m := _CONTROL_RE.match(line):
                controls.append(ObservedControl(m.group(1), m.group(2), m.group(3) or "", m.group(4)))
    return controls


def heading_of(observation_text: str) -> str:
    for line in observation_text.splitlines():
        if line.startswith("HEADINGS: "):
            return line[len("HEADINGS: ") :].split(" | ")[0]
    return ""


def has_extracted(observation_text: str, name: str) -> bool:
    return f"  {name} = " in observation_text


def field_value(observation_text: str, ref: str) -> str | None:
    for control in parse_controls(observation_text):
        if control.ref == ref and (m := re.search(r'value="([^"]*)"', control.rest)):
            return m.group(1)
    return None


def find(controls: list[ObservedControl], role: str, name: str, within: str | None = None) -> str:
    for control in controls:
        if control.role == role and control.name == name and (within is None or within in control.rest):
            return control.ref
    raise LookupError(f"no {role} {name!r} in observation")


def _click(ref: str, why: str, heading: str | None = None) -> Decision:
    return Decision(reasoning=why, action=AgentAction.CLICK, target=ControlRef(ref=ref),
                    expected_heading=heading, confidence=0.9)


def _type(ref: str, value: str, why: str) -> Decision:
    return Decision(reasoning=why, action=AgentAction.TYPE, target=ControlRef(ref=ref),
                    value=value, confidence=0.9)


def savings_lookup_script(observation_text: str, _step: int) -> Decision:
    """Plays the 'look up member and read savings balance' goal."""
    controls = parse_controls(observation_text)
    heading = heading_of(observation_text)
    if heading == "Operator Sign In":
        op = find(controls, "textbox", "Operator ID:")
        code = find(controls, "textbox", "Access Code:")
        if not field_value(observation_text, op):
            return _type(op, "${operator_id}", "Enter the operator id from inputs")
        if "RECENT ACTIONS" not in observation_text or "${access_code}" not in observation_text.split("RECENT ACTIONS")[-1]:
            return _type(code, "${access_code}", "Enter the access code placeholder")
        return _click(find(controls, "button", "Sign In"), "Submit the sign-in form", "Main Menu")
    if heading == "Main Menu":
        return _click(find(controls, "link", "Member Search", within="table.grid"),
                      "Open member search from the menu", "Member Search")
    if heading == "Member Search":
        field = find(controls, "textbox", "Member Number")
        if field_value(observation_text, field) is None:
            return _type(field, "${member_id}", "Enter the member number")
        return _click(find(controls, "button", "Search"), "Run the search", "Member Details")
    if heading == "Member Details":
        if not has_extracted(observation_text, "savings_balance"):
            return Decision(
                reasoning="The savings balance is in the accounts table",
                action=AgentAction.EXTRACT,
                target=TableCellRef(table_ref="t2", row_match="Savings", column_header="Current Balance"),
                output_name="savings_balance",
                output_type=ValueType.DECIMAL,
                confidence=0.95,
            )
        return Decision(reasoning="Goal reached", action=AgentAction.FINISH,
                        summary="Looked up the member and read the current savings balance.",
                        outputs={"member_id": "${member_id}"}, confidence=0.99)
    return Decision(reasoning="Unexpected screen", action=AgentAction.ESCALATE,
                    reason=f"unknown screen {heading!r}", confidence=0.3)


def subaccount_script(observation_text: str, _step: int) -> Decision:
    """Plays the 'open a savings sub-account and reach the confirmation screen' goal."""
    controls = parse_controls(observation_text)
    heading = heading_of(observation_text)
    if heading in {"Operator Sign In", "Main Menu", "Member Search"}:
        return savings_lookup_script(observation_text, _step)
    if heading == "Member Details":
        return _click(find(controls, "link", "Open New Sub-Account"), "Start opening a sub-account",
                      "Open New Sub-Account")
    if heading == "Open New Sub-Account":
        nick = find(controls, "textbox", "")
        if field_value(observation_text, nick) is None:
            return _type(nick, "${nickname}", "Enter the requested nickname")
        return _click(find(controls, "button", "Continue"), "Continue to review", "Review Sub-Account Request")
    if heading == "Review Sub-Account Request":
        return _click(find(controls, "button", "Confirm and Open Account"), "Confirm the request",
                      "Sub-Account Opened")
    if heading == "Sub-Account Opened":
        if not has_extracted(observation_text, "confirmation_id"):
            return Decision(
                reasoning="Read the confirmation number",
                action=AgentAction.EXTRACT,
                target=TableCellRef(table_ref="t1", row_match="Confirmation Number", column_index=1),
                output_name="confirmation_id",
                output_type=ValueType.STRING,
                confidence=0.95,
            )
        return Decision(reasoning="Goal reached", action=AgentAction.FINISH,
                        summary="Opened a sub-account and reached the confirmation screen.",
                        outputs={"member_id": "${member_id}"}, confidence=0.99)
    return Decision(reasoning="Unexpected screen", action=AgentAction.ESCALATE,
                    reason=f"unknown screen {heading!r}", confidence=0.3)


class ScriptedPlanner:
    """Implements ``app.agent.planner.Planner`` without a model."""

    def __init__(self, script: Callable[[str, int], Decision], model_name: str = "scripted-test-double") -> None:
        self._script = script
        self._model_name = model_name
        self._calls: list[PlannerCall] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def calls(self) -> list[PlannerCall]:
        return self._calls

    async def decide(self, *, step: int, goal: str, observation_text: str) -> Decision:
        decision = self._script(observation_text, step)
        self._calls.append(
            PlannerCall(
                step=step, model=self._model_name, prompt_chars=len(goal) + len(observation_text),
                attempts=1, raw_response=decision.model_dump_json(exclude_none=True),
                decision=decision.model_dump(mode="json", exclude_none=True),
            )
        )
        return decision
