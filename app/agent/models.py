"""The strict decision contract between the LLM and the agent loop.

The model chooses *which* observed control to act on (by ``ref``) and *what* to do; it never
writes selectors, never sees secret values, and cannot express anything outside this schema.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.schema import IDENTIFIER_RE, PLACEHOLDER_RE, ValueType


class AgentAction(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    PRESS = "press"
    WAIT = "wait"
    EXTRACT = "extract"
    FINISH = "finish"
    ESCALATE = "escalate"


class ControlRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["control"] = "control"
    ref: str = Field(pattern=r"^c\d{1,3}$", description="A control ref from the observation")


class TableCellRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["table_cell"] = "table_cell"
    table_ref: str | None = Field(default=None, pattern=r"^t\d{1,2}$")
    row_match: str = Field(min_length=1, description="Exact text of a cell in the wanted row")
    column_header: str | None = Field(default=None, description="Header text of the column")
    column_index: int | None = Field(default=None, ge=0, description="0-based, if no headers")

    @model_validator(mode="after")
    def _one_column_address(self) -> TableCellRef:
        if (self.column_header is None) == (self.column_index is None):
            raise ValueError("give exactly one of column_header or column_index")
        return self


DecisionTarget = ControlRef | TableCellRef

TARGET_ACTIONS = {AgentAction.CLICK, AgentAction.TYPE, AgentAction.SELECT, AgentAction.EXTRACT}
MAX_REASONING_CHARS = 500


class Decision(BaseModel):
    """One action per model response. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(max_length=MAX_REASONING_CHARS)
    action: AgentAction
    target: DecisionTarget | None = None
    value: str | None = Field(
        default=None, description="type/select: literal text or ${input_name} placeholder"
    )
    key: str | None = Field(default=None, description="press: e.g. Enter, Tab")
    url: str | None = Field(default=None, description="navigate: an absolute URL seen on the page")
    output_name: str | None = Field(default=None, pattern=IDENTIFIER_RE)
    output_type: ValueType | None = None
    expected_heading: str | None = Field(
        default=None, description="Heading you expect after the action; verified, never trusted"
    )
    outputs: dict[str, str] | None = Field(
        default=None, description="finish: extra outputs to echo, values must be ${input} refs"
    )
    summary: str | None = Field(default=None, description="finish: what was accomplished")
    reason: str | None = Field(default=None, description="escalate: why a human is needed")
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _consistent(self) -> Decision:
        a = self.action
        if a in TARGET_ACTIONS and self.target is None:
            raise ValueError(f"{a} requires a target")
        if a not in TARGET_ACTIONS and self.target is not None:
            raise ValueError(f"{a} must not have a target")
        if a is AgentAction.EXTRACT and not isinstance(self.target, TableCellRef | ControlRef):
            raise ValueError("extract needs a control or table_cell target")
        if a in {AgentAction.TYPE, AgentAction.SELECT} and not self.value:
            raise ValueError(f"{a} requires a value")
        if a is AgentAction.PRESS and not self.key:
            raise ValueError("press requires a key")
        if a is AgentAction.NAVIGATE and not self.url:
            raise ValueError("navigate requires a url")
        if a is AgentAction.EXTRACT and (self.output_name is None or self.output_type is None):
            raise ValueError("extract requires output_name and output_type")
        if a is AgentAction.ESCALATE and not self.reason:
            raise ValueError("escalate requires a reason")
        if self.outputs:
            for name, ref in self.outputs.items():
                if not PLACEHOLDER_RE.fullmatch(ref):
                    raise ValueError(f"outputs[{name!r}] must be a ${{input}} reference")
        return self
