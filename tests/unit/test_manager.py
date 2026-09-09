"""Escalation manager protocol with a fake surface (no browser)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.artifacts.schema import ActionType, Strategy, TargetSpec
from app.config import Settings
from app.escalation.manager import (
    EscalationManager,
    InterventionKind,
    InterventionStatus,
    OperatorAction,
    OperatorError,
)
from app.escalation.session import SessionController, SessionState
from app.observability.events import EventLog, EventType
from app.observability.evidence import EvidenceStore
from app.runtime import observation_limits
from app.safety.redaction import Redactor

from tests.fakes.fake_surface import FakeBackend, FakeSurface, element


def make_manager(tmp_path: Path, surface: FakeSurface, *, attended: bool, timeout_s: float = 5.0):
    redactor = Redactor(["teller-pass"])
    events = EventLog("run", redactor=redactor, sinks=[])
    manager = EscalationManager(
        session=SessionController("run"),
        surface=surface,
        events=events,
        evidence=EvidenceStore(tmp_path / "ev", redactor),
        observation_limits=observation_limits(Settings(_env_file=None)),  # type: ignore[call-arg]
        poll_interval_s=0.01,
        timeout_s=timeout_s,
        attended=attended,
        secret_values=frozenset({"teller-pass"}),
    )
    return manager, events


def request_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": InterventionKind.TARGET_UNRESOLVED,
        "reason": "ambiguous",
        "goal": "g",
        "capability_id": "cap",
        "capability_name": "cap_name",
        "step_id": "s06",
        "step_index": 6,
    }
    return {**base, **overrides}


async def test_unattended_request_is_recorded_and_aborts_the_session(tmp_path: Path) -> None:
    surface = FakeSurface()
    manager, events = make_manager(tmp_path, surface, attended=False)
    request = await manager.request(**request_kwargs())  # type: ignore[arg-type]
    assert request.status is InterventionStatus.UNATTENDED
    assert manager.session.state is SessionState.ABORTED
    assert request.screenshot_ref == "intervention-01-target_unresolved.png"
    assert (tmp_path / "ev" / "int-001.json").exists()
    assert [e.event_type for e in events.events][:2] == [
        EventType.SESSION_STATE_CHANGED,
        EventType.ESCALATION_CREATED,
    ] or EventType.ESCALATION_CREATED in [e.event_type for e in events.events]


async def test_take_control_act_release_resumes_automation(tmp_path: Path) -> None:
    backend = FakeBackend({(Strategy.CSS, "input.member"): [element("field")]})
    surface = FakeSurface(backend_impl=backend)
    manager, events = make_manager(tmp_path, surface, attended=True)

    async def human() -> None:
        while not manager.open_interventions():
            await asyncio.sleep(0.01)
        request = manager.open_interventions()[0]
        with pytest.raises(OperatorError):
            manager.release(request.id)  # not under human control yet
        manager.take_control(request.id)
        assert manager.session.state is SessionState.HUMAN_CONTROL
        with pytest.raises(OperatorError):
            manager.approve(request.id)  # wrong state
        record = await manager.perform_human_action(
            request.id,
            OperatorAction(
                action=ActionType.TYPE, target=TargetSpec(css="input.member"), value="teller-pass"
            ),
        )
        assert record.ok and record.value == "[REDACTED]" and record.source == "console"
        with pytest.raises(OperatorError):  # unresolvable target is reported, not guessed
            await manager.perform_human_action(
                request.id, OperatorAction(action=ActionType.CLICK, target=TargetSpec(css="#nope"))
            )
        manager.release(request.id, "done")

    task = asyncio.create_task(human())
    request = await manager.request(**request_kwargs())  # type: ignore[arg-type]
    await task
    assert request.status is InterventionStatus.RELEASED and request.operator_note == "done"
    assert manager.session.state is SessionState.AUTOMATION
    assert [a.ok for a in request.human_action_log] == [True, False]
    assert surface.actions == [("type", ("field", "teller-pass"))]
    types = [e.event_type for e in events.events]
    assert EventType.HUMAN_CONTROL_GRANTED in types and EventType.HUMAN_ACTION in types
    assert EventType.HUMAN_CONTROL_RELEASED in types and EventType.ESCALATION_RESOLVED in types


async def test_approve_only_for_approval_requests(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path, FakeSurface(), attended=True)

    async def human() -> None:
        while not manager.open_interventions():
            await asyncio.sleep(0.01)
        request = manager.open_interventions()[0]
        manager.approve(request.id, "ok")

    task = asyncio.create_task(human())
    request = await manager.request(
        **request_kwargs(kind=InterventionKind.APPROVAL_REQUIRED, pending_action="click Confirm")  # type: ignore[arg-type]
    )
    await task
    assert request.status is InterventionStatus.APPROVED
    assert manager.session.state is SessionState.AUTOMATION

    manager2, _ = make_manager(tmp_path / "b", FakeSurface(), attended=True)

    async def human2() -> None:
        while not manager2.open_interventions():
            await asyncio.sleep(0.01)
        request = manager2.open_interventions()[0]
        with pytest.raises(OperatorError, match="needs control transfer"):
            manager2.approve(request.id)
        manager2.abort(request.id, "no")

    task = asyncio.create_task(human2())
    request = await manager2.request(**request_kwargs())  # type: ignore[arg-type]
    await task
    assert (
        request.status is InterventionStatus.ABORTED
        and manager2.session.state is SessionState.ABORTED
    )


async def test_timeout_when_nobody_responds(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path, FakeSurface(), attended=True, timeout_s=0.05)
    request = await manager.request(**request_kwargs())  # type: ignore[arg-type]
    assert request.status is InterventionStatus.TIMED_OUT
    assert manager.session.state is SessionState.ABORTED


async def test_browser_actions_recorded_only_under_human_control(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from app.escalation.manager import HumanActionRecord

    manager, _ = make_manager(tmp_path, FakeSurface(), attended=True)
    record = HumanActionRecord(timestamp=datetime.now(UTC), source="browser", action="click")
    manager.record_browser_action(record)  # no intervention: dropped
    assert not manager.interventions

    async def human() -> None:
        while not manager.open_interventions():
            await asyncio.sleep(0.01)
        request = manager.open_interventions()[0]
        manager.take_control(request.id)
        manager.record_browser_action(record)
        manager.release(request.id)

    task = asyncio.create_task(human())
    request = await manager.request(**request_kwargs())  # type: ignore[arg-type]
    await task
    assert [a.source for a in request.human_action_log] == ["browser"]
