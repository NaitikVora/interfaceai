"""Records actions a human performs *directly* in a headed browser window during HUMAN_CONTROL.

Console-proxied actions are recorded by the manager itself. This adapter covers the other
path: with ``HEADLESS=false`` the operator can simply click in the live window; capture-phase DOM
listeners report each click/change/submit back through a Playwright binding. Password values
are never reported. Events are ignored unless a human currently owns the session, so
automation's own synthetic events are not mistaken for human ones.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from app.automation.browser import PlaywrightSurface
from app.automation.surface import ComputerSurface
from app.escalation.manager import EscalationManager, HumanActionRecord
from app.escalation.session import SessionState

BINDING_NAME = "__cuaHumanEvent"
INIT_SCRIPT = (Path(__file__).parent / "human_recorder.js").read_text(encoding="utf-8")


class HumanActionRecorder:
    def __init__(self, page: Page, manager: EscalationManager) -> None:
        self._page = page
        self._manager = manager

    async def install(self) -> None:
        await self._page.expose_binding(BINDING_NAME, self._on_event)
        await self._page.add_init_script(INIT_SCRIPT)
        await self._page.evaluate(INIT_SCRIPT)

    async def _on_event(self, _source: Any, payload: dict[str, Any]) -> None:
        if self._manager.session.state is not SessionState.HUMAN_CONTROL:
            return
        if self._manager.proxied_action_in_progress:
            return
        target_bits = [
            payload.get("tag"),
            f"type={payload['type']}" if payload.get("type") else None,
            f"name={payload['name']}" if payload.get("name") else None,
            f"label={payload['label']!r}" if payload.get("label") else None,
            f"text={payload['text']!r}" if payload.get("text") else None,
            f"href={payload['href']}" if payload.get("href") else None,
        ]
        self._manager.record_browser_action(
            HumanActionRecord(
                timestamp=datetime.now(UTC),
                source="browser",
                action=str(payload.get("kind", "event")),
                target=" ".join(b for b in target_bits if b),
                value=payload.get("value") or None,
                url_before=str(payload.get("url", "")),
                url_after=self._page.url,
            )
        )


async def install_human_recorder(surface: ComputerSurface, manager: EscalationManager) -> bool:
    """Attach the recorder when the surface is a Playwright page. Returns False otherwise."""
    if not isinstance(surface, PlaywrightSurface):
        return False
    await HumanActionRecorder(surface.page, manager).install()
    return True
