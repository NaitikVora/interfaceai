"""Locator resolver: priority, ambiguity policy, scoping, fallbacks, diagnostics."""

from __future__ import annotations

import pytest
from app.artifacts.schema import Point, Strategy, TargetSpec, Viewport
from app.automation.locators import LocatorResolver, ResolutionError, ResolutionFailure

from tests.fakes.fake_surface import FakeBackend, element


def resolver(backend: FakeBackend) -> LocatorResolver:
    return LocatorResolver(backend, poll_interval_s=0.01, ambiguity_confirmations=2)


SPEC = TargetSpec(
    role="button",
    name="Search",
    attributes={"tag": "input", "value": "Search"},
    css="form > input",
    xpath="/html/body/form/input",
)


async def test_semantic_strategy_wins_and_structural_is_not_consulted() -> None:
    backend = FakeBackend(
        {
            (Strategy.ROLE_NAME, "button:Search"): [element("btn")],
            (Strategy.CSS, "form > input"): [element("btn")],
        }
    )
    resolved = await resolver(backend).resolve(SPEC, timeout_s=1)
    assert resolved.strategy is Strategy.ROLE_NAME
    assert resolved.element is not None and resolved.element.handle == "btn"
    assert all(call[0] is not Strategy.CSS for call in backend.calls)
    assert resolved.diagnostics.drift_fallback is False


async def test_more_specific_semantic_strategy_breaks_a_tie() -> None:
    backend = FakeBackend(
        {
            (Strategy.ROLE_NAME, "button:Search"): [element("a"), element("b")],
            (Strategy.ATTRIBUTES, "tag=input,value=Search"): [element("b")],
        }
    )
    resolved = await resolver(backend).resolve(SPEC, timeout_s=1)
    assert resolved.strategy is Strategy.ATTRIBUTES
    assert [a.detail for a in resolved.diagnostics.attempts] == ["ambiguous", "unique"]


async def test_structural_strategies_never_break_a_semantic_tie() -> None:
    backend = FakeBackend(
        {
            (Strategy.ROLE_NAME, "button:Search"): [element("a"), element("b")],
            (Strategy.ATTRIBUTES, "tag=input,value=Search"): [element("a"), element("b")],
            (Strategy.CSS, "form > input"): [element("a")],
            (Strategy.XPATH, "/html/body/form/input"): [element("a")],
        }
    )
    with pytest.raises(ResolutionError) as info:
        await resolver(backend).resolve(SPEC, timeout_s=1)
    assert info.value.code is ResolutionFailure.AMBIGUOUS_TARGET
    details = [a.detail for a in info.value.diagnostics.attempts]
    assert "skipped: cannot break a semantic tie" in details
    assert info.value.diagnostics.polls == 2  # confirmed on consecutive polls, then failed fast


async def test_structural_fallback_recovers_from_drift_and_flags_it() -> None:
    backend = FakeBackend({(Strategy.CSS, "form > input"): [element("btn")]})
    resolved = await resolver(backend).resolve(SPEC, timeout_s=1)
    assert resolved.strategy is Strategy.CSS
    assert resolved.diagnostics.drift_fallback is True


async def test_hidden_and_disabled_elements_are_not_usable() -> None:
    backend = FakeBackend(
        {
            (Strategy.ROLE_NAME, "button:Search"): [
                element("hidden", visible=False),
                element("disabled", enabled=False),
                element("ok"),
            ]
        }
    )
    resolved = await resolver(backend).resolve(SPEC, timeout_s=1)
    assert resolved.element is not None and resolved.element.handle == "ok"
    assert resolved.diagnostics.attempts[0].detail == "unique after filtering hidden/disabled"

    backend = FakeBackend({(Strategy.ROLE_NAME, "button:Search"): [element("d", enabled=False)]})
    with pytest.raises(ResolutionError) as info:
        await resolver(backend).resolve(SPEC, timeout_s=0.05)
    assert info.value.code is ResolutionFailure.TARGET_NOT_FOUND
    assert "1 disabled" in info.value.diagnostics.attempts[0].detail
    # extraction does not need an enabled element
    resolved = await resolver(backend).resolve(SPEC, timeout_s=0.05, require_enabled=False)
    assert resolved.element is not None and resolved.element.handle == "d"


async def test_not_found_polls_until_deadline() -> None:
    backend = FakeBackend()
    with pytest.raises(ResolutionError) as info:
        await resolver(backend).resolve(SPEC, timeout_s=0.05)
    assert info.value.code is ResolutionFailure.TARGET_NOT_FOUND
    assert info.value.diagnostics.polls >= 2


async def test_fallback_spec_is_used_after_primary_is_exhausted() -> None:
    spec = TargetSpec(
        role="button", name="Search", fallbacks=[TargetSpec(role="button", name="Find")]
    )
    backend = FakeBackend({(Strategy.ROLE_NAME, "button:Find"): [element("find")]})
    resolved = await resolver(backend).resolve(spec, timeout_s=1)
    assert resolved.element is not None and resolved.element.handle == "find"
    assert resolved.spec.name == "Find"
    assert [a.spec_index for a in resolved.diagnostics.attempts] == [0, 1]


async def test_within_scope_disambiguates_semantic_strategies() -> None:
    spec = TargetSpec(role="link", name="Member Search", within=TargetSpec(css="td.nav"))
    backend = FakeBackend(
        {
            (Strategy.CSS, "td.nav"): [element("nav")],
            (Strategy.ROLE_NAME, "link:Member Search"): [element("nav-link"), element("menu-link")],
            (Strategy.ROLE_NAME, "scoped:link:Member Search"): [element("nav-link")],
        }
    )
    resolved = await resolver(backend).resolve(spec, timeout_s=1)
    assert resolved.element is not None and resolved.element.handle == "nav-link"
    assert any(scoped for _, _, scoped in backend.calls)


async def test_unresolvable_scope_skips_semantic_strategies_but_allows_structural() -> None:
    spec = TargetSpec(role="link", name="X", within=TargetSpec(css="td.missing"), css="a.x")
    backend = FakeBackend({(Strategy.CSS, "a.x"): [element("x")]})
    resolved = await resolver(backend).resolve(spec, timeout_s=1)
    assert resolved.strategy is Strategy.CSS
    assert any("container not resolved" in a.detail for a in resolved.diagnostics.attempts)


async def test_coordinates_only_when_opted_in_and_nothing_else_matched() -> None:
    spec = TargetSpec(
        role="button",
        name="Go",
        coordinates=Point(x=10, y=20),
        viewport=Viewport(width=1, height=1),
    )
    backend = FakeBackend()
    with pytest.raises(ResolutionError):
        await resolver(backend).resolve(spec, timeout_s=0.02)
    resolved = await resolver(backend).resolve(spec, timeout_s=0.02, allow_coordinates=True)
    assert resolved.strategy is Strategy.COORDINATES and resolved.point == Point(x=10, y=20)


async def test_frames_are_explicitly_unsupported() -> None:
    spec = TargetSpec(css="#x", frame="iframe[name=main]")
    with pytest.raises(ResolutionError) as info:
        await resolver(FakeBackend()).resolve(spec, timeout_s=0.02)
    assert info.value.code is ResolutionFailure.UNSUPPORTED_TARGET


async def test_diagnostics_serialize() -> None:
    backend = FakeBackend({(Strategy.ROLE_NAME, "button:Search"): [element("btn")]})
    resolved = await resolver(backend).resolve(SPEC, timeout_s=1)
    data = resolved.diagnostics.as_dict()
    assert data["attempts"][0] == {
        "spec": 0,
        "strategy": "role_name",
        "matches": 1,
        "usable": 1,
        "detail": "unique",
    }
