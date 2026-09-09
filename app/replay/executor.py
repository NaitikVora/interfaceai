"""Deterministic replay of a capability artifact. **No LLM is involved anywhere in this module.**

For every step::

    1. wait for preconditions          -> unexpected state? consult rules
    2. evaluate policy                  -> violation stops; irreversible needs human approval
    3. resolve the target               -> not found / ambiguous? consult rules, else escalate/fail
    4. perform the action
    5. wait for postconditions          -> unexpected state? consult rules
    6. record evidence

Rules classify unexpected states as business outcomes (stop, report), recoverable conditions
(apply bounded recovery, retry) or hard failures (stop with evidence).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from app.artifacts.params import (
    InvalidInputError,
    ParsedValue,
    ValueParseError,
    find_placeholders,
    parse_value,
    substitute,
    validate_inputs,
)
from app.artifacts.schema import (
    ActionType,
    CapabilityArtifact,
    ClickRecovery,
    ExtractSource,
    InputSource,
    OnError,
    OutcomeCategory,
    RetryStepRecovery,
    RiskClass,
    RuleMessageSource,
    StatusSource,
    Step,
)
from app.automation.actions import ActionRequest, perform_action
from app.automation.browser import SurfaceActionError
from app.automation.locators import LocatorResolver, ResolutionError, ResolutionFailure
from app.automation.surface import ComputerSurface
from app.automation.waits import Deadline
from app.config import Settings
from app.escalation.manager import (
    EscalationManager,
    InterventionKind,
    InterventionRequest,
    InterventionStatus,
)
from app.observability.events import EventLog, EventType
from app.observability.evidence import EvidenceStore
from app.replay.checkpoints import (
    ConditionContext,
    ConditionOutcome,
    check_conditions,
    wait_for_conditions,
)
from app.replay.errors import (
    EscalationCode,
    Failure,
    HardFailureCode,
    ReplayAbortError,
    ReplayResult,
    ReplayStatus,
)
from app.replay.rules import RuleEngine, RuleMatch
from app.safety.policy import PolicyEngine
from app.safety.redaction import Redactor

RECOVERY_CLICK_TIMEOUT_S = 3.0
HANDOFF_VERIFY_TIMEOUT_S = 2.0
"""After a human releases control the page is already in its final state; a short bounded wait
absorbs an in-flight navigation without stalling on conditions that will never hold."""


class _Disposition(StrEnum):
    RETRY = "retry"
    DONE = "done"


class _ResumeAtError(Exception):
    """Control flow: after a human handoff, continue at ``step_index`` (0-based)."""

    def __init__(self, step_index: int) -> None:
        self.step_index = step_index
        super().__init__(step_index)


class ReplayExecutor:
    def __init__(
        self,
        *,
        surface: ComputerSurface,
        policy: PolicyEngine,
        settings: Settings,
        events: EventLog,
        evidence: EvidenceStore,
        escalation: EscalationManager,
        redactor: Redactor,
    ) -> None:
        self._surface = surface
        self._policy = policy
        self._settings = settings
        self._events = events
        self._evidence = evidence
        self._escalation = escalation
        self._redactor = redactor
        self._resolver = LocatorResolver(
            surface.backend, poll_interval_s=settings.replay_poll_interval_s
        )

    # ------------------------------------------------------------------ public API
    async def replay(
        self,
        artifact: CapabilityArtifact,
        inputs: Mapping[str, object],
        *,
        base_url: str | None = None,
    ) -> ReplayResult:
        started = datetime.now(UTC)
        run = _RunState(artifact, started, max_runtime_s=self._settings.replay_max_runtime_s)
        self._events.capability_id = artifact.artifact_id

        try:
            run.params = self._prepare(artifact, inputs, base_url)
            self._events.emit(
                EventType.REPLAY_STARTED,
                capability=artifact.name,
                version=artifact.version,
                inputs={k: v for k, v in run.params.items() if not self._is_secret(artifact, k)},
                steps=len(artifact.steps),
                llm_in_loop=False,
            )
            await self._ensure_entry(artifact, run)
            position = 0
            while position < len(artifact.steps):
                step = artifact.steps[position]
                try:
                    await self._run_step(run, position + 1, step)
                except _ResumeAtError as jump:
                    skipped = artifact.steps[position : jump.step_index]
                    run.human_completed.extend(s.id for s in skipped)
                    run.steps_completed = jump.step_index
                    position = jump.step_index
                    continue
                position += 1
                run.steps_completed = position
            await self._verify_final_checkpoint(artifact, run)
            return await self._finish(run, ReplayStatus.SUCCESS, None)
        except ReplayAbortError as abort:
            run.rule_outputs.update(abort.outputs)
            return await self._finish(run, abort.status, abort.failure)

    # ------------------------------------------------------------------ setup
    def _prepare(
        self, artifact: CapabilityArtifact, inputs: Mapping[str, object], base_url: str | None
    ) -> dict[str, str]:
        try:
            values = validate_inputs(artifact, inputs)
        except InvalidInputError as exc:
            raise ReplayAbortError(
                ReplayStatus.HARD_FAILURE,
                self._failure(HardFailureCode.INVALID_INPUT, "; ".join(exc.problems), step=None),
            ) from exc
        effective_base = (base_url or artifact.target.base_url).rstrip("/")
        if not self._policy.url_allowed(effective_base + "/"):
            raise ReplayAbortError(
                ReplayStatus.HARD_FAILURE,
                self._failure(
                    HardFailureCode.POLICY_VIOLATION,
                    f"base URL {effective_base!r} is outside the policy allowlist",
                    step=None,
                ),
            )
        problems = self._compatibility_problems(artifact)
        if problems:
            raise ReplayAbortError(
                ReplayStatus.HARD_FAILURE,
                self._failure(
                    HardFailureCode.ARTIFACT_INCOMPATIBLE, "; ".join(problems), step=None
                ),
            )
        for name, spec in artifact.inputs.items():
            if spec.sensitive and name in values:
                self._redactor.add_secret(values[name])
        return {**values, "base_url": effective_base}

    def _compatibility_problems(self, artifact: CapabilityArtifact) -> list[str]:
        problems: list[str] = []
        disallowed = [
            a for a in artifact.policy.actions if a not in self._policy.config.allowed_actions
        ]
        if disallowed:
            problems.append(f"artifact needs actions not allowed by policy: {disallowed}")
        if (
            artifact.policy.max_risk_class is RiskClass.IRREVERSIBLE
            and self._policy.config.irreversible_actions == "block"
        ):
            problems.append("artifact contains irreversible steps but policy blocks them")
        if (
            artifact.policy.max_risk_class is RiskClass.SENSITIVE
            and self._policy.config.sensitive_actions == "block"
        ):
            problems.append("artifact contains sensitive steps but policy blocks them")
        return problems

    async def _ensure_entry(self, artifact: CapabilityArtifact, run: _RunState) -> None:
        if artifact.steps[0].action is ActionType.NAVIGATE:
            return
        url = run.params["base_url"] + artifact.target.entry_path
        request = ActionRequest(action=ActionType.NAVIGATE, url=url)
        decision = self._policy.evaluate(request, current_url=url)
        if not decision.allowed:
            raise ReplayAbortError(
                ReplayStatus.HARD_FAILURE,
                self._failure(HardFailureCode.POLICY_VIOLATION, decision.reason, step=None),
            )
        await self._surface.navigate(url)

    # ------------------------------------------------------------------ step loop
    async def _run_step(self, run: _RunState, index: int, step: Step) -> None:
        ctx = ConditionContext(self._surface, self._resolver, run.params)
        action_attempts = 0
        while True:
            self._check_run_deadline(run, step)
            await self._check_operator_pause(run, index, step)

            pre, pre_match = await run.rules.wait_for_expected_state(
                step.preconditions,
                ctx,
                timeout_s=step.timeout_s,
                poll_interval_s=self._settings.replay_poll_interval_s,
            )
            if not pre.ok:
                disposition = await self._handle_unexpected_state(
                    run, index, step, pre, phase="pre", match=pre_match
                )
                if disposition is _Disposition.DONE:
                    return
                continue

            request = self._build_request(run, step)
            decision = self._policy.evaluate(
                request,
                current_url=await self._surface.current_url(),
                proposed_risk=step.risk_class,
                value_is_secret=self._value_is_secret(run.artifact, step),
            )
            self._events.emit(
                EventType.ACTION_REQUESTED,
                step_id=step.id,
                action=step.action.value,
                target=step.target.summary() if step.target else None,
                value=request.value,
                url=request.url,
            )
            self._events.emit(
                EventType.POLICY_DECISION, step_id=step.id, **decision.as_event_data()
            )
            if not decision.allowed:
                self._events.emit(
                    EventType.POLICY_VIOLATION, step_id=step.id, **decision.as_event_data()
                )
                raise ReplayAbortError(
                    ReplayStatus.HARD_FAILURE,
                    await self._failure_with_screenshot(
                        run,
                        index,
                        step,
                        HardFailureCode.POLICY_VIOLATION,
                        decision.reason,
                        details=decision.as_event_data(),
                    ),
                )
            if decision.requires_confirmation and step.id not in run.approved_steps:
                approval = await self._seek_approval(run, index, step, request, decision.reason)
                if approval is _Disposition.DONE:
                    return
                run.approved_steps.add(step.id)

            resolved = None
            if request.needs_target and step.target is not None:
                try:
                    resolved = await self._resolver.resolve(
                        step.target,
                        timeout_s=step.timeout_s,
                        require_enabled=request.requires_enabled_target,
                        allow_coordinates=self._policy.coordinates_allowed(decision.risk_class),
                    )
                except ResolutionError as exc:
                    self._events.emit(
                        EventType.TARGET_UNRESOLVED,
                        step_id=step.id,
                        code=exc.code.value,
                        target=step.target.summary(),
                        diagnostics=exc.diagnostics.as_dict(),
                    )
                    disposition = await self._handle_unresolved(run, index, step, exc)
                    if disposition is _Disposition.DONE:
                        return
                    continue
                self._events.emit(
                    EventType.TARGET_RESOLVED,
                    step_id=step.id,
                    strategy=resolved.strategy.value,
                    drift_fallback=resolved.diagnostics.drift_fallback,
                    polls=resolved.diagnostics.polls,
                )
                run.strategies[step.id] = resolved.strategy.value
                if resolved.diagnostics.drift_fallback:
                    run.drift.append(step.id)

            action_attempts += 1
            try:
                outcome = await perform_action(self._surface, request, resolved)
            except SurfaceActionError as exc:
                self._events.emit(
                    EventType.ACTION_FAILED,
                    step_id=step.id,
                    error=str(exc),
                    attempt=action_attempts,
                )
                if action_attempts < step.retry_policy.max_attempts:
                    await asyncio.sleep(step.retry_policy.backoff_s)
                    continue
                raise ReplayAbortError(
                    ReplayStatus.HARD_FAILURE,
                    await self._failure_with_screenshot(
                        run, index, step, HardFailureCode.ACTION_FAILED, str(exc)
                    ),
                ) from exc
            self._events.emit(
                EventType.ACTION_EXECUTED,
                step_id=step.id,
                action=step.action.value,
                strategy=outcome.strategy.value if outcome.strategy else None,
                via_coordinates=outcome.via_coordinates,
            )

            if step.action is ActionType.EXTRACT:
                self._store_extracted(run, index, step, outcome.extracted_text or "")

            post, post_match = await run.rules.wait_for_expected_state(
                step.postconditions,
                ctx,
                timeout_s=step.timeout_s,
                poll_interval_s=self._settings.replay_poll_interval_s,
            )
            if post.ok:
                await self._step_succeeded(run, index, step, post)
                return
            self._events.emit(
                EventType.CHECKPOINT_FAILED,
                step_id=step.id,
                expected=post.expected,
                observed=post.observed,
            )
            disposition = await self._handle_unexpected_state(
                run, index, step, post, phase="post", match=post_match
            )
            if disposition is _Disposition.DONE:
                return

    async def _step_succeeded(
        self, run: _RunState, index: int, step: Step, post: ConditionOutcome
    ) -> None:
        self._events.emit(
            EventType.CHECKPOINT_VERIFIED,
            step_id=step.id,
            conditions=len(step.postconditions),
            observed=post.observed[:160],
        )
        if step.evidence_policy.value == "always":
            self._evidence.save_png(
                EvidenceStore.step_file_name(index, step.id, "after"),
                await self._surface.screenshot(),
            )

    # ------------------------------------------------------------------ unexpected states
    async def _handle_unexpected_state(
        self,
        run: _RunState,
        index: int,
        step: Step,
        outcome: ConditionOutcome,
        *,
        phase: str,
        match: RuleMatch | None,
    ) -> _Disposition:
        ctx = ConditionContext(self._surface, self._resolver, run.params)
        if match is None:
            match = await run.rules.first_match(ctx)
        if match is not None:
            return await self._apply_rule(run, index, step, match)
        code = (
            HardFailureCode.PRECONDITION_FAILED
            if phase == "pre"
            else HardFailureCode.CHECKPOINT_FAILED
        )
        message = (
            f"{'precondition' if phase == 'pre' else 'postcondition'} not met within "
            f"{step.timeout_s:.0f}s: {outcome.expected}"
        )
        return await self._fail_or_escalate(
            run,
            index,
            step,
            code=code,
            message=message,
            expected=outcome.expected,
            observed=outcome.observed,
            kind=InterventionKind.CHECKPOINT_FAILED,
        )

    async def _handle_unresolved(
        self, run: _RunState, index: int, step: Step, exc: ResolutionError
    ) -> _Disposition:
        ctx = ConditionContext(self._surface, self._resolver, run.params)
        match = await run.rules.first_match(ctx)
        if match is not None:
            return await self._apply_rule(run, index, step, match)
        code = {
            ResolutionFailure.TARGET_NOT_FOUND: HardFailureCode.TARGET_NOT_FOUND,
            ResolutionFailure.AMBIGUOUS_TARGET: HardFailureCode.AMBIGUOUS_TARGET,
            ResolutionFailure.UNSUPPORTED_TARGET: HardFailureCode.UNSUPPORTED_TARGET,
        }[exc.code]
        observed = await check_conditions([], ctx)
        return await self._fail_or_escalate(
            run,
            index,
            step,
            code=code,
            message=f"{exc.code.value.replace('_', ' ').lower()}: {exc.spec.summary()}",
            expected=f"exactly one usable element for {exc.spec.summary()}",
            observed=observed.observed,
            kind=InterventionKind.TARGET_UNRESOLVED,
            details={"locator_diagnostics": exc.diagnostics.as_dict()},
        )

    async def _apply_rule(
        self, run: _RunState, index: int, step: Step, match: RuleMatch
    ) -> _Disposition:
        rule = match.rule
        self._events.emit(
            EventType.RULE_MATCHED,
            step_id=step.id,
            rule=rule.id,
            category=rule.category.value,
            code=rule.code,
            message=match.message,
        )
        if rule.category is OutcomeCategory.BUSINESS_OUTCOME:
            self._events.emit(EventType.BUSINESS_OUTCOME, step_id=step.id, code=rule.code)
            run.rule_message = match.message
            raise ReplayAbortError(
                ReplayStatus.BUSINESS_OUTCOME,
                await self._failure_with_screenshot(
                    run,
                    index,
                    step,
                    rule.code,
                    match.message or rule.description,
                    category=OutcomeCategory.BUSINESS_OUTCOME,
                    observed=match.observed,
                ),
                outputs=dict(rule.outputs),
            )
        if rule.category is OutcomeCategory.HARD_FAILURE:
            raise ReplayAbortError(
                ReplayStatus.HARD_FAILURE,
                await self._failure_with_screenshot(
                    run,
                    index,
                    step,
                    rule.code,
                    match.message or rule.description,
                    observed=match.observed,
                ),
            )

        key = (step.id, rule.id)
        run.rule_attempts[key] = run.rule_attempts.get(key, 0) + 1
        if run.rule_attempts[key] > rule.max_attempts:
            raise ReplayAbortError(
                ReplayStatus.RECOVERABLE_EXHAUSTED,
                await self._failure_with_screenshot(
                    run,
                    index,
                    step,
                    rule.code,
                    f"{rule.description}: still present after {rule.max_attempts} "
                    f"recovery attempt(s)",
                    category=OutcomeCategory.RECOVERABLE,
                    observed=match.observed,
                ),
            )
        run.recoveries += 1
        recovery = rule.recovery
        assert recovery is not None
        match recovery:
            case RetryStepRecovery():
                await asyncio.sleep(step.retry_policy.backoff_s)
                self._events.emit(
                    EventType.RECOVERY_APPLIED,
                    step_id=step.id,
                    rule=rule.id,
                    recovery="retry_step",
                    attempt=run.rule_attempts[key],
                )
                return _Disposition.RETRY
            case ClickRecovery(target=target):
                resolved = await self._resolver.resolve(
                    target, timeout_s=RECOVERY_CLICK_TIMEOUT_S, require_enabled=True
                )
                await perform_action(
                    self._surface, ActionRequest(action=ActionType.CLICK, target=target), resolved
                )
                await self._surface.wait_for_settled(RECOVERY_CLICK_TIMEOUT_S)
                self._events.emit(
                    EventType.RECOVERY_APPLIED,
                    step_id=step.id,
                    rule=rule.id,
                    recovery="click",
                    target=target.summary(),
                    attempt=run.rule_attempts[key],
                )
                ctx = ConditionContext(self._surface, self._resolver, run.params)
                post, _ = await run.rules.wait_for_expected_state(
                    step.postconditions,
                    ctx,
                    timeout_s=step.timeout_s,
                    poll_interval_s=self._settings.replay_poll_interval_s,
                )
                if step.postconditions and post.ok:
                    await self._step_succeeded(run, index, step, post)
                    return _Disposition.DONE
                return _Disposition.RETRY
        raise TypeError(f"unknown recovery {recovery!r}")  # pragma: no cover

    # ------------------------------------------------------------------ escalation
    async def _fail_or_escalate(
        self,
        run: _RunState,
        index: int,
        step: Step,
        *,
        code: HardFailureCode,
        message: str,
        expected: str | None,
        observed: str | None,
        kind: InterventionKind,
        details: dict[str, Any] | None = None,
    ) -> _Disposition:
        failure = await self._failure_with_screenshot(
            run, index, step, code, message, expected=expected, observed=observed, details=details
        )
        if step.on_error is OnError.FAIL:
            raise ReplayAbortError(ReplayStatus.HARD_FAILURE, failure)

        request = await self._escalation.request(
            kind=kind,
            reason=message,
            goal=run.artifact.description,
            capability_id=run.artifact.artifact_id,
            capability_name=run.artifact.name,
            step_id=step.id,
            step_index=index,
            details={"expected": expected, "observed": observed, **(details or {})},
        )
        run.interventions += 1
        if request.status is InterventionStatus.RELEASED:
            return await self._after_handoff(run, index, step, failure)
        raise ReplayAbortError(ReplayStatus.ESCALATED, self._escalated(failure, request))

    async def _seek_approval(
        self, run: _RunState, index: int, step: Step, request: ActionRequest, reason: str
    ) -> _Disposition | None:
        """Returns None when approved (caller proceeds), DONE when the human did the step."""
        intervention = await self._escalation.request(
            kind=InterventionKind.APPROVAL_REQUIRED,
            reason=reason,
            goal=run.artifact.description,
            capability_id=run.artifact.artifact_id,
            capability_name=run.artifact.name,
            step_id=step.id,
            step_index=index,
            pending_action=describe_request(request, self._redactor),
        )
        run.interventions += 1
        if intervention.status is InterventionStatus.APPROVED:
            return None
        failure = self._failure(
            HardFailureCode.POLICY_VIOLATION,
            "irreversible action was not approved",
            step=step.id,
            expected="human approval for: " + (intervention.pending_action or step.description),
            observed=intervention.status.value,
            screenshot_ref=intervention.screenshot_ref,
        )
        if intervention.status is InterventionStatus.RELEASED:
            return await self._after_handoff(run, index, step, failure)
        raise ReplayAbortError(ReplayStatus.ESCALATED, self._escalated(failure, intervention))

    async def _after_handoff(
        self, run: _RunState, index: int, step: Step, failure: Failure
    ) -> _Disposition:
        """After a human released control, work out where the flow now stands.

        1. The step's postconditions hold  -> the human completed it; continue.
        2. The screen has not moved on     -> retry the step once (the human may have unblocked it).
        3. The screen moved on             -> resume at the first later step whose entry state
                                              (preconditions) matches; the human did the steps in
                                              between.
        Anything else is reported as unresolved rather than guessed.
        """
        ctx = ConditionContext(self._surface, self._resolver, run.params)
        await self._surface.wait_for_settled(HANDOFF_VERIFY_TIMEOUT_S)
        post = await wait_for_conditions(
            step.postconditions,
            ctx,
            timeout_s=HANDOFF_VERIFY_TIMEOUT_S,
            poll_interval_s=self._settings.replay_poll_interval_s,
        )
        if step.postconditions and post.ok:
            self._events.emit(
                EventType.CHECKPOINT_VERIFIED, step_id=step.id, after_human_handoff=True
            )
            return _Disposition.DONE
        pre = await check_conditions(step.preconditions, ctx)
        if pre.ok:
            if step.id not in run.retried_after_handoff:
                run.retried_after_handoff.add(step.id)
                return _Disposition.RETRY
        else:
            for later_index in range(index, len(run.artifact.steps)):
                later = run.artifact.steps[later_index]
                if not later.preconditions:
                    continue
                if (await check_conditions(later.preconditions, ctx)).ok:
                    self._events.emit(
                        EventType.HUMAN_CONTROL_RELEASED,
                        step_id=step.id,
                        resumed_at=later.id,
                        skipped=[s.id for s in run.artifact.steps[index - 1 : later_index]],
                    )
                    raise _ResumeAtError(later_index)
        unresolved = failure.model_copy(
            update={
                "message": f"{failure.message} (still unresolved after human handoff)",
                "details": {
                    **failure.details,
                    "escalation": EscalationCode.UNRESOLVED_AFTER_HANDOFF,
                },
            }
        )
        raise ReplayAbortError(ReplayStatus.ESCALATED, unresolved)

    async def _check_operator_pause(self, run: _RunState, index: int, step: Step) -> None:
        if not self._escalation.pause_requested:
            return
        self._escalation.pause_requested = False
        request = await self._escalation.request(
            kind=InterventionKind.OPERATOR_PAUSE,
            reason="operator requested a pause before the next step",
            goal=run.artifact.description,
            capability_id=run.artifact.artifact_id,
            capability_name=run.artifact.name,
            step_id=step.id,
            step_index=index,
        )
        run.interventions += 1
        if request.status is InterventionStatus.RELEASED:
            return
        failure = self._failure(
            HardFailureCode.UNEXPECTED_STATE, "run stopped during operator pause", step=step.id
        )
        raise ReplayAbortError(ReplayStatus.ESCALATED, self._escalated(failure, request))

    @staticmethod
    def _escalated(failure: Failure, request: InterventionRequest) -> Failure:
        code = {
            InterventionStatus.UNATTENDED: EscalationCode.NO_OPERATOR,
            InterventionStatus.TIMED_OUT: EscalationCode.INTERVENTION_TIMEOUT,
            InterventionStatus.ABORTED: EscalationCode.ABORTED_BY_OPERATOR,
        }.get(request.status, EscalationCode.UNRESOLVED_AFTER_HANDOFF)
        return failure.model_copy(
            update={
                "message": f"{failure.message} (escalated: {code.value.replace('_', ' ').lower()})",
                "details": {**failure.details, "escalation": code, "intervention_id": request.id},
            }
        )

    # ------------------------------------------------------------------ extraction & results
    def _store_extracted(self, run: _RunState, index: int, step: Step, text: str) -> None:
        name = step.arguments.output or step.id
        value_type = step.arguments.value_type
        assert value_type is not None
        try:
            parsed = parse_value(text, value_type)
        except ValueParseError as exc:
            raise ReplayAbortError(
                ReplayStatus.HARD_FAILURE,
                self._failure(
                    HardFailureCode.EXTRACTION_FAILED,
                    str(exc),
                    step=step.id,
                    expected=f"{value_type.value} value for output {name!r}",
                    observed=text[:200],
                ),
            ) from exc
        run.extracted[name] = parsed
        self._events.emit(
            EventType.VALUE_EXTRACTED,
            step_id=step.id,
            output=name,
            value=str(parsed),
            value_type=value_type.value,
        )

    async def _verify_final_checkpoint(self, artifact: CapabilityArtifact, run: _RunState) -> None:
        ctx = ConditionContext(self._surface, self._resolver, run.params)
        outcome = await wait_for_conditions(
            artifact.checkpoint.conditions,
            ctx,
            timeout_s=self._settings.replay_default_step_timeout_s,
            poll_interval_s=self._settings.replay_poll_interval_s,
        )
        if outcome.ok:
            self._events.emit(EventType.CHECKPOINT_VERIFIED, final=True, observed=outcome.observed)
            return
        self._events.emit(
            EventType.CHECKPOINT_FAILED,
            final=True,
            expected=outcome.expected,
            observed=outcome.observed,
        )
        raise ReplayAbortError(
            ReplayStatus.HARD_FAILURE,
            self._failure(
                HardFailureCode.CHECKPOINT_FAILED,
                f"final checkpoint not met: {artifact.checkpoint.description}",
                step=None,
                expected=outcome.expected,
                observed=outcome.observed,
                screenshot_ref=self._evidence.save_png(
                    "final-checkpoint-failed", await self._surface.screenshot()
                ),
            ),
        )

    async def _finish(
        self, run: _RunState, status: ReplayStatus, failure: Failure | None
    ) -> ReplayResult:
        artifact = run.artifact
        outputs = self._assemble_outputs(run, status) if status in _OUTPUT_STATUSES else {}
        if run.params:
            self._evidence.save_png("final-screenshot", await self._surface.screenshot())
        result = ReplayResult(
            status=status,
            run_id=self._events.run_id,
            capability_id=artifact.artifact_id,
            capability_name=artifact.name,
            capability_version=artifact.version,
            outputs=outputs,
            failure=failure,
            step_id=failure.step_id if failure else None,
            steps_completed=run.steps_completed,
            steps_total=len(artifact.steps),
            evidence_dir=str(self._evidence.run_dir),
            evidence_refs=list(self._evidence.refs),
            strategies_used=dict(run.strategies),
            drift_signals=list(run.drift),
            recoveries_applied=run.recoveries,
            human_interventions=run.interventions,
            human_completed_steps=list(run.human_completed),
            llm_calls=0,
            started_at=run.started,
            finished_at=datetime.now(UTC),
        )
        self._events.emit(
            EventType.REPLAY_COMPLETED
            if status is not ReplayStatus.HARD_FAILURE
            else EventType.RUN_FAILED,
            status=status.value,
            outputs=outputs,
            failure=failure.model_dump(mode="json") if failure else None,
            steps_completed=run.steps_completed,
        )
        self._evidence.save_json("result", result.model_dump(mode="json"))
        self._escalation.session.complete()
        return result

    def _assemble_outputs(self, run: _RunState, status: ReplayStatus) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for name, spec in run.artifact.outputs.items():
            match spec.source:
                case StatusSource():
                    outputs[name] = (
                        "success" if status is ReplayStatus.SUCCESS else run.rule_outputs.get(name)
                    )
                case ExtractSource():
                    outputs[name] = run.extracted.get(name)
                case InputSource(name=input_name):
                    outputs[name] = run.params.get(input_name)
                case RuleMessageSource():
                    outputs[name] = run.rule_message
        for name, value in run.rule_outputs.items():
            outputs.setdefault(name, value)
        return outputs

    # ------------------------------------------------------------------ helpers
    def _build_request(self, run: _RunState, step: Step) -> ActionRequest:
        args = step.arguments
        return ActionRequest(
            action=step.action,
            target=step.target,
            value=substitute(args.value, run.params) if args.value is not None else None,
            key=args.key,
            url=substitute(args.url, run.params) if args.url is not None else None,
            output=args.output,
            value_type=args.value_type,
            clear_first=args.clear_first,
        )

    @staticmethod
    def _value_is_secret(artifact: CapabilityArtifact, step: Step) -> bool:
        value = step.arguments.value
        if not value:
            return False
        return any(
            artifact.inputs[name].sensitive
            for name in find_placeholders(value)
            if name in artifact.inputs
        )

    @staticmethod
    def _is_secret(artifact: CapabilityArtifact, name: str) -> bool:
        spec = artifact.inputs.get(name)
        return spec is not None and spec.sensitive

    def _check_run_deadline(self, run: _RunState, step: Step) -> None:
        if run.deadline.expired():
            raise ReplayAbortError(
                ReplayStatus.HARD_FAILURE,
                self._failure(
                    HardFailureCode.REPLAY_TIMEOUT,
                    f"run exceeded {self._settings.replay_max_runtime_s:.0f}s",
                    step=step.id,
                ),
            )

    def _failure(
        self,
        code: HardFailureCode | str,
        message: str,
        *,
        step: str | None,
        category: OutcomeCategory = OutcomeCategory.HARD_FAILURE,
        expected: str | None = None,
        observed: str | None = None,
        screenshot_ref: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Failure:
        return Failure(
            category=category,
            code=str(code.value if isinstance(code, HardFailureCode) else code),
            message=message,
            expected=expected,
            observed=observed,
            step_id=step,
            screenshot_ref=screenshot_ref,
            timestamp=datetime.now(UTC),
            details=self._redactor.redact(details or {}),
        )

    async def _failure_with_screenshot(
        self,
        run: _RunState,
        index: int,
        step: Step,
        code: HardFailureCode | str,
        message: str,
        *,
        category: OutcomeCategory = OutcomeCategory.HARD_FAILURE,
        expected: str | None = None,
        observed: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Failure:
        code_text = code.value if isinstance(code, HardFailureCode) else code
        suffix = code_text.lower().replace("_", "-")
        ref = self._evidence.save_png(
            EvidenceStore.step_file_name(index, step.id, suffix), await self._surface.screenshot()
        )
        self._evidence.save_text(
            EvidenceStore.step_file_name(index, step.id, f"{suffix}-page"),
            await self._surface.page_text(),
        )
        return self._failure(
            code,
            message,
            step=step.id,
            category=category,
            expected=expected,
            observed=observed,
            screenshot_ref=ref,
            details=details,
        )


_OUTPUT_STATUSES = frozenset({ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME})


class _RunState:
    """Mutable per-run bookkeeping kept off the executor so executors are reusable."""

    def __init__(
        self, artifact: CapabilityArtifact, started: datetime, *, max_runtime_s: float
    ) -> None:
        self.artifact = artifact
        self.started = started
        self.params: dict[str, str] = {}
        self.rules = RuleEngine(artifact.conditions)
        self.extracted: dict[str, ParsedValue] = {}
        self.rule_outputs: dict[str, str] = {}
        self.rule_message: str | None = None
        self.strategies: dict[str, str] = {}
        self.drift: list[str] = []
        self.recoveries = 0
        self.interventions = 0
        self.human_completed: list[str] = []
        self.steps_completed = 0
        self.rule_attempts: dict[tuple[str, str], int] = {}
        self.retried_after_handoff: set[str] = set()
        self.approved_steps: set[str] = set()
        self.deadline = Deadline(max_runtime_s)


def describe_request(request: ActionRequest, redactor: Redactor) -> str:
    target = request.target.summary() if request.target else ""
    match request.action:
        case ActionType.NAVIGATE:
            return f"navigate to {request.url}"
        case ActionType.CLICK:
            return f"click {target}"
        case ActionType.TYPE:
            return f"type {redactor.redact_text(request.value or '')!r} into {target}"
        case ActionType.SELECT:
            return f"select {request.value!r} in {target}"
        case ActionType.PRESS:
            return f"press {request.key}"
        case ActionType.EXTRACT:
            return f"read {target} as {request.output}"
    raise ValueError(request.action)  # pragma: no cover
