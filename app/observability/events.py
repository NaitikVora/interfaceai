"""Structured run events. Every event is redacted before it reaches any sink."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field

from app.safety.redaction import Redactor


class EventType(StrEnum):
    RUN_STARTED = "RUN_STARTED"
    OBSERVATION_CREATED = "OBSERVATION_CREATED"
    LLM_DECISION = "LLM_DECISION"
    LLM_RESPONSE_REJECTED = "LLM_RESPONSE_REJECTED"
    ACTION_REQUESTED = "ACTION_REQUESTED"
    POLICY_DECISION = "POLICY_DECISION"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    TARGET_RESOLVED = "TARGET_RESOLVED"
    TARGET_UNRESOLVED = "TARGET_UNRESOLVED"
    ACTION_EXECUTED = "ACTION_EXECUTED"
    ACTION_FAILED = "ACTION_FAILED"
    VALUE_EXTRACTED = "VALUE_EXTRACTED"
    CHECKPOINT_VERIFIED = "CHECKPOINT_VERIFIED"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    RULE_MATCHED = "RULE_MATCHED"
    RECOVERY_APPLIED = "RECOVERY_APPLIED"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    STEP_RECORDED = "STEP_RECORDED"
    ARTIFACT_SAVED = "ARTIFACT_SAVED"
    ESCALATION_CREATED = "ESCALATION_CREATED"
    HUMAN_CONTROL_GRANTED = "HUMAN_CONTROL_GRANTED"
    HUMAN_ACTION = "HUMAN_ACTION"
    HUMAN_CONTROL_RELEASED = "HUMAN_CONTROL_RELEASED"
    ESCALATION_RESOLVED = "ESCALATION_RESOLVED"
    SESSION_STATE_CHANGED = "SESSION_STATE_CHANGED"
    REPLAY_STARTED = "REPLAY_STARTED"
    REPLAY_COMPLETED = "REPLAY_COMPLETED"
    DISCOVERY_COMPLETED = "DISCOVERY_COMPLETED"
    RUN_FAILED = "RUN_FAILED"


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    run_id: str
    event_type: EventType
    capability_id: str | None = None
    step_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class JsonlEventSink:
    """Append-only JSON lines file; one event per line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), separators=(",", ":")) + "\n")


class StructlogSink:
    """Mirrors events to the process logger for a live console view."""

    def __init__(self) -> None:
        self._log = structlog.get_logger("run")

    def emit(self, event: Event) -> None:
        self._log.info(
            event.event_type.value,
            run_id=event.run_id,
            step_id=event.step_id,
            **{k: v for k, v in event.data.items() if k not in {"run_id", "step_id", "event"}},
        )


class EventLog:
    """Per-run event emitter. Redacts, fans out to sinks and keeps an in-memory copy."""

    def __init__(
        self,
        run_id: str,
        *,
        redactor: Redactor,
        sinks: list[EventSink],
        capability_id: str | None = None,
    ) -> None:
        self.run_id = run_id
        self.capability_id = capability_id
        self._redactor = redactor
        self._sinks = sinks
        self.events: list[Event] = []

    def emit(self, event_type: EventType, *, step_id: str | None = None, **data: Any) -> Event:
        event = Event(
            timestamp=datetime.now(UTC),
            run_id=self.run_id,
            event_type=event_type,
            capability_id=self.capability_id,
            step_id=step_id,
            data=self._redactor.redact(data),
        )
        self.events.append(event)
        for sink in self._sinks:
            sink.emit(event)
        return event

    def count(self, event_type: EventType) -> int:
        return sum(1 for e in self.events if e.event_type is event_type)
