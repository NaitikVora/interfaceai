"""Session ownership state machine and the surface guard that enforces it.

Exactly one actor may act on the live surface at a time::

    AUTOMATION --escalate--> ESCALATED --take_control--> HUMAN_CONTROL --release--> RESUMING
    RESUMING --resume--> AUTOMATION            ESCALATED --approve/abort--> AUTOMATION | ABORTED
    any --complete--> COMPLETED

``OwnedSurface`` wraps the one real surface for a given actor and refuses mutating operations
unless that actor currently owns control. Read-only operations (screenshots, URL, text) are
always allowed so the operator console can show the live state while automation is paused.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from app.artifacts.schema import Viewport
from app.automation.surface import (
    ComputerSurface,
    MatchedElement,
    Observation,
    ObservationLimits,
    StrategyBackend,
)


class Actor(StrEnum):
    AUTOMATION = "automation"
    HUMAN = "human"


class SessionState(StrEnum):
    AUTOMATION = "AUTOMATION"
    ESCALATED = "ESCALATED"
    HUMAN_CONTROL = "HUMAN_CONTROL"
    RESUMING = "RESUMING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.AUTOMATION: frozenset({SessionState.ESCALATED, SessionState.COMPLETED}),
    SessionState.ESCALATED: frozenset(
        {SessionState.HUMAN_CONTROL, SessionState.AUTOMATION, SessionState.ABORTED}
    ),
    SessionState.HUMAN_CONTROL: frozenset({SessionState.RESUMING, SessionState.ABORTED}),
    SessionState.RESUMING: frozenset({SessionState.AUTOMATION, SessionState.COMPLETED}),
    SessionState.COMPLETED: frozenset(),
    SessionState.ABORTED: frozenset(),
}

_OWNER: dict[SessionState, Actor | None] = {
    SessionState.AUTOMATION: Actor.AUTOMATION,
    SessionState.ESCALATED: None,
    SessionState.HUMAN_CONTROL: Actor.HUMAN,
    SessionState.RESUMING: Actor.AUTOMATION,
    SessionState.COMPLETED: None,
    SessionState.ABORTED: None,
}


class IllegalTransitionError(RuntimeError):
    pass


class ControlViolationError(PermissionError):
    """An actor tried to act on the surface without owning control."""


class SessionController:
    def __init__(
        self,
        session_id: str,
        *,
        on_transition: Callable[[SessionState, SessionState], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self._state = SessionState.AUTOMATION
        self._on_transition = on_transition
        self.history: list[tuple[SessionState, SessionState]] = []

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def control_owner(self) -> Actor | None:
        return _OWNER[self._state]

    def transition(self, target: SessionState) -> None:
        if target not in _TRANSITIONS[self._state]:
            raise IllegalTransitionError(f"cannot go from {self._state} to {target}")
        previous, self._state = self._state, target
        self.history.append((previous, target))
        if self._on_transition is not None:
            self._on_transition(previous, target)

    def authorize(self, actor: Actor) -> None:
        owner = self.control_owner
        if owner is not actor:
            raise ControlViolationError(
                f"{actor} may not act: session is {self._state}, owner is {owner or 'nobody'}"
            )

    # Convenience transitions with intent-revealing names.
    def escalate(self) -> None:
        self.transition(SessionState.ESCALATED)

    def grant_human_control(self) -> None:
        self.transition(SessionState.HUMAN_CONTROL)

    def release_human_control(self) -> None:
        self.transition(SessionState.RESUMING)

    def resume_automation(self) -> None:
        self.transition(SessionState.AUTOMATION)

    def approve_and_continue(self) -> None:
        self.transition(SessionState.AUTOMATION)

    def abort(self) -> None:
        self.transition(SessionState.ABORTED)

    def complete(self) -> None:
        if self._state in {SessionState.COMPLETED, SessionState.ABORTED}:
            return
        if self._state not in {SessionState.AUTOMATION, SessionState.RESUMING}:
            raise IllegalTransitionError(f"cannot complete from {self._state}")
        self.transition(SessionState.COMPLETED)


class OwnedSurface:
    """The one live surface, as seen by one actor. Mutations require ownership."""

    def __init__(self, inner: ComputerSurface, session: SessionController, actor: Actor) -> None:
        self._inner = inner
        self._session = session
        self._actor = actor

    @property
    def actor(self) -> Actor:
        return self._actor

    @property
    def backend(self) -> StrategyBackend:
        return self._inner.backend

    @property
    def viewport(self) -> Viewport:
        return self._inner.viewport

    def _guard(self) -> None:
        self._session.authorize(self._actor)

    # read-only
    async def observe(self, limits: ObservationLimits) -> Observation:
        return await self._inner.observe(limits)

    async def screenshot(self) -> bytes:
        return await self._inner.screenshot()

    async def current_url(self) -> str:
        return await self._inner.current_url()

    async def page_text(self) -> str:
        return await self._inner.page_text()

    async def page_headings(self) -> list[str]:
        return await self._inner.page_headings()

    async def wait_for_settled(self, timeout_s: float) -> None:
        await self._inner.wait_for_settled(timeout_s)

    async def read_text(self, element: MatchedElement) -> str:
        return await self._inner.read_text(element)

    # mutating
    async def navigate(self, url: str) -> None:
        self._guard()
        await self._inner.navigate(url)

    async def click(self, element: MatchedElement) -> None:
        self._guard()
        await self._inner.click(element)

    async def click_at(self, x: float, y: float) -> None:
        self._guard()
        await self._inner.click_at(x, y)

    async def type_text(self, element: MatchedElement, value: str, *, clear: bool) -> None:
        self._guard()
        await self._inner.type_text(element, value, clear=clear)

    async def select_option(self, element: MatchedElement, value: str) -> None:
        self._guard()
        await self._inner.select_option(element, value)

    async def press(self, key: str) -> None:
        self._guard()
        await self._inner.press(key)
