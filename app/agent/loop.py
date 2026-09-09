"""The discovery loop: OBSERVE -> DECIDE (LLM) -> ACT -> VERIFY, recorded as artifact steps.

The model proposes; the loop validates every proposal against the policy engine, resolves the
chosen control with the same resolver replay uses, performs the action, verifies the outcome
(extraction must parse, the page must settle) and records a parameterized step. Known runtime
states (interstitials, business outcomes) are handled by the rule engine before the model is
consulted, so the recording contains only the goal path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from app.agent.models import AgentAction, ControlRef, Decision, TableCellRef
from app.agent.observation import AgentContext, render_observation
from app.agent.planner import Planner, PlannerError
from app.agent.recorder import DiscoveryInput, Recorder, verify_target_spec
from app.artifacts.params import ValueParseError, find_placeholders, parse_value, substitute
from app.artifacts.profile import ApplicationProfile
from app.artifacts.schema import (
    ActionType,
    CapabilityArtifact,
    ClickRecovery,
    OutcomeCategory,
    RetryStepRecovery,
    TableCellSpec,
    TargetSpec,
)
from app.artifacts.serializer import save_artifact
from app.automation.actions import ActionRequest, perform_action
from app.automation.browser import SurfaceActionError
from app.automation.locators import LocatorResolver, ResolutionError
from app.automation.surface import ComputerSurface, Observation
from app.automation.waits import Deadline
from app.config import Settings
from app.escalation.manager import EscalationManager, InterventionKind, InterventionStatus
from app.observability.events import EventLog, EventType
from app.observability.evidence import EvidenceStore
from app.replay.checkpoints import ConditionContext
from app.replay.errors import Failure
from app.replay.rules import RuleEngine
from app.runtime import observation_limits
from app.safety.policy import PolicyEngine
from app.safety.redaction import Redactor

MAX_CONSECUTIVE_REJECTIONS = 4
SETTLE_TIMEOUT_S = 3.0
TARGET_RESOLVE_TIMEOUT_S = 3.0
RECOVERY_CLICK_TIMEOUT_S = 3.0


class DiscoveryStatus(StrEnum):
    COMPLETED = "COMPLETED"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: DiscoveryStatus
    run_id: str
    goal: str
    artifact_path: str | None = None
    artifact_id: str | None = None
    steps_recorded: int = 0
    llm_calls: int = 0
    extracted: dict[str, str] = Field(default_factory=dict)
    failure: Failure | None = None
    evidence_dir: str
    duration_s: float

    def render(self) -> str:
        lines = [self.status.value]
        if self.artifact_path:
            lines.append(f"  artifact: {self.artifact_path}")
        if self.extracted:
            lines.append("  extracted: " + ", ".join(f"{k}={v}" for k, v in self.extracted.items()))
        if self.failure is not None:
            lines.append(
                f"  {self.failure.category.value} / {self.failure.code}: {self.failure.message}"
            )
        lines.append(
            f"  steps recorded: {self.steps_recorded}  llm calls: {self.llm_calls}  "
            f"duration: {self.duration_s:.1f}s"
        )
        lines.append(f"  evidence: {self.evidence_dir}")
        return "\n".join(lines)


class _Stop(Exception):  # noqa: N818 - control flow, carries the terminal result
    def __init__(self, status: DiscoveryStatus, failure: Failure | None = None) -> None:
        self.status = status
        self.failure = failure
        super().__init__(status)


@dataclass
class _Ctx:
    goal: str
    inputs: list[DiscoveryInput]
    values: dict[str, str]
    sensitive: frozenset[str]
    extracted: dict[str, str] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    echo_outputs: dict[str, str] = field(default_factory=dict)
    summary: str | None = None
    fingerprints: list[str] = field(default_factory=list)
    rejections: int = 0
    step_index: int = 0


class AgentLoop:
    def __init__(
        self,
        *,
        surface: ComputerSurface,
        planner: Planner,
        policy: PolicyEngine,
        profile: ApplicationProfile,
        settings: Settings,
        events: EventLog,
        evidence: EvidenceStore,
        escalation: EscalationManager,
        redactor: Redactor,
        artifacts_dir: Path,
    ) -> None:
        self._surface = surface
        self._planner = planner
        self._policy = policy
        self._profile = profile
        self._settings = settings
        self._events = events
        self._evidence = evidence
        self._escalation = escalation
        self._redactor = redactor
        self._artifacts_dir = artifacts_dir
        self._resolver = LocatorResolver(
            surface.backend, poll_interval_s=settings.replay_poll_interval_s
        )
        self._rules = RuleEngine(profile.conditions)
        self._limits = observation_limits(settings)

    # ------------------------------------------------------------------ public API
    async def discover(
        self,
        *,
        goal: str,
        entry_url: str,
        inputs: list[DiscoveryInput],
        capability_name: str,
    ) -> DiscoveryResult:
        started = time.monotonic()
        deadline = Deadline(self._settings.agent_max_runtime_s)
        ctx = _Ctx(
            goal=goal,
            inputs=inputs,
            values={i.name: i.value for i in inputs},
            sensitive=frozenset(i.name for i in inputs if i.sensitive),
        )
        for item in inputs:
            if item.sensitive:
                self._redactor.add_secret(item.value)
        recorder = Recorder(
            inputs=inputs,
            base_url=_base_url(entry_url),
            profile=self._profile,
            settings=self._settings,
        )
        self._events.emit(
            EventType.RUN_STARTED,
            mode="discovery",
            goal=goal,
            entry_url=entry_url,
            inputs=[i.name for i in inputs],
            model=self._planner.model_name,
            max_steps=self._settings.agent_max_steps,
        )

        status = DiscoveryStatus.COMPLETED
        failure: Failure | None = None
        artifact: CapabilityArtifact | None = None
        artifact_path: Path | None = None
        try:
            await self._enter(ctx, recorder, entry_url)
            observation = await self._observe(ctx)
            while True:
                if deadline.expired():
                    raise _Stop(
                        DiscoveryStatus.FAILED,
                        self._failure("DISCOVERY_TIMEOUT", "runtime budget exhausted"),
                    )
                if ctx.step_index >= self._settings.agent_max_steps:
                    raise _Stop(
                        DiscoveryStatus.FAILED,
                        self._failure("STEP_BUDGET_EXHAUSTED", "max steps reached"),
                    )
                observation = await self._handle_known_states(ctx, observation)
                observation = await self._check_pause(ctx, observation)
                ctx.step_index += 1
                decision = await self._decide(ctx, observation)
                if self._is_stuck(ctx, observation, decision):
                    observation = await self._escalate(
                        ctx,
                        kind=InterventionKind.AGENT_STUCK,
                        reason=f"the model repeated the same action on an unchanged screen "
                        f"{self._settings.agent_stuck_repeats} times",
                    )
                    continue
                if decision.action is AgentAction.FINISH:
                    ctx.summary = decision.summary
                    ctx.echo_outputs = dict(decision.outputs or {})
                    break
                observation = await self._act(ctx, recorder, decision, observation)
            artifact = recorder.build_artifact(
                name=capability_name,
                goal=goal,
                description=ctx.summary or goal,
                run_id=self._events.run_id,
                llm_model=self._planner.model_name,
                llm_decisions=len(self._planner.calls),
                duration_s=time.monotonic() - started,
                final_observation=observation,
                echo_outputs=ctx.echo_outputs,
            )
            artifact_path = save_artifact(artifact, self._artifacts_dir)
            self._evidence.save_json(
                "artifact", artifact.model_dump(mode="json", exclude_none=True)
            )
            self._events.emit(
                EventType.ARTIFACT_SAVED,
                path=str(artifact_path),
                artifact_id=artifact.artifact_id,
                steps=len(artifact.steps),
            )
        except _Stop as stop:
            status, failure = stop.status, stop.failure
        except PlannerError as exc:
            status = DiscoveryStatus.FAILED
            failure = self._failure("LLM_UNAVAILABLE", str(exc))

        return await self._finish(
            ctx,
            status=status,
            failure=failure,
            artifact=artifact,
            artifact_path=artifact_path,
            started=started,
        )

    # ------------------------------------------------------------------ phases
    async def _enter(self, ctx: _Ctx, recorder: Recorder, entry_url: str) -> None:
        request = ActionRequest(action=ActionType.NAVIGATE, url=entry_url)
        decision = self._policy.evaluate(request, current_url=entry_url)
        if not decision.allowed:
            raise _Stop(
                DiscoveryStatus.FAILED,
                self._failure("POLICY_VIOLATION", f"entry URL rejected: {decision.reason}"),
            )
        before = await self._surface.observe(self._limits)
        await self._surface.navigate(entry_url)
        await self._surface.wait_for_settled(SETTLE_TIMEOUT_S)
        after = await self._surface.observe(self._limits)
        ctx.step_index += 1
        synthetic = Decision(
            reasoning="Open the entry URL",
            action=AgentAction.NAVIGATE,
            url=entry_url,
            confidence=1.0,
        )
        step = recorder.record(
            index=ctx.step_index,
            decision=synthetic,
            request=request,
            spec=None,
            before=before,
            after=after,
            risk=decision.risk_class,
            extracted_text=None,
        )
        self._events.emit(
            EventType.STEP_RECORDED, step_id=step.id, action="navigate", url=entry_url
        )
        ctx.history.append(f"{ctx.step_index}. navigate {entry_url} -> ok")
        await self._snapshot(ctx.step_index, "navigate")

    async def _observe(self, ctx: _Ctx) -> Observation:
        await self._surface.wait_for_settled(SETTLE_TIMEOUT_S)
        observation = await self._surface.observe(self._limits)
        self._events.emit(
            EventType.OBSERVATION_CREATED,
            step_id=None,
            url=observation.url,
            headings=observation.headings,
            controls=len(observation.controls),
            tables=len(observation.tables),
            text_chars=len(observation.text),
        )
        return observation

    async def _handle_known_states(self, ctx: _Ctx, observation: Observation) -> Observation:
        """Deterministically handle interstitials; stop on business outcomes / fatal states."""
        cond_ctx = ConditionContext(self._surface, self._resolver, ctx.values)
        match = await self._rules.first_match(cond_ctx)
        if match is None:
            return observation
        rule = match.rule
        self._events.emit(
            EventType.RULE_MATCHED, rule=rule.id, category=rule.category.value, code=rule.code
        )
        if rule.category is OutcomeCategory.BUSINESS_OUTCOME:
            raise _Stop(
                DiscoveryStatus.BUSINESS_OUTCOME,
                self._failure(
                    rule.code,
                    match.message or rule.description,
                    category=OutcomeCategory.BUSINESS_OUTCOME,
                    observed=match.observed,
                ),
            )
        if rule.category is OutcomeCategory.HARD_FAILURE:
            raise _Stop(
                DiscoveryStatus.FAILED,
                self._failure(
                    rule.code, match.message or rule.description, observed=match.observed
                ),
            )
        recovery = rule.recovery
        assert recovery is not None
        match recovery:
            case ClickRecovery(target=target):
                resolved = await self._resolver.resolve(
                    target, timeout_s=RECOVERY_CLICK_TIMEOUT_S, require_enabled=True
                )
                await perform_action(
                    self._surface, ActionRequest(action=ActionType.CLICK, target=target), resolved
                )
                self._events.emit(
                    EventType.RECOVERY_APPLIED,
                    rule=rule.id,
                    recovery="click",
                    target=target.summary(),
                )
            case RetryStepRecovery():
                self._events.emit(EventType.RECOVERY_APPLIED, rule=rule.id, recovery="re-observe")
        ctx.history.append(f"-- system handled '{rule.description}' ({rule.code})")
        return await self._observe(ctx)

    def _is_stuck(self, ctx: _Ctx, observation: Observation, decision: Decision) -> bool:
        """Stuck = the same proposal on an unchanged screen, ``agent_stuck_repeats`` times."""
        signature = (
            f"{observation.fingerprint()}::{decision.action}:{decision.target}:"
            f"{decision.value}:{decision.url}:{decision.key}"
        )
        ctx.fingerprints.append(signature)
        repeats = self._settings.agent_stuck_repeats
        if len(ctx.fingerprints) < repeats or len(set(ctx.fingerprints[-repeats:])) != 1:
            return False
        ctx.fingerprints.clear()
        return True

    async def _check_pause(self, ctx: _Ctx, observation: Observation) -> Observation:
        if not self._escalation.pause_requested:
            return observation
        self._escalation.pause_requested = False
        return await self._escalate(
            ctx, kind=InterventionKind.OPERATOR_PAUSE, reason="operator requested a pause"
        )

    async def _decide(self, ctx: _Ctx, observation: Observation) -> Decision:
        rendered = render_observation(
            observation,
            AgentContext(
                step=ctx.step_index,
                max_steps=self._settings.agent_max_steps,
                inputs=ctx.values,
                sensitive=ctx.sensitive,
                extracted=ctx.extracted,
                history=ctx.history[-self._settings.agent_max_history :],
                notes=ctx.notes,
            ),
            self._redactor,
            max_chars=self._settings.obs_max_text_chars * 4,
        )
        ctx.notes.clear()
        decision = await self._planner.decide(
            step=ctx.step_index, goal=ctx.goal, observation_text=rendered
        )
        call = self._planner.calls[-1]
        self._events.emit(
            EventType.LLM_DECISION,
            step_id=None,
            step=ctx.step_index,
            model=call.model,
            attempts=call.attempts,
            prompt_chars=call.prompt_chars,
            usage=call.usage,
            action=decision.action.value,
            target=decision.target.model_dump(exclude_none=True) if decision.target else None,
            value=decision.value,
            url=decision.url,
            output_name=decision.output_name,
            expected_heading=decision.expected_heading,
            confidence=decision.confidence,
            reasoning=decision.reasoning,
        )
        self._evidence.save_json(
            f"llm-call-{ctx.step_index:02d}",
            {
                "step": ctx.step_index,
                "model": call.model,
                "attempts": call.attempts,
                "usage": call.usage,
                "prompt": rendered,
                "raw_response": call.raw_response,
            },
        )
        return decision

    async def _act(
        self, ctx: _Ctx, recorder: Recorder, decision: Decision, observation: Observation
    ) -> Observation:
        if decision.action is AgentAction.ESCALATE:
            return await self._escalate(
                ctx,
                kind=InterventionKind.AGENT_REQUESTED,
                reason=decision.reason or "model request",
            )
        if decision.action is AgentAction.WAIT:
            await self._surface.wait_for_settled(self._settings.agent_max_wait_s)
            ctx.history.append(f"{ctx.step_index}. wait -> ok")
            return await self._observe(ctx)

        try:
            request, spec = await self._prepare_request(ctx, decision, observation)
        except _Rejected as rejected:
            return await self._reject(ctx, rejected.reason, observation)

        policy = self._policy.evaluate(
            request,
            current_url=observation.url,
            value_is_secret=self._value_is_secret(ctx, decision.value),
        )
        self._events.emit(
            EventType.ACTION_REQUESTED,
            action=request.action.value,
            target=spec.summary() if spec else None,
            value=request.value,
            url=request.url,
        )
        self._events.emit(EventType.POLICY_DECISION, **policy.as_event_data())
        if not policy.allowed:
            self._events.emit(EventType.POLICY_VIOLATION, **policy.as_event_data())
            return await self._reject(ctx, f"rejected by policy: {policy.reason}", observation)
        if policy.requires_confirmation:
            intervention = await self._escalation.request(
                kind=InterventionKind.APPROVAL_REQUIRED,
                reason=policy.reason,
                goal=ctx.goal,
                capability_id=None,
                capability_name=None,
                step_id=None,
                step_index=ctx.step_index,
                pending_action=f"{request.action.value} {spec.summary() if spec else request.url}",
            )
            if intervention.status is InterventionStatus.RELEASED:
                ctx.history.append(f"{ctx.step_index}. {request.action.value} performed by human")
                after = await self._observe(ctx)
                recorder.record(
                    index=ctx.step_index,
                    decision=decision,
                    request=request,
                    spec=spec,
                    before=observation,
                    after=after,
                    risk=policy.risk_class,
                    extracted_text=None,
                )
                return after
            if intervention.status is not InterventionStatus.APPROVED:
                raise _Stop(
                    DiscoveryStatus.ESCALATED,
                    self._failure(
                        "CONFIRMATION_REQUIRED",
                        f"irreversible action not approved ({intervention.status.value})",
                    ),
                )

        resolved = None
        if request.needs_target and spec is not None:
            try:
                resolved = await self._resolver.resolve(
                    spec,
                    timeout_s=TARGET_RESOLVE_TIMEOUT_S,
                    require_enabled=request.requires_enabled_target,
                )
            except ResolutionError as exc:
                return await self._reject(
                    ctx,
                    f"target could not be resolved ({exc.code.value}): {spec.summary()}",
                    observation,
                )
        try:
            outcome = await perform_action(self._surface, request, resolved)
        except SurfaceActionError as exc:
            return await self._reject(ctx, f"action failed on the surface: {exc}", observation)

        extracted_text = outcome.extracted_text
        if request.action is ActionType.EXTRACT:
            assert request.output and request.value_type
            try:
                parsed = parse_value(extracted_text or "", request.value_type)
            except ValueParseError as exc:
                return await self._reject(
                    ctx, f"extracted text could not be parsed: {exc}", observation
                )
            ctx.extracted[request.output] = str(parsed)
            self._events.emit(
                EventType.VALUE_EXTRACTED,
                output=request.output,
                value=str(parsed),
                value_type=request.value_type.value,
            )

        self._events.emit(
            EventType.ACTION_EXECUTED,
            action=request.action.value,
            strategy=outcome.strategy.value if outcome.strategy else None,
        )
        after = await self._observe(ctx)
        step = recorder.record(
            index=ctx.step_index,
            decision=decision,
            request=request,
            spec=spec,
            before=observation,
            after=after,
            risk=policy.risk_class,
            extracted_text=extracted_text,
        )
        self._events.emit(
            EventType.STEP_RECORDED,
            step_id=step.id,
            action=step.action.value,
            target=spec.summary() if spec else None,
            postconditions=len(step.postconditions),
        )
        ctx.rejections = 0
        ctx.history.append(self._history_line(ctx, decision, spec, extracted_text))
        await self._snapshot(ctx.step_index, request.action.value)
        return after

    # ------------------------------------------------------------------ helpers
    async def _prepare_request(
        self, ctx: _Ctx, decision: Decision, observation: Observation
    ) -> tuple[ActionRequest, TargetSpec | None]:
        action = ActionType(decision.action.value)
        spec: TargetSpec | None = None
        match decision.target:
            case ControlRef(ref=ref):
                control = observation.control(ref)
                if control is None:
                    raise _Rejected(f"unknown control ref {ref!r}; use a ref from the observation")
                raw_spec = control.to_target_spec()
                spec = await verify_target_spec(self._surface.backend, control, raw_spec)
            case TableCellRef() as cell:
                headers = None
                if cell.table_ref is not None:
                    table = observation.table(cell.table_ref)
                    if table is None:
                        raise _Rejected(f"unknown table ref {cell.table_ref!r}")
                    headers = table.headers or None
                spec = TargetSpec(
                    description=f"table cell [{cell.row_match} / "
                    f"{cell.column_header or cell.column_index}]",
                    table_cell=TableCellSpec(
                        row_match=cell.row_match,
                        column_header=cell.column_header,
                        column_index=cell.column_index,
                        table_headers=headers,
                    ),
                )
            case None:
                spec = None

        value = decision.value
        if value is not None:
            unknown = find_placeholders(value) - set(ctx.values)
            if unknown:
                raise _Rejected(f"unknown input placeholder(s) {sorted(unknown)}")
            for name, actual in ctx.values.items():
                if name in ctx.sensitive and actual and actual in value:
                    raise _Rejected("secret values must never be typed literally")
            value = substitute(value, ctx.values)

        url = decision.url
        if url is not None and not url.startswith(("http://", "https://")):
            raise _Rejected("navigate requires an absolute http(s) URL shown on the page")

        try:
            request = ActionRequest(
                action=action,
                target=spec,
                value=value,
                key=decision.key,
                url=url,
                output=decision.output_name,
                value_type=decision.output_type,
            )
        except ValueError as exc:
            raise _Rejected(str(exc)) from exc
        return request, spec

    def _value_is_secret(self, ctx: _Ctx, value: str | None) -> bool:
        return bool(value) and any(name in ctx.sensitive for name in find_placeholders(value or ""))

    async def _reject(self, ctx: _Ctx, reason: str, observation: Observation) -> Observation:
        ctx.rejections += 1
        ctx.notes.append(reason)
        ctx.history.append(f"{ctx.step_index}. REJECTED: {reason[:120]}")
        self._events.emit(
            EventType.LLM_RESPONSE_REJECTED, reason=reason, consecutive=ctx.rejections
        )
        if ctx.rejections >= MAX_CONSECUTIVE_REJECTIONS:
            raise _Stop(
                DiscoveryStatus.FAILED,
                self._failure(
                    "MODEL_NOT_CONVERGING",
                    f"{ctx.rejections} consecutive proposals were rejected; last: {reason}",
                ),
            )
        return observation

    async def _escalate(self, ctx: _Ctx, *, kind: InterventionKind, reason: str) -> Observation:
        intervention = await self._escalation.request(
            kind=kind,
            reason=reason,
            goal=ctx.goal,
            capability_id=None,
            capability_name=None,
            step_id=None,
            step_index=ctx.step_index,
        )
        if intervention.status is InterventionStatus.RELEASED:
            actions = len(intervention.human_action_log)
            ctx.history.append(f"-- human operator intervened ({actions} action(s)); continuing")
            ctx.fingerprints.clear()
            return await self._observe(ctx)
        raise _Stop(
            DiscoveryStatus.ESCALATED,
            self._failure(
                "ESCALATED",
                f"{reason} ({intervention.status.value})",
                details={"intervention_id": intervention.id},
            ),
        )

    @staticmethod
    def _history_line(
        ctx: _Ctx, decision: Decision, spec: TargetSpec | None, extracted: str | None
    ) -> str:
        target = spec.summary() if spec else (decision.url or decision.key or "")
        if decision.action is AgentAction.EXTRACT:
            return f"{ctx.step_index}. extract {target} -> {decision.output_name}={extracted}"
        value = f" value={decision.value}" if decision.value else ""
        return f"{ctx.step_index}. {decision.action.value} {target}{value} -> ok"

    async def _snapshot(self, index: int, action: str) -> None:
        self._evidence.save_png(f"step-{index:02d}-{action}", await self._surface.screenshot())

    def _failure(
        self,
        code: str,
        message: str,
        *,
        category: OutcomeCategory = OutcomeCategory.HARD_FAILURE,
        observed: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Failure:
        return Failure(
            category=category,
            code=code,
            message=message,
            observed=observed,
            timestamp=datetime.now(UTC),
            details=self._redactor.redact(details or {}),
        )

    async def _finish(
        self,
        ctx: _Ctx,
        *,
        status: DiscoveryStatus,
        failure: Failure | None,
        artifact: CapabilityArtifact | None,
        artifact_path: Path | None,
        started: float,
    ) -> DiscoveryResult:
        self._evidence.save_png("final-screenshot", await self._surface.screenshot())
        result = DiscoveryResult(
            status=status,
            run_id=self._events.run_id,
            goal=ctx.goal,
            artifact_path=str(artifact_path) if artifact_path else None,
            artifact_id=artifact.artifact_id if artifact else None,
            steps_recorded=len(artifact.steps) if artifact else 0,
            llm_calls=len(self._planner.calls),
            extracted=dict(ctx.extracted),
            failure=failure,
            evidence_dir=str(self._evidence.run_dir),
            duration_s=round(time.monotonic() - started, 2),
        )
        self._events.emit(
            EventType.DISCOVERY_COMPLETED
            if status is DiscoveryStatus.COMPLETED
            else EventType.RUN_FAILED,
            status=status.value,
            llm_calls=result.llm_calls,
            steps_recorded=result.steps_recorded,
            failure=failure.model_dump(mode="json") if failure else None,
        )
        self._evidence.save_json("result", result.model_dump(mode="json"))
        self._escalation.session.complete()
        return result


class _Rejected(Exception):  # noqa: N818 - internal control flow
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
