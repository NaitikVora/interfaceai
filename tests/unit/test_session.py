"""Session ownership state machine and the OwnedSurface guard."""

from __future__ import annotations

import pytest
from app.escalation.session import (
    Actor,
    ControlViolationError,
    IllegalTransitionError,
    OwnedSurface,
    SessionController,
    SessionState,
)

from tests.fakes.fake_surface import FakeSurface, element


def test_happy_path_transitions_and_owners() -> None:
    seen: list[tuple[SessionState, SessionState]] = []
    session = SessionController("run", on_transition=lambda a, b: seen.append((a, b)))
    assert session.state is SessionState.AUTOMATION and session.control_owner is Actor.AUTOMATION
    session.escalate()
    assert session.control_owner is None  # paused: nobody acts
    session.grant_human_control()
    assert session.control_owner is Actor.HUMAN
    session.release_human_control()
    assert session.state is SessionState.RESUMING and session.control_owner is Actor.AUTOMATION
    session.resume_automation()
    session.complete()
    assert session.state is SessionState.COMPLETED
    assert [b for _, b in seen] == [
        SessionState.ESCALATED,
        SessionState.HUMAN_CONTROL,
        SessionState.RESUMING,
        SessionState.AUTOMATION,
        SessionState.COMPLETED,
    ]


def test_approval_path_and_abort() -> None:
    session = SessionController("run")
    session.escalate()
    session.approve_and_continue()
    assert session.state is SessionState.AUTOMATION
    session.escalate()
    session.grant_human_control()
    session.abort()
    assert session.state is SessionState.ABORTED
    session.complete()  # idempotent on terminal states
    assert session.state is SessionState.ABORTED


def test_illegal_transitions_are_rejected() -> None:
    session = SessionController("run")
    with pytest.raises(IllegalTransitionError):
        session.grant_human_control()  # must escalate first
    with pytest.raises(IllegalTransitionError):
        session.release_human_control()
    session.escalate()
    with pytest.raises(IllegalTransitionError):
        session.complete()  # cannot complete while paused for a human
    session.grant_human_control()
    with pytest.raises(IllegalTransitionError):
        session.escalate()
    with pytest.raises(IllegalTransitionError):
        session.approve_and_continue()


async def test_owned_surface_enforces_control_in_code() -> None:
    inner = FakeSurface()
    session = SessionController("run")
    automation = OwnedSurface(inner, session, Actor.AUTOMATION)
    human = OwnedSurface(inner, session, Actor.HUMAN)
    btn = element("btn")

    await automation.click(btn)
    with pytest.raises(ControlViolationError):
        await human.click(btn)

    session.escalate()
    with pytest.raises(ControlViolationError):
        await automation.click(btn)  # paused: nobody may act
    with pytest.raises(ControlViolationError):
        await human.type_text(btn, "x", clear=True)
    # read-only operations stay available to everyone (console needs the live screenshot)
    assert await human.screenshot() and await automation.current_url()

    session.grant_human_control()
    await human.click(btn)
    with pytest.raises(ControlViolationError):
        await automation.navigate("http://x")
    session.release_human_control()
    await automation.press("Enter")  # RESUMING already belongs to automation
    with pytest.raises(ControlViolationError):
        await human.press("Enter")
    assert [a for a, _ in inner.actions] == ["click", "click", "press"]
