"""Normalized action requests and their execution on a surface.

``ActionRequest`` is the single representation of "do X to Y" shared by discovery (from an LLM
decision), replay (from an artifact step) and human operators (from the console). Values are
concrete here; parameter substitution happens before an ``ActionRequest`` is built.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, model_validator

from app.artifacts.schema import ActionType, Strategy, TargetSpec, ValueType
from app.automation.locators import ResolvedTarget
from app.automation.surface import ComputerSurface

TARGET_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.EXTRACT}
)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ActionType
    target: TargetSpec | None = None
    value: str | None = None
    key: str | None = None
    url: str | None = None
    output: str | None = None
    value_type: ValueType | None = None
    clear_first: bool = True

    @model_validator(mode="after")
    def _consistent(self) -> ActionRequest:
        if self.action in TARGET_ACTIONS and self.target is None:
            raise ValueError(f"{self.action} requires a target")
        if self.action in {ActionType.TYPE, ActionType.SELECT} and self.value is None:
            raise ValueError(f"{self.action} requires a value")
        if self.action is ActionType.PRESS and not self.key:
            raise ValueError("press requires a key")
        if self.action is ActionType.NAVIGATE and not self.url:
            raise ValueError("navigate requires a url")
        if self.action is ActionType.EXTRACT and (not self.output or self.value_type is None):
            raise ValueError("extract requires output and value_type")
        return self

    @property
    def needs_target(self) -> bool:
        return self.action in TARGET_ACTIONS

    @property
    def requires_enabled_target(self) -> bool:
        return self.action is not ActionType.EXTRACT


@dataclass(frozen=True)
class ActionOutcome:
    extracted_text: str | None = None
    strategy: Strategy | None = None
    via_coordinates: bool = False


async def perform_action(
    surface: ComputerSurface, request: ActionRequest, resolved: ResolvedTarget | None
) -> ActionOutcome:
    """Execute one request. Callers resolve targets first so diagnostics stay with them."""
    if request.needs_target and resolved is None:
        raise ValueError(f"{request.action} needs a resolved target")

    match request.action:
        case ActionType.NAVIGATE:
            await surface.navigate(request.url or "")
            return ActionOutcome()
        case ActionType.PRESS:
            await surface.press(request.key or "")
            return ActionOutcome()
        case ActionType.CLICK:
            assert resolved is not None
            if resolved.element is None and resolved.point is not None:
                await surface.click_at(resolved.point.x, resolved.point.y)
                return ActionOutcome(strategy=resolved.strategy, via_coordinates=True)
            assert resolved.element is not None
            await surface.click(resolved.element)
            return ActionOutcome(strategy=resolved.strategy)
        case ActionType.TYPE:
            assert resolved is not None and resolved.element is not None
            await surface.type_text(
                resolved.element, request.value or "", clear=request.clear_first
            )
            return ActionOutcome(strategy=resolved.strategy)
        case ActionType.SELECT:
            assert resolved is not None and resolved.element is not None
            await surface.select_option(resolved.element, request.value or "")
            return ActionOutcome(strategy=resolved.strategy)
        case ActionType.EXTRACT:
            assert resolved is not None and resolved.element is not None
            text = await surface.read_text(resolved.element)
            return ActionOutcome(extracted_text=text, strategy=resolved.strategy)
    raise ValueError(f"unsupported action {request.action}")  # pragma: no cover
