"""Condition evaluation, waits and the runtime-condition rule engine (fake surface)."""

from __future__ import annotations

import asyncio

from app.artifacts.schema import (
    AllOf,
    AnyOf,
    ConditionRule,
    ElementValue,
    ElementVisible,
    HeadingPresent,
    Not,
    OutcomeCategory,
    Strategy,
    TargetSpec,
    TextAbsent,
    TextPresent,
    UrlMatches,
)
from app.automation.locators import LocatorResolver
from app.replay.checkpoints import ConditionContext, check_conditions, wait_for_conditions
from app.replay.rules import RuleEngine

from tests.fakes.fake_surface import FakeBackend, FakeSurface, element


def ctx(surface: FakeSurface, **params: str) -> ConditionContext:
    resolver = LocatorResolver(surface.backend, poll_interval_s=0.01)
    return ConditionContext(surface, resolver, {"member_id": "12345", **params})


async def test_text_url_and_heading_conditions() -> None:
    surface = FakeSurface(
        url="http://h/members/12345",
        text="Member Details ... Accounts",
        headings=["Member Details"],
    )
    outcome = await check_conditions(
        [
            UrlMatches(pattern="/members/${member_id}$"),
            TextPresent(text="Accounts"),
            TextAbsent(text="No member found"),
            HeadingPresent(text="Member Details"),
            Not(condition=HeadingPresent(text="System Notice")),
            AnyOf(conditions=[TextPresent(text="zzz"), TextPresent(text="Member")]),
            AllOf(conditions=[TextPresent(text="Member"), TextPresent(text="Accounts")]),
        ],
        ctx(surface),
    )
    assert outcome.ok and outcome.failed is None
    assert "headings=[Member Details]" in outcome.observed


async def test_url_parameters_are_regex_escaped() -> None:
    surface = FakeSurface(url="http://h/members/12.45")
    assert (
        await check_conditions(
            [UrlMatches(pattern="/members/${member_id}$")], ctx(surface, member_id="12.45")
        )
    ).ok
    assert not (
        await check_conditions(
            [UrlMatches(pattern="/members/${member_id}$")], ctx(surface, member_id="12x45")
        )
    ).ok


async def test_failed_condition_is_reported_with_observation() -> None:
    surface = FakeSurface(
        url="http://h/members/search", text="No member found", headings=["Member Search"]
    )
    outcome = await check_conditions([HeadingPresent(text="Member Details")], ctx(surface))
    assert not outcome.ok and outcome.expected == "page heading 'Member Details' is shown"
    assert "url=http://h/members/search" in outcome.observed


async def test_element_conditions_use_the_resolver() -> None:
    backend = FakeBackend({(Strategy.ROLE_NAME, "textbox:Member Number"): [element("field")]})
    surface = FakeSurface(backend_impl=backend, values={"field": "12345"})
    target = TargetSpec(role="textbox", name="Member Number")
    outcome = await check_conditions(
        [ElementVisible(target=target), ElementValue(target=target, value="${member_id}")],
        ctx(surface),
    )
    assert outcome.ok
    outcome = await check_conditions([ElementValue(target=target, value="99999")], ctx(surface))
    assert not outcome.ok


async def test_wait_for_conditions_polls_until_state_changes() -> None:
    surface = FakeSurface(url="http://h/loading", text="Loading")

    async def arrive() -> None:
        await asyncio.sleep(0.05)
        surface.url, surface.headings = "http://h/members/12345", ["Member Details"]

    task = asyncio.create_task(arrive())
    outcome = await wait_for_conditions(
        [HeadingPresent(text="Member Details")], ctx(surface), timeout_s=2, poll_interval_s=0.01
    )
    await task
    assert outcome.ok


RULES = [
    ConditionRule(
        id="not-found",
        description="no member",
        when=TextPresent(text="No member found"),
        category=OutcomeCategory.BUSINESS_OUTCOME,
        code="MEMBER_NOT_FOUND",
        outputs={"status": "not_found"},
    ),
    ConditionRule(
        id="notice",
        description="notice",
        when=HeadingPresent(text="System Notice"),
        category=OutcomeCategory.RECOVERABLE,
        code="KNOWN_DIALOG",
        recovery={"kind": "click", "target": {"role": "button", "name": "Acknowledge"}},
    ),
    ConditionRule(
        id="error",
        description="error",
        when=TextPresent(text="Application Error"),
        category=OutcomeCategory.HARD_FAILURE,
        code="UNEXPECTED_STATE",
    ),
]


async def test_rule_engine_matches_in_order_and_classifies() -> None:
    engine = RuleEngine(RULES)
    surface = FakeSurface(text="No member found matching 99999", headings=["Member Search"])
    match = await engine.first_match(ctx(surface))
    assert match is not None and match.rule.code == "MEMBER_NOT_FOUND"
    assert match.category is OutcomeCategory.BUSINESS_OUTCOME

    surface = FakeSurface(text="Application Error", headings=["Application Error"])
    match = await engine.first_match(ctx(surface))
    assert match is not None and match.category is OutcomeCategory.HARD_FAILURE

    assert (
        await engine.first_match(ctx(FakeSurface(text="Main Menu", headings=["Main Menu"]))) is None
    )


async def test_wait_for_expected_state_confirms_rules_on_consecutive_polls() -> None:
    engine = RuleEngine(RULES)
    surface = FakeSurface(
        url="http://h/members/search", text="No member found", headings=["Member Search"]
    )
    outcome, match = await engine.wait_for_expected_state(
        [HeadingPresent(text="Member Details")], ctx(surface), timeout_s=5, poll_interval_s=0.01
    )
    assert not outcome.ok and match is not None and match.rule.id == "not-found"

    # a rule that matches only once (stale page mid-navigation) does not fire
    flicker = FakeSurface(url="http://h/x", text="No member found", headings=["Member Search"])

    async def navigate() -> None:
        await asyncio.sleep(0.005)
        flicker.text, flicker.headings = "Member Details Accounts", ["Member Details"]

    task = asyncio.create_task(navigate())
    outcome, match = await engine.wait_for_expected_state(
        [HeadingPresent(text="Member Details")], ctx(flicker), timeout_s=2, poll_interval_s=0.01
    )
    await task
    assert outcome.ok and match is None
