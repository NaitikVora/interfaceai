"""Per-run wiring shared by the CLI, the tests and the demo scripts.

A run owns: an id, a redactor, an event log (JSONL + console), an evidence store, the session
ownership controller and the escalation manager bound to the live surface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.automation.surface import ComputerSurface, ObservationLimits
from app.config import Settings
from app.escalation.manager import EscalationManager, InterventionRequest
from app.escalation.session import Actor, OwnedSurface, SessionController, SessionState
from app.observability.events import EventLog, EventSink, EventType, JsonlEventSink, StructlogSink
from app.observability.evidence import EvidenceKind, EvidenceStore, new_run_id, run_directory
from app.observability.logging import process_redactor
from app.safety.redaction import Redactor


@dataclass
class RunContext:
    run_id: str
    settings: Settings
    redactor: Redactor
    events: EventLog
    evidence: EvidenceStore
    session: SessionController
    escalation: EscalationManager
    automation_surface: OwnedSurface

    @property
    def limits(self) -> ObservationLimits:
        return observation_limits(self.settings)


def observation_limits(settings: Settings) -> ObservationLimits:
    return ObservationLimits(
        max_text_chars=settings.obs_max_text_chars,
        max_controls=settings.obs_max_controls,
        max_tables=settings.obs_max_tables,
        max_table_rows=settings.obs_max_table_rows,
        max_table_cols=settings.obs_max_table_cols,
        max_cell_chars=settings.obs_max_cell_chars,
    )


def build_run_context(
    *,
    kind: EvidenceKind,
    settings: Settings,
    surface: ComputerSurface,
    attended: bool,
    secrets: list[str] | None = None,
    run_id: str | None = None,
    evidence_root: Path | None = None,
    console_logging: bool = True,
    on_intervention: Callable[[InterventionRequest], None] | None = None,
) -> RunContext:
    run_id = run_id or new_run_id(kind)
    redactor = Redactor(secrets or [])
    for secret in secrets or []:
        process_redactor.add_secret(secret)
    if settings.llm_api_key is not None:
        redactor.add_secret(settings.llm_api_key.get_secret_value())
        process_redactor.add_secret(settings.llm_api_key.get_secret_value())

    run_dir = run_directory(evidence_root or settings.evidence_dir, kind, run_id)
    evidence = EvidenceStore(run_dir, redactor)
    sinks: list[EventSink] = [JsonlEventSink(evidence.events_path())]
    if console_logging:
        sinks.append(StructlogSink())
    events = EventLog(run_id, redactor=redactor, sinks=sinks)

    def on_transition(previous: SessionState, current: SessionState) -> None:
        events.emit(
            EventType.SESSION_STATE_CHANGED,
            previous=previous.value,
            current=current.value,
            control_owner=session.control_owner.value if session.control_owner else None,
        )

    session = SessionController(run_id, on_transition=on_transition)
    escalation = EscalationManager(
        session=session,
        surface=surface,
        events=events,
        evidence=evidence,
        observation_limits=observation_limits(settings),
        poll_interval_s=settings.replay_poll_interval_s,
        timeout_s=settings.escalation_timeout_s,
        attended=attended,
        secret_values=frozenset(secrets or []),
        on_created=on_intervention,
    )
    return RunContext(
        run_id=run_id,
        settings=settings,
        redactor=redactor,
        events=events,
        evidence=evidence,
        session=session,
        escalation=escalation,
        automation_surface=OwnedSurface(surface, session, Actor.AUTOMATION),
    )
