"""Runtime-condition rule engine: recognizes known application states.

Consulted by replay only when a pre/postcondition is not met, and by discovery before each
model call so that known interstitials are handled deterministically instead of by the LLM.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.artifacts.schema import Condition, ConditionRule, OutcomeCategory
from app.automation.locators import ResolutionError
from app.automation.waits import Deadline
from app.replay.checkpoints import (
    ConditionContext,
    ConditionOutcome,
    PageState,
    check_conditions,
    describe_state,
    evaluate_condition,
    snapshot_state,
)

RULE_CONFIRMATIONS = 2
"""A rule must match on this many consecutive polls before it fires, so that a stale page
caught mid-navigation cannot trigger a spurious business outcome."""


@dataclass(frozen=True)
class RuleMatch:
    rule: ConditionRule
    observed: str
    message: str | None

    @property
    def category(self) -> OutcomeCategory:
        return self.rule.category


class RuleEngine:
    def __init__(self, rules: list[ConditionRule]) -> None:
        self._rules = rules

    @property
    def rules(self) -> list[ConditionRule]:
        return self._rules

    async def first_match(
        self, ctx: ConditionContext, state: PageState | None = None
    ) -> RuleMatch | None:
        """Rules are evaluated in artifact order against one consistent page snapshot."""
        if not self._rules:
            return None
        state = state or await snapshot_state(ctx.surface)
        for rule in self._rules:
            if await evaluate_condition(rule.when, ctx, state):
                return RuleMatch(
                    rule=rule,
                    observed=describe_state(state),
                    message=await self._read_message(rule, ctx),
                )
        return None

    async def wait_for_expected_state(
        self,
        conditions: list[Condition],
        ctx: ConditionContext,
        *,
        timeout_s: float,
        poll_interval_s: float,
    ) -> tuple[ConditionOutcome, RuleMatch | None]:
        """Wait until the expected conditions hold **or** a known runtime state is recognized.

        Returns ``(outcome, None)`` when the conditions hold or the timeout elapsed, and
        ``(outcome, match)`` as soon as a rule has matched on consecutive polls.
        """
        deadline = Deadline(timeout_s)
        streak: tuple[str, int] | None = None
        while True:
            state = await snapshot_state(ctx.surface)
            outcome = await check_conditions(conditions, ctx, state)
            if outcome.ok:
                return outcome, None
            match = await self.first_match(ctx, state)
            if match is not None:
                streak = (
                    (match.rule.id, streak[1] + 1)
                    if streak and streak[0] == match.rule.id
                    else (match.rule.id, 1)
                )
                if streak[1] >= RULE_CONFIRMATIONS:
                    return outcome, match
            else:
                streak = None
            if deadline.expired():
                return outcome, None
            await asyncio.sleep(min(poll_interval_s, max(deadline.remaining(), 0.01)))

    @staticmethod
    async def _read_message(rule: ConditionRule, ctx: ConditionContext) -> str | None:
        if rule.message_from is None:
            return None
        try:
            resolved = await ctx.resolver.resolve(
                rule.message_from, timeout_s=0.25, require_enabled=False
            )
        except ResolutionError:
            return None
        if resolved.element is None:
            return None
        return (await ctx.surface.read_text(resolved.element))[:300]
