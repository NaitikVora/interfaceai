"""Intervention requests and the handoff protocol.

Lifecycle of one intervention (``EscalationManager.request`` blocks the run while it is open)::

    PENDING --take_control--> HUMAN_CONTROL --release--> RELEASED  (automation resumes, verifies)
    PENDING --approve--> APPROVED                    (automation performs the gated action itself)
    PENDING | HUMAN_CONTROL --abort--> ABORTED                       (run ends as ESCALATED)
    PENDING --timeout--> TIMED_OUT / no operator attached --> UNATTENDED

Human actions taken through the console flow through the same ``OwnedSurface`` as automation,
so ownership is enforced in code and every action is recorded in ``human_action_log``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.artifacts.schema import ActionType, TargetSpec, ValueType
from app.automation.actions import ActionRequest, perform_action
from app.automation.browser import SurfaceActionError
from app.automation.locators import LocatorResolver, ResolutionError
from app.automation.surface import ComputerSurface, Observation, ObservationLimits
from app.escalation.session import Actor, ControlViolationError, OwnedSurface, SessionController
from app.observability.events import EventLog, EventType
from app.observability.evidence import EvidenceStore


class InterventionKind(StrEnum):
    APPROVAL_REQUIRED = "approval_required"
    TARGET_UNRESOLVED = "target_unresolved"
    CHECKPOINT_FAILED = "checkpoint_failed"
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    AGENT_STUCK = "agent_stuck"
    AGENT_REQUESTED = "agent_requested"
    OPERATOR_PAUSE = "operator_pause"


class InterventionStatus(StrEnum):
    PENDING = "pending"
    HUMAN_CONTROL = "human_control"
    APPROVED = "approved"
    RELEASED = "released"
    ABORTED = "aborted"
    TIMED_OUT = "timed_out"
    UNATTENDED = "unattended"


TERMINAL_STATUSES = frozenset(
    {
        InterventionStatus.APPROVED,
        InterventionStatus.RELEASED,
        InterventionStatus.ABORTED,
        InterventionStatus.TIMED_OUT,
        InterventionStatus.UNATTENDED,
    }
)


class HumanActionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    source: str = Field(description="console (proxied through the surface) or browser (direct)")
    action: str
    target: str | None = None
    value: str | None = Field(default=None, description="Redacted; secrets never stored")
    url_before: str | None = None
    url_after: str | None = None
    ok: bool = True
    error: str | None = None


class InterventionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    capability_id: str | None
    capability_name: str | None
    goal: str
    kind: InterventionKind
    reason: str
    current_step: str | None
    step_index: int | None
    pending_action: str | None = Field(
        default=None, description="Human-readable action awaiting approval, if any"
    )
    screenshot_ref: str | None
    current_url: str
    page_excerpt: str
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    status: InterventionStatus = InterventionStatus.PENDING
    resolved_at: datetime | None = None
    operator_note: str | None = None
    human_action_log: list[HumanActionRecord] = Field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status not in TERMINAL_STATUSES


class OperatorAction(BaseModel):
    """An action a human submits through the console. ``ref`` refers to the latest observation."""

    model_config = ConfigDict(extra="forbid")

    action: ActionType
    ref: str | None = None
    target: TargetSpec | None = None
    value: str | None = None
    key: str | None = None
    url: str | None = None


class OperatorError(RuntimeError):
    """Invalid operator request (wrong state, unknown intervention, unresolvable target)."""


class EscalationManager:
    def __init__(
        self,
        *,
        session: SessionController,
        surface: ComputerSurface,
        events: EventLog,
        evidence: EvidenceStore,
        observation_limits: ObservationLimits,
        poll_interval_s: float,
        timeout_s: float,
        attended: bool,
        secret_values: frozenset[str] = frozenset(),
        on_created: Callable[[InterventionRequest], None] | None = None,
    ) -> None:
        self.session = session
        self._surface = surface
        self._human_surface = OwnedSurface(surface, session, Actor.HUMAN)
        self._resolver = LocatorResolver(surface.backend, poll_interval_s=poll_interval_s)
        self._events = events
        self._evidence = evidence
        self._limits = observation_limits
        self._timeout_s = timeout_s
        self.attended = attended
        self._secret_values = secret_values
        self._on_created = on_created
        self.interventions: dict[str, InterventionRequest] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self._latest_observation: Observation | None = None
        self.pause_requested = False
        self.proxied_action_in_progress = False
        """True while a console action runs, so the browser recorder does not double-record."""

    # ------------------------------------------------------------------ automation side
    async def request(
        self,
        *,
        kind: InterventionKind,
        reason: str,
        goal: str,
        capability_id: str | None,
        capability_name: str | None,
        step_id: str | None,
        step_index: int | None,
        pending_action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> InterventionRequest:
        """Pause automation, publish the request and block until a human resolves it."""
        number = len(self.interventions) + 1
        intervention_id = f"int-{number:03d}"
        screenshot_ref = self._evidence.save_png(
            f"intervention-{number:02d}-{kind.value}", await self._surface.screenshot()
        )
        page_text = await self._surface.page_text()
        request = InterventionRequest(
            id=intervention_id,
            run_id=self._events.run_id,
            capability_id=capability_id,
            capability_name=capability_name,
            goal=goal,
            kind=kind,
            reason=reason,
            current_step=step_id,
            step_index=step_index,
            pending_action=pending_action,
            screenshot_ref=screenshot_ref,
            current_url=await self._surface.current_url(),
            page_excerpt=page_text[:600],
            details=details or {},
            created_at=datetime.now(UTC),
        )
        self.interventions[intervention_id] = request
        self._waiters[intervention_id] = asyncio.Event()
        self.session.escalate()
        self._events.emit(
            EventType.ESCALATION_CREATED,
            step_id=step_id,
            intervention_id=intervention_id,
            kind=kind.value,
            reason=reason,
            pending_action=pending_action,
            screenshot=screenshot_ref,
            attended=self.attended,
        )
        self._persist(request)
        if self._on_created is not None:
            self._on_created(request)

        if not self.attended:
            self._finish(request, InterventionStatus.UNATTENDED, "no operator attached")
            self.session.abort()
            return request

        try:
            await asyncio.wait_for(self._waiters[intervention_id].wait(), timeout=self._timeout_s)
        except TimeoutError:
            if request.is_open:
                self._finish(request, InterventionStatus.TIMED_OUT, "operator did not respond")
                self.session.abort()
                return request

        if request.status is InterventionStatus.RELEASED:
            self.session.resume_automation()
        return request

    def latest_observation(self) -> Observation | None:
        return self._latest_observation

    # ------------------------------------------------------------------ operator side
    def get(self, intervention_id: str) -> InterventionRequest:
        try:
            return self.interventions[intervention_id]
        except KeyError as exc:
            raise OperatorError(f"unknown intervention {intervention_id}") from exc

    def open_interventions(self) -> list[InterventionRequest]:
        return [i for i in self.interventions.values() if i.is_open]

    def take_control(self, intervention_id: str) -> InterventionRequest:
        request = self.get(intervention_id)
        if request.status is not InterventionStatus.PENDING:
            raise OperatorError(f"intervention {intervention_id} is {request.status}, not pending")
        self.session.grant_human_control()
        request.status = InterventionStatus.HUMAN_CONTROL
        self._events.emit(
            EventType.HUMAN_CONTROL_GRANTED,
            step_id=request.current_step,
            intervention_id=intervention_id,
        )
        self._persist(request)
        return request

    async def observe_for_operator(self, intervention_id: str) -> Observation:
        self.get(intervention_id)
        self._latest_observation = await self._surface.observe(self._limits)
        return self._latest_observation

    async def perform_human_action(
        self, intervention_id: str, action: OperatorAction
    ) -> HumanActionRecord:
        request = self.get(intervention_id)
        if request.status is not InterventionStatus.HUMAN_CONTROL:
            raise OperatorError("take control before performing actions")
        target = self._target_for(action)
        action_request = ActionRequest(
            action=action.action,
            target=target,
            value=action.value,
            key=action.key,
            url=action.url,
            output="operator_read" if action.action is ActionType.EXTRACT else None,
            value_type=ValueType.STRING if action.action is ActionType.EXTRACT else None,
        )
        url_before = await self._surface.current_url()
        record = HumanActionRecord(
            timestamp=datetime.now(UTC),
            source="console",
            action=action.action.value,
            target=target.summary() if target else None,
            value=self._redact_value(action.value),
            url_before=url_before,
        )
        self.proxied_action_in_progress = True
        try:
            resolved = None
            if action_request.needs_target and target is not None:
                resolved = await self._resolver.resolve(
                    target,
                    timeout_s=3.0,
                    require_enabled=action_request.requires_enabled_target,
                )
            outcome = await perform_action(self._human_surface, action_request, resolved)
            await self._surface.wait_for_settled(3.0)
            if outcome.extracted_text is not None:
                record = record.model_copy(update={"value": outcome.extracted_text[:200]})
        except (ResolutionError, SurfaceActionError, ControlViolationError, ValueError) as exc:
            record = record.model_copy(update={"ok": False, "error": str(exc)[:300]})
        finally:
            self.proxied_action_in_progress = False
        record = record.model_copy(update={"url_after": await self._surface.current_url()})
        request.human_action_log.append(record)
        self._events.emit(
            EventType.HUMAN_ACTION,
            step_id=request.current_step,
            intervention_id=intervention_id,
            **record.model_dump(mode="json"),
        )
        self._persist(request)
        if not record.ok:
            raise OperatorError(record.error or "action failed")
        return record

    def record_browser_action(self, record: HumanActionRecord) -> None:
        """Record an action the human performed directly in a headed browser window."""
        active = next(
            (
                i
                for i in self.interventions.values()
                if i.status is InterventionStatus.HUMAN_CONTROL
            ),
            None,
        )
        if active is None:
            return
        active.human_action_log.append(record)
        self._events.emit(
            EventType.HUMAN_ACTION,
            step_id=active.current_step,
            intervention_id=active.id,
            **record.model_dump(mode="json"),
        )
        self._persist(active)

    def release(self, intervention_id: str, note: str | None = None) -> InterventionRequest:
        request = self.get(intervention_id)
        if request.status is not InterventionStatus.HUMAN_CONTROL:
            raise OperatorError("only an intervention under human control can be released")
        self.session.release_human_control()
        self._events.emit(
            EventType.HUMAN_CONTROL_RELEASED,
            step_id=request.current_step,
            intervention_id=intervention_id,
            human_actions=len(request.human_action_log),
        )
        self._finish(request, InterventionStatus.RELEASED, note)
        return request

    def approve(self, intervention_id: str, note: str | None = None) -> InterventionRequest:
        request = self.get(intervention_id)
        if request.status is not InterventionStatus.PENDING:
            raise OperatorError("only a pending intervention can be approved")
        if request.kind is not InterventionKind.APPROVAL_REQUIRED:
            raise OperatorError("this intervention needs control transfer, not approval")
        self.session.approve_and_continue()
        self._finish(request, InterventionStatus.APPROVED, note)
        return request

    def abort(self, intervention_id: str, note: str | None = None) -> InterventionRequest:
        request = self.get(intervention_id)
        if not request.is_open:
            raise OperatorError(f"intervention {intervention_id} is already {request.status}")
        self.session.abort()
        self._finish(request, InterventionStatus.ABORTED, note)
        return request

    def request_pause(self) -> None:
        """Ask automation to hand over at the next step boundary."""
        self.pause_requested = True

    # ------------------------------------------------------------------ internals
    def _target_for(self, action: OperatorAction) -> TargetSpec | None:
        if action.target is not None:
            return action.target
        if action.ref is None:
            return None
        if self._latest_observation is None:
            raise OperatorError("observe the page before referring to a control")
        control = self._latest_observation.control(action.ref)
        if control is None:
            raise OperatorError(f"unknown control ref {action.ref}")
        # The operator picked this exact node from a fresh observation, so address it
        # structurally: semantic strategies may legitimately be ambiguous here (that is often
        # why a human was called in the first place).
        return TargetSpec(description=control.describe(), css=control.css, xpath=control.xpath)

    def _redact_value(self, value: str | None) -> str | None:
        if value is None:
            return None
        return "[REDACTED]" if value in self._secret_values else value[:200]

    def _finish(
        self, request: InterventionRequest, status: InterventionStatus, note: str | None
    ) -> None:
        request.status = status
        request.resolved_at = datetime.now(UTC)
        request.operator_note = note
        self._events.emit(
            EventType.ESCALATION_RESOLVED,
            step_id=request.current_step,
            intervention_id=request.id,
            status=status.value,
            note=note,
            human_actions=len(request.human_action_log),
        )
        self._persist(request)
        waiter = self._waiters.get(request.id)
        if waiter is not None:
            waiter.set()

    def _persist(self, request: InterventionRequest) -> None:
        self._evidence.save_json(f"{request.id}", request.model_dump(mode="json"))
