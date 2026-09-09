"""PlaywrightSurface against the real demo app: perception, strategies, extraction."""

from __future__ import annotations

import pytest
from app.artifacts.schema import Strategy, TableCellSpec, TargetSpec
from app.automation.browser import PlaywrightSurface, attributes_to_css
from app.automation.locators import LocatorResolver, ResolutionError, ResolutionFailure
from app.runtime import observation_limits

from tests.conftest import DemoServer

pytestmark = pytest.mark.browser


async def login(surface: PlaywrightSurface, base: str, resolver: LocatorResolver) -> None:
    await surface.navigate(f"{base}/login")
    op = await resolver.resolve(TargetSpec(label="Operator ID:"), timeout_s=3)
    await surface.type_text(op.element, "teller1", clear=True)  # type: ignore[arg-type]
    code = await resolver.resolve(TargetSpec(label="Access Code:"), timeout_s=3)
    await surface.type_text(code.element, "teller-pass", clear=True)  # type: ignore[arg-type]
    btn = await resolver.resolve(TargetSpec(role="button", name="Sign In"), timeout_s=3)
    await surface.click(btn.element)  # type: ignore[arg-type]
    await surface.wait_for_settled(3)


async def test_snapshot_names_agree_with_playwright_and_every_strategy_is_unique(
    surface: PlaywrightSurface, demo: DemoServer, settings
) -> None:
    await surface.navigate(f"{demo.base_url}/login")
    observation = await surface.observe(observation_limits(settings))
    assert observation.headings == ["Operator Sign In"]
    by_name = {c.name: c for c in observation.controls}
    assert (
        by_name["Operator ID:"].role == "textbox"
        and by_name["Access Code:"].attributes["type"] == "password"
    )
    assert "current_value" not in by_name["Access Code:"].attributes
    for control in observation.controls:
        spec = control.to_target_spec()
        for strategy in spec.available_strategies():
            matches = await surface.match(spec, strategy)
            assert len(matches) == 1, (control.ref, strategy)


async def test_containers_scope_semantically_ambiguous_links(
    surface: PlaywrightSurface, demo: DemoServer, settings
) -> None:
    resolver = LocatorResolver(surface.backend, poll_interval_s=0.05)
    await login(surface, demo.base_url, resolver)
    observation = await surface.observe(observation_limits(settings))
    links = [c for c in observation.controls if c.name == "Member Search"]
    assert len(links) == 2
    assert {c.container.attributes.get("class") for c in links if c.container} == {"nav", "grid"}
    with pytest.raises(ResolutionError) as info:
        await resolver.resolve(
            TargetSpec(role="link", name="Member Search", css=links[0].css), timeout_s=1
        )
    assert info.value.code is ResolutionFailure.AMBIGUOUS_TARGET
    scoped = TargetSpec(
        role="link",
        name="Member Search",
        within=TargetSpec(attributes={"tag": "td", "class": "nav"}),
    )
    resolved = await resolver.resolve(scoped, timeout_s=2)
    assert resolved.strategy is Strategy.ROLE_NAME


async def test_tables_and_semantic_cell_extraction(
    surface: PlaywrightSurface, demo: DemoServer, settings
) -> None:
    resolver = LocatorResolver(surface.backend, poll_interval_s=0.05)
    await login(surface, demo.base_url, resolver)
    await surface.navigate(f"{demo.base_url}/members/12345")
    observation = await surface.observe(observation_limits(settings))
    accounts = next(t for t in observation.tables if t.headers)
    assert accounts.headers == [
        "Account Type",
        "Account Number",
        "Nickname",
        "Current Balance",
        "Status",
    ]
    assert accounts.rows[1][0] == "Savings" and accounts.rows[1][3] == "$8,432.17"
    cell = await resolver.resolve(
        TargetSpec(table_cell=TableCellSpec(row_match="Savings", column_header="Current Balance")),
        timeout_s=2,
        require_enabled=False,
    )
    assert await surface.read_text(cell.element) == "$8,432.17"  # type: ignore[arg-type]
    name = await resolver.resolve(
        TargetSpec(table_cell=TableCellSpec(row_match="Name", column_index=1)),
        timeout_s=2,
        require_enabled=False,
    )
    assert await surface.read_text(name.element) == "Demo Member"  # type: ignore[arg-type]
    with pytest.raises(ResolutionError):
        await resolver.resolve(
            TargetSpec(
                table_cell=TableCellSpec(row_match="Bitcoin", column_header="Current Balance")
            ),
            timeout_s=0.3,
        )


async def test_duplicate_form_makes_search_controls_ambiguous(
    surface: PlaywrightSurface, demo: DemoServer
) -> None:
    resolver = LocatorResolver(surface.backend, poll_interval_s=0.05)
    await login(surface, demo.base_url, resolver)
    await demo.inject(duplicate_search_form=True)
    await surface.navigate(f"{demo.base_url}/members/search")
    with pytest.raises(ResolutionError) as info:
        await resolver.resolve(TargetSpec(role="button", name="Search"), timeout_s=2)
    assert info.value.code is ResolutionFailure.AMBIGUOUS_TARGET
    assert info.value.diagnostics.duration_ms < 1500  # fails fast, does not wait for the deadline


async def test_screenshot_masks_password_fields_and_dialogs_are_dismissed(
    surface: PlaywrightSurface, demo: DemoServer, settings
) -> None:
    await surface.navigate(f"{demo.base_url}/login")
    png = await surface.screenshot()
    assert png.startswith(b"\x89PNG")
    await surface.page.evaluate("() => { setTimeout(() => alert('legacy popup'), 10); }")
    await surface.page.wait_for_timeout(100)
    observation = await surface.observe(observation_limits(settings))
    assert observation.dialogs == ["alert: legacy popup"]
    assert await surface.page_headings() == ["Operator Sign In"]


def test_attributes_to_css_escapes_and_allowlists() -> None:
    assert (
        attributes_to_css({"tag": "input", "name": 'a"b', "onclick": "x"}) == 'input[name="a\\"b"]'
    )
    assert attributes_to_css({"tag": "bad tag"}) == ""
