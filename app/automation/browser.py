"""Playwright implementation of ``ComputerSurface`` and ``StrategyBackend``.

This is the only module that knows about Playwright locators. Everything above it works with
``TargetSpec`` strategies and opaque ``MatchedElement`` handles.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Locator,
    Page,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.artifacts.schema import Strategy, TargetSpec, Viewport
from app.automation.surface import (
    MatchedElement,
    Observation,
    ObservationLimits,
)
from app.automation.waits import Deadline
from app.config import Settings

_SNAPSHOT_JS = (Path(__file__).parent / "dom_snapshot.js").read_text(encoding="utf-8")
_TABLE_CELL_JS = (Path(__file__).parent / "table_cell.js").read_text(encoding="utf-8")

MAX_MATCHES_INSPECTED = 12
"""Upper bound on candidates inspected per strategy; beyond this the match is clearly ambiguous."""

ATTRIBUTE_KEYS = (
    "name",
    "id",
    "type",
    "value",
    "title",
    "href",
    "placeholder",
    "action",
    "role",
    "class",
)

AriaRole = Literal[
    "button",
    "cell",
    "checkbox",
    "columnheader",
    "combobox",
    "heading",
    "link",
    "listbox",
    "menuitem",
    "radio",
    "searchbox",
    "slider",
    "spinbutton",
    "tab",
    "textbox",
]
KNOWN_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "cell",
        "checkbox",
        "columnheader",
        "combobox",
        "heading",
        "link",
        "listbox",
        "menuitem",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "tab",
        "textbox",
    }
)

_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


class SurfaceActionError(RuntimeError):
    """A low-level action failed on the surface (element detached, navigation aborted, ...)."""


class PlaywrightSurface:
    """A single page in a single browser context. One instance per run."""

    def __init__(self, page: Page, *, action_timeout_ms: int) -> None:
        self._page = page
        self._action_timeout_ms = action_timeout_ms
        self._dialog_messages: list[str] = []
        page.on("dialog", self._on_dialog)

    # ------------------------------------------------------------------ properties
    @property
    def page(self) -> Page:
        """Escape hatch for Playwright-specific adapters (human action recorder)."""
        return self._page

    @property
    def backend(self) -> PlaywrightSurface:
        return self

    @property
    def viewport(self) -> Viewport:
        size = self._page.viewport_size or {"width": 1280, "height": 900}
        return Viewport(width=size["width"], height=size["height"])

    # ------------------------------------------------------------------ perception
    async def observe(self, limits: ObservationLimits) -> Observation:
        raw = await self._page.evaluate(
            _SNAPSHOT_JS,
            {
                "max_text_chars": limits.max_text_chars,
                "max_controls": limits.max_controls,
                "max_tables": limits.max_tables,
                "max_table_rows": limits.max_table_rows,
                "max_table_cols": limits.max_table_cols,
                "max_cell_chars": limits.max_cell_chars,
            },
        )
        dialogs, self._dialog_messages = self._dialog_messages, []
        return Observation.model_validate(
            {**raw, "dialogs": dialogs, "captured_at": datetime.now(UTC)}
        )

    async def current_url(self) -> str:
        return self._page.url

    async def page_text(self) -> str:
        text = await self._page.evaluate("() => document.body ? document.body.innerText : ''")
        return normalize_text(str(text))

    async def page_headings(self) -> list[str]:
        raw = await self._page.evaluate(
            """() => Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'))
                .filter(h => {
                    const r = h.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                })
                .map(h => h.textContent)"""
        )
        return [normalize_text(str(h)) for h in raw if normalize_text(str(h))]

    async def screenshot(self) -> bytes:
        return await self._page.screenshot(
            type="png", mask=[self._page.locator("input[type=password]")]
        )

    async def wait_for_settled(self, timeout_s: float) -> None:
        """Wait for the document to load and its text to stop changing (bounded)."""
        deadline = Deadline(timeout_s)
        try:
            await self._page.wait_for_load_state(
                "load", timeout=max(int(deadline.remaining() * 1000), 1)
            )
        except PlaywrightTimeoutError:
            return
        previous = await self._state_sample()
        while not deadline.expired():
            await asyncio.sleep(0.1)
            current = await self._state_sample()
            if current == previous:
                return
            previous = current

    async def _state_sample(self) -> tuple[str, int]:
        length = await self._page.evaluate(
            "() => document.body ? document.body.innerText.length : 0"
        )
        return self._page.url, int(length)

    # ------------------------------------------------------------------ strategy backend
    async def match(
        self, spec: TargetSpec, strategy: Strategy, scope: MatchedElement | None = None
    ) -> list[MatchedElement]:
        root: Page | Locator = self._page if scope is None else self._handle(scope)
        locator = await self._locator_for(root, spec, strategy)
        if locator is None:
            return []
        try:
            count = await locator.count()
        except PlaywrightError:
            return []
        matched: list[MatchedElement] = []
        for index in range(min(count, MAX_MATCHES_INSPECTED)):
            nth = locator.nth(index)
            try:
                visible = await nth.is_visible()
                enabled = await nth.is_enabled() if visible else False
            except PlaywrightError:
                continue
            matched.append(MatchedElement(handle=nth, visible=visible, enabled=enabled))
        return matched

    async def _locator_for(
        self, root: Page | Locator, spec: TargetSpec, strategy: Strategy
    ) -> Locator | None:
        match strategy:
            case Strategy.ROLE_NAME:
                if spec.role not in KNOWN_ROLES or spec.name is None:
                    return None
                return root.get_by_role(cast(AriaRole, spec.role), name=spec.name, exact=True)
            case Strategy.LABEL:
                return root.get_by_label(spec.label or "", exact=True)
            case Strategy.ATTRIBUTES:
                css = attributes_to_css(spec.attributes or {})
                return root.locator(css) if css else None
            case Strategy.TEXT:
                return root.get_by_text(spec.text or "", exact=True)
            case Strategy.TABLE_CELL:
                if spec.table_cell is None:
                    return None
                xpaths = await self._page.evaluate(
                    _TABLE_CELL_JS, spec.table_cell.model_dump(exclude_none=True)
                )
                if not xpaths:
                    return None
                union = " | ".join(str(x) for x in xpaths)
                return self._page.locator(f"xpath={union}")
            case Strategy.CSS:
                return self._page.locator(spec.css or "")
            case Strategy.XPATH:
                return self._page.locator(f"xpath={spec.xpath}")
            case Strategy.COORDINATES:
                return None

    # ------------------------------------------------------------------ actions
    async def navigate(self, url: str) -> None:
        try:
            await self._page.goto(
                url, wait_until="domcontentloaded", timeout=self._action_timeout_ms
            )
        except PlaywrightError as exc:
            raise SurfaceActionError(f"navigate failed: {_short(exc)}") from exc

    async def click(self, element: MatchedElement) -> None:
        try:
            await self._handle(element).click(timeout=self._action_timeout_ms)
        except PlaywrightError as exc:
            raise SurfaceActionError(f"click failed: {_short(exc)}") from exc

    async def click_at(self, x: float, y: float) -> None:
        try:
            await self._page.mouse.click(x, y)
        except PlaywrightError as exc:
            raise SurfaceActionError(f"click_at failed: {_short(exc)}") from exc

    async def type_text(self, element: MatchedElement, value: str, *, clear: bool) -> None:
        handle = self._handle(element)
        try:
            if clear:
                await handle.fill(value, timeout=self._action_timeout_ms)
            else:
                await handle.press_sequentially(value, timeout=self._action_timeout_ms)
        except PlaywrightError as exc:
            raise SurfaceActionError(f"type failed: {_short(exc)}") from exc

    async def select_option(self, element: MatchedElement, value: str) -> None:
        handle = self._handle(element)
        try:
            options = await handle.evaluate(
                "el => Array.from(el.options || []).map(o => [o.value, o.label.trim()])"
            )
            labels = {label: val for val, label in options}
            values = {val for val, _ in options}
            if value in labels:
                await handle.select_option(value=labels[value], timeout=self._action_timeout_ms)
            elif value in values:
                await handle.select_option(value=value, timeout=self._action_timeout_ms)
            else:
                raise SurfaceActionError(
                    f"select failed: option {value!r} not among {sorted(labels)}"
                )
        except PlaywrightError as exc:
            raise SurfaceActionError(f"select failed: {_short(exc)}") from exc

    async def press(self, key: str) -> None:
        try:
            await self._page.keyboard.press(key)
        except PlaywrightError as exc:
            raise SurfaceActionError(f"press failed: {_short(exc)}") from exc

    async def read_text(self, element: MatchedElement) -> str:
        handle = self._handle(element)
        try:
            value = await handle.evaluate(
                """el => {
                    const tag = el.tagName.toLowerCase();
                    if (tag === 'select') return el.selectedOptions[0]?.label ?? '';
                    if (tag === 'input' || tag === 'textarea') return el.value ?? '';
                    return el.innerText ?? el.textContent ?? '';
                }"""
            )
        except PlaywrightError as exc:
            raise SurfaceActionError(f"read failed: {_short(exc)}") from exc
        return normalize_text(str(value))

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _handle(element: MatchedElement) -> Locator:
        handle: Any = element.handle
        if not isinstance(handle, Locator):
            raise SurfaceActionError("element handle does not belong to this surface")
        return handle

    async def _on_dialog(self, dialog: Dialog) -> None:
        self._dialog_messages.append(f"{dialog.type}: {dialog.message}")
        await dialog.dismiss()


def attributes_to_css(attributes: dict[str, str]) -> str:
    """Build a CSS selector from stable semantic attributes (tag + allow-listed attributes)."""
    tag = attributes.get("tag", "")
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9-]*", tag or "a"):
        return ""
    parts = [tag]
    for key in ATTRIBUTE_KEYS:
        value = attributes.get(key)
        if value:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'[{key}="{escaped}"]')
    return "".join(parts) if len(parts) > 1 or tag else ""


def _short(exc: BaseException) -> str:
    return str(exc).splitlines()[0][:200]


@asynccontextmanager
async def launch_surface(settings: Settings) -> AsyncIterator[PlaywrightSurface]:
    """Launch Chromium with one context and one page; closes everything on exit."""
    async with async_playwright() as playwright:
        browser: Browser = await playwright.chromium.launch(
            headless=settings.headless, slow_mo=settings.browser_slow_mo_ms
        )
        context: BrowserContext = await browser.new_context(
            viewport={"width": settings.viewport_width, "height": settings.viewport_height}
        )
        page = await context.new_page()
        try:
            yield PlaywrightSurface(
                page, action_timeout_ms=int(settings.replay_default_step_timeout_s * 1000)
            )
        finally:
            await context.close()
            await browser.close()
