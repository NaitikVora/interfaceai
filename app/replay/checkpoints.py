"""Condition evaluation and state-based waiting for checkpoints and pre/postconditions."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.artifacts.params import substitute
from app.artifacts.schema import (
    AllOf,
    AnyOf,
    Condition,
    ElementEnabled,
    ElementHidden,
    ElementValue,
    ElementVisible,
    HeadingPresent,
    Not,
    TargetSpec,
    TextAbsent,
    TextPresent,
    UrlMatches,
    describe_condition,
)
from app.automation.locators import LocatorResolver, ResolutionError
from app.automation.surface import ComputerSurface
from app.automation.waits import wait_until

ELEMENT_PROBE_TIMEOUT_S = 0.25
"""Element conditions are probed without their own long wait; the outer condition wait polls."""


@dataclass(frozen=True)
class PageState:
    url: str
    text: str
    headings: tuple[str, ...]


@dataclass(frozen=True)
class ConditionContext:
    surface: ComputerSurface
    resolver: LocatorResolver
    params: Mapping[str, str]


@dataclass(frozen=True)
class ConditionOutcome:
    ok: bool
    failed: Condition | None
    observed: str

    @property
    def expected(self) -> str | None:
        return describe_condition(self.failed) if self.failed is not None else None


async def snapshot_state(surface: ComputerSurface) -> PageState:
    return PageState(
        url=await surface.current_url(),
        text=await surface.page_text(),
        headings=tuple(await surface.page_headings()),
    )


def describe_state(state: PageState, max_chars: int = 240) -> str:
    excerpt = state.text[:max_chars] + ("…" if len(state.text) > max_chars else "")
    headings = ", ".join(state.headings[:3]) or "-"
    return f"url={state.url} headings=[{headings}] text={excerpt!r}"


async def evaluate_condition(condition: Condition, ctx: ConditionContext, state: PageState) -> bool:
    match condition:
        case UrlMatches(pattern=pattern):
            escaped = {k: re.escape(v) for k, v in ctx.params.items()}
            return re.search(substitute(pattern, escaped), state.url) is not None
        case TextPresent(text=text):
            return substitute(text, ctx.params) in state.text
        case TextAbsent(text=text):
            return substitute(text, ctx.params) not in state.text
        case HeadingPresent(text=text):
            return substitute(text, ctx.params) in state.headings
        case ElementVisible(target=target):
            return await _probe(ctx, target, require_enabled=False)
        case ElementEnabled(target=target):
            return await _probe(ctx, target, require_enabled=True)
        case ElementHidden(target=target):
            return not await _probe(ctx, target, require_enabled=False)
        case ElementValue(target=target, value=value):
            return await _has_value(ctx, target, substitute(value, ctx.params))
        case AllOf(conditions=conditions):
            for inner in conditions:
                if not await evaluate_condition(inner, ctx, state):
                    return False
            return True
        case AnyOf(conditions=conditions):
            for inner in conditions:
                if await evaluate_condition(inner, ctx, state):
                    return True
            return False
        case Not(condition=inner):
            return not await evaluate_condition(inner, ctx, state)
    raise TypeError(f"unknown condition {condition!r}")  # pragma: no cover


async def _probe(ctx: ConditionContext, target: TargetSpec, *, require_enabled: bool) -> bool:
    try:
        await ctx.resolver.resolve(
            target, timeout_s=ELEMENT_PROBE_TIMEOUT_S, require_enabled=require_enabled
        )
    except ResolutionError:
        return False
    return True


async def _has_value(ctx: ConditionContext, target: TargetSpec, expected: str) -> bool:
    try:
        resolved = await ctx.resolver.resolve(
            target, timeout_s=ELEMENT_PROBE_TIMEOUT_S, require_enabled=False
        )
    except ResolutionError:
        return False
    if resolved.element is None:
        return False
    return await ctx.surface.read_text(resolved.element) == expected


async def check_conditions(
    conditions: list[Condition], ctx: ConditionContext, state: PageState | None = None
) -> ConditionOutcome:
    """Evaluate once against a single consistent page snapshot."""
    state = state or await snapshot_state(ctx.surface)
    for condition in conditions:
        if not await evaluate_condition(condition, ctx, state):
            return ConditionOutcome(ok=False, failed=condition, observed=describe_state(state))
    return ConditionOutcome(ok=True, failed=None, observed=describe_state(state))


async def wait_for_conditions(
    conditions: list[Condition],
    ctx: ConditionContext,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> ConditionOutcome:
    """Poll until every condition holds; returns the last outcome on timeout."""
    if not conditions:
        return await check_conditions([], ctx)
    last: ConditionOutcome | None = None

    async def satisfied() -> bool:
        nonlocal last
        last = await check_conditions(conditions, ctx)
        return last.ok

    await wait_until(satisfied, timeout_s=timeout_s, poll_interval_s=poll_interval_s)
    assert last is not None
    return last
