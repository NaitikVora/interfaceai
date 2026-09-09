"""Turns executed discovery actions into artifact steps and, finally, a capability artifact.

Principles:

* **Parameterize.** Every occurrence of an input value in a typed value, URL or condition is
  replaced by ``${input_name}``.
* **Verify targets before recording them.** A strategy is kept only if it matched exactly one
  element on the live page (optionally scoped to the control's container). Unverified strategies
  are dropped rather than persisted as guesses.
* **Checkpoints are observations, not hopes.** Pre/postconditions are derived from the page
  states actually seen before and after the action.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from app.agent.models import Decision
from app.artifacts.profile import ApplicationProfile
from app.artifacts.schema import (
    PLACEHOLDER_RE,
    ActionType,
    ArtifactMetadata,
    CapabilityArtifact,
    Checkpoint,
    Compatibility,
    Condition,
    ElementValue,
    ExtractSource,
    HeadingPresent,
    InputSource,
    InputSpec,
    OnError,
    OutputSpec,
    PolicyRequirements,
    RetryPolicy,
    RiskClass,
    RuleMessageSource,
    StatusSource,
    Step,
    StepArguments,
    TargetApplication,
    TargetSpec,
    UrlMatches,
    ValueType,
)
from app.automation.actions import ActionRequest
from app.automation.surface import (
    ControlDescriptor,
    MatchedElement,
    Observation,
    StrategyBackend,
)
from app.config import Settings
from app.safety.policy import RISK_ORDER

MIN_CANONICALIZED_LENGTH = 3
"""Input values shorter than this are not substituted into text (too many false positives)."""

_DIGIT_RUN_RE = re.compile(r"\d{3,}")


@dataclass(frozen=True)
class DiscoveryInput:
    name: str
    value: str
    sensitive: bool


async def verify_target_spec(
    backend: StrategyBackend, control: ControlDescriptor, spec: TargetSpec
) -> TargetSpec:
    """Keep only strategies that uniquely identify the element right now.

    Semantic strategies that are ambiguous page-wide are retried scoped to the control's
    container; if that makes them unique, the container becomes the spec's ``within`` scope.
    """
    container_spec = control.container.to_target_spec() if control.container else None
    scope = None
    if container_spec is not None:
        scope = await _unique_visible(backend, container_spec)

    kept_unscoped: set[str] = set()
    kept_scoped: set[str] = set()
    for strategy in spec.semantic_strategies():
        matches = await backend.match(spec, strategy)
        visible = [m for m in matches if m.visible]
        if len(visible) == 1:
            kept_unscoped.add(strategy.value)
        elif len(visible) > 1 and scope is not None:
            scoped = [m for m in await backend.match(spec, strategy, scope) if m.visible]
            if len(scoped) == 1:
                kept_scoped.add(strategy.value)

    fields_by_strategy = {
        "role_name": ("role", "name"),
        "label": ("label",),
        "attributes": ("attributes",),
        "text": ("text",),
        "table_cell": ("table_cell",),
    }
    updates: dict[str, object] = {}
    for strategy_name, fields in fields_by_strategy.items():
        if strategy_name not in kept_unscoped and strategy_name not in kept_scoped:
            for name in fields:
                updates[name] = None
    if kept_scoped:
        updates["within"] = container_spec
    for structural in ("css", "xpath"):
        value = getattr(spec, structural)
        if value is None:
            continue
        probe = TargetSpec(**{structural: value})
        visible = [
            m for m in await backend.match(probe, probe.available_strategies()[0]) if m.visible
        ]
        if len(visible) != 1:
            updates[structural] = None
    verified = spec.model_copy(update=updates)
    if not verified.available_strategies():
        return spec
    return verified


async def _unique_visible(backend: StrategyBackend, spec: TargetSpec) -> MatchedElement | None:
    for strategy in spec.available_strategies():
        visible = [m for m in await backend.match(spec, strategy) if m.visible]
        if len(visible) == 1:
            return visible[0]
    return None


class Recorder:
    def __init__(
        self,
        *,
        inputs: list[DiscoveryInput],
        base_url: str,
        profile: ApplicationProfile,
        settings: Settings,
    ) -> None:
        self._inputs = inputs
        self._base_url = base_url.rstrip("/")
        self._profile = profile
        self._settings = settings
        self.steps: list[Step] = []
        self._extracted_values: list[str] = []
        self._output_types: dict[str, tuple[ValueType, str]] = {}
        self.notes: list[str] = []

    # ------------------------------------------------------------------ canonicalization
    def canonicalize(self, text: str) -> str:
        """Replace input values with placeholders; longest values first, base URL too."""
        if not text:
            return text
        result = text
        ordered = sorted(self._inputs, key=lambda i: len(i.value), reverse=True)
        for item in ordered:
            if len(item.value) >= MIN_CANONICALIZED_LENGTH and item.value in result:
                result = result.replace(item.value, f"${{{item.name}}}")
        if self._base_url and self._base_url in result:
            result = result.replace(self._base_url, "${base_url}")
        return result

    def path_pattern(self, url: str) -> str:
        """Regex for the URL path with input values as placeholders, e.g. ``/members/${id}$``."""
        path = urlsplit(url).path or "/"
        canonical = self.canonicalize(path)
        parts: list[str] = []
        position = 0
        for match in PLACEHOLDER_RE.finditer(canonical):
            parts.append(re.escape(canonical[position : match.start()]))
            parts.append(match.group(0))
            position = match.end()
        parts.append(re.escape(canonical[position:]))
        return "".join(parts) + "$"

    def page_identity(self, observation: Observation) -> list[Condition]:
        """URL pattern plus the first *stable* heading of the page."""
        conditions: list[Condition] = [UrlMatches(pattern=self.path_pattern(observation.url))]
        heading = self._stable_heading(observation)
        if heading is not None:
            conditions.append(HeadingPresent(text=heading))
        return conditions

    def _stable_heading(self, observation: Observation) -> str | None:
        for heading in observation.headings:
            canonical = self.canonicalize(heading)
            if PLACEHOLDER_RE.search(canonical) or _DIGIT_RUN_RE.search(canonical):
                continue
            if any(v and v in heading for v in self._extracted_values):
                continue
            return canonical
        return None

    # ------------------------------------------------------------------ recording
    def record(
        self,
        *,
        index: int,
        decision: Decision,
        request: ActionRequest,
        spec: TargetSpec | None,
        before: Observation,
        after: Observation,
        risk: RiskClass,
        extracted_text: str | None,
    ) -> Step:
        action = request.action
        value = self.canonicalize(request.value) if request.value is not None else None
        url = self.canonicalize(request.url) if request.url is not None else None
        preconditions = self.page_identity(before)
        postconditions: list[Condition] = []
        if action in {ActionType.NAVIGATE, ActionType.CLICK, ActionType.PRESS}:
            postconditions = self.page_identity(after)
            if decision.expected_heading and decision.expected_heading in after.headings:
                expected = HeadingPresent(text=self.canonicalize(decision.expected_heading))
                if expected not in postconditions:
                    postconditions.append(expected)
        elif action in {ActionType.TYPE, ActionType.SELECT} and spec is not None and value:
            postconditions = [ElementValue(target=spec, value=value)]
        if action is ActionType.EXTRACT and extracted_text is not None:
            self._extracted_values.append(extracted_text)
            assert request.output is not None and request.value_type is not None
            self._output_types[request.output] = (
                request.value_type,
                self._step_id(index, action, spec),
            )

        if action is ActionType.NAVIGATE:
            preconditions = []

        step = Step(
            id=self._step_id(index, action, spec),
            action=action,
            description=self._describe(action, spec, value, url, request),
            target=spec,
            arguments=StepArguments(
                value=value,
                key=request.key,
                url=url,
                output=request.output,
                value_type=request.value_type,
                clear_first=request.clear_first,
            ),
            preconditions=preconditions,
            postconditions=postconditions,
            timeout_s=self._settings.replay_default_step_timeout_s,
            retry_policy=RetryPolicy(max_attempts=1 if risk is RiskClass.IRREVERSIBLE else 2),
            risk_class=risk,
            on_error=OnError.ESCALATE,
        )
        self.steps.append(step)
        return step

    def _step_id(self, index: int, action: ActionType, spec: TargetSpec | None) -> str:
        slug_source = spec.summary() if spec is not None else action.value
        slug = re.sub(r"\$\{[^}]*\}", "", slug_source.lower())
        slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")[:24].strip("-") or action.value
        return f"s{index:02d}-{action.value}-{slug}"

    @staticmethod
    def _describe(
        action: ActionType,
        spec: TargetSpec | None,
        value: str | None,
        url: str | None,
        request: ActionRequest,
    ) -> str:
        target = spec.summary() if spec is not None else ""
        match action:
            case ActionType.NAVIGATE:
                return f"Open {url}"
            case ActionType.CLICK:
                return f"Click {target}"
            case ActionType.TYPE:
                return f"Enter {value} into {target}"
            case ActionType.SELECT:
                return f"Select {value} in {target}"
            case ActionType.PRESS:
                return f"Press {request.key}"
            case ActionType.EXTRACT:
                return f"Read {target} as {request.output} ({request.value_type})"
        raise ValueError(action)  # pragma: no cover

    # ------------------------------------------------------------------ artifact
    def build_artifact(
        self,
        *,
        name: str,
        goal: str,
        description: str,
        run_id: str,
        llm_model: str | None,
        llm_decisions: int,
        duration_s: float,
        final_observation: Observation,
        echo_outputs: dict[str, str],
    ) -> CapabilityArtifact:
        used = self._used_inputs()
        inputs: dict[str, InputSpec] = {}
        for item in self._inputs:
            if item.name not in used and item.name not in echo_outputs.values():
                self.notes.append(f"input {item.name!r} was declared but never used; dropped")
                continue
            inputs[item.name] = InputSpec(
                type=ValueType.STRING,
                description=f"{item.name.replace('_', ' ')} supplied by the caller",
                sensitive=item.sensitive,
                pattern=r"^\d+$" if item.value.isdigit() else None,
            )

        outputs: dict[str, OutputSpec] = {
            "status": OutputSpec(
                type=ValueType.STRING,
                description="success, or the business outcome that pre-empted the flow",
                enum=self._profile.status_values(),
                source=StatusSource(),
            )
        }
        for output_name, ref in echo_outputs.items():
            input_name = ref[2:-1]
            if input_name in inputs:
                outputs[output_name] = OutputSpec(
                    type=ValueType.STRING,
                    description=f"echo of input {input_name}",
                    source=InputSource(name=input_name),
                )
        for output_name, (value_type, step_id) in self._output_types.items():
            outputs[output_name] = OutputSpec(
                type=value_type,
                description=f"extracted by step {step_id}",
                nullable=True,
                source=ExtractSource(step_id=step_id),
            )
        outputs["message"] = OutputSpec(
            type=ValueType.STRING,
            description="the application's own message when a business outcome occurs",
            nullable=True,
            source=RuleMessageSource(),
        )

        actions = sorted({s.action for s in self.steps}, key=lambda a: a.value)
        max_risk = max((s.risk_class for s in self.steps), key=lambda r: RISK_ORDER[r])
        return CapabilityArtifact(
            artifact_id=str(uuid.uuid4()),
            name=name,
            version=1,
            description=self.canonicalize(description),
            target=TargetApplication(
                vendor=self._profile.vendor,
                application=self._profile.product,
                version=self._profile.application_version,
                base_url=self._base_url,
                entry_path=urlsplit(self.steps[0].arguments.url or "/").path
                if self.steps and self.steps[0].action is ActionType.NAVIGATE
                else "/",
            ),
            inputs=inputs,
            outputs=outputs,
            steps=list(self.steps),
            checkpoint=Checkpoint(
                description="Final screen reached: "
                + (
                    final_observation.headings[0]
                    if final_observation.headings
                    else final_observation.url
                ),
                conditions=self.page_identity(final_observation),
            ),
            conditions=self._profile.conditions,
            policy=PolicyRequirements(
                actions=actions,
                url_patterns=[f"^{re.escape(self._base_url)}(/.*)?$"],
                max_risk_class=max_risk,
                requires_human_confirmation=max_risk is RiskClass.IRREVERSIBLE,
            ),
            compatibility=Compatibility(
                vendor=self._profile.vendor,
                product=self._profile.product,
                supported_versions=self._profile.supported_versions,
                notes="Recorded against the base profile; tenant overrides layer on top.",
            ),
            metadata=ArtifactMetadata(
                goal=self.canonicalize(goal),
                discovery_run_id=run_id,
                llm_model=llm_model,
                llm_decisions=llm_decisions,
                discovery_duration_s=round(duration_s, 2),
                notes=list(self.notes),
            ),
            created_at=datetime.now(UTC),
        )

    def _used_inputs(self) -> set[str]:
        used: set[str] = set()
        for step in self.steps:
            for text in (step.arguments.value, step.arguments.url):
                if text:
                    used.update(PLACEHOLDER_RE.findall(text))
        return used
