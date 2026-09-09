"""Capability artifact schema (schema_version 1.0).

Design goals, in priority order:

1. **A contract, not a transcript.** Typed inputs, typed outputs with provenance, an explicit
   success checkpoint and a runtime-condition taxonomy. Nothing from the LLM conversation is
   stored; the model's reasoning is evidence, not capability.
2. **Surface-agnostic targets.** ``TargetSpec`` describes *what* a control is (role/name, label,
   semantic attributes, visible text, table position) before *where* it is (CSS/XPath) and only
   then *how it looks* (coordinates). The replay engine tries strategies in that fixed order.
3. **Verified checkpoints.** Every condition stored here was observed to be true during the
   discovery run; replay never assumes an action succeeded.
4. **Parameterized.** Runtime values appear only as ``${input_name}`` placeholders.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION: Literal["1.0"] = "1.0"

PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
IDENTIFIER_RE = r"^[a-z][a-z0-9_]*$"
STEP_ID_RE = r"^[a-z0-9][a-z0-9_-]*$"
CODE_RE = r"^[A-Z][A-Z0-9_]*$"

RESERVED_PARAMETERS: frozenset[str] = frozenset({"base_url"})
"""Placeholders provided by the runtime rather than by the caller."""


class StrictModel(BaseModel):
    """All artifact models reject unknown fields so malformed artifacts fail loudly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


# ----------------------------------------------------------------------------------------------
# Enumerations
# ----------------------------------------------------------------------------------------------


class ActionType(StrEnum):
    """Actions a replayable step may perform. ``wait``/``finish``/``escalate`` are agent-level."""

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    PRESS = "press"
    EXTRACT = "extract"


class RiskClass(StrEnum):
    SAFE = "safe"
    SENSITIVE = "sensitive"
    IRREVERSIBLE = "irreversible"


class ValueType(StrEnum):
    STRING = "string"
    DECIMAL = "decimal"
    INTEGER = "integer"
    BOOLEAN = "boolean"


class OnError(StrEnum):
    FAIL = "fail"
    ESCALATE = "escalate"


class EvidencePolicy(StrEnum):
    ON_FAILURE = "on_failure"
    ALWAYS = "always"


class OutcomeCategory(StrEnum):
    """The three kinds of non-success states a replay can end in (see REPORT.md section 3)."""

    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"


class Strategy(StrEnum):
    """Locator strategies in **priority order**. The resolver never reorders these."""

    ROLE_NAME = "role_name"
    LABEL = "label"
    ATTRIBUTES = "attributes"
    TEXT = "text"
    TABLE_CELL = "table_cell"
    CSS = "css"
    XPATH = "xpath"
    COORDINATES = "coordinates"


STRATEGY_PRIORITY: tuple[Strategy, ...] = tuple(Strategy)

SEMANTIC_STRATEGIES: frozenset[Strategy] = frozenset(
    {Strategy.ROLE_NAME, Strategy.LABEL, Strategy.ATTRIBUTES, Strategy.TEXT, Strategy.TABLE_CELL}
)
"""Strategies that describe *what* a control is. The rest describe *where* it is."""

STRUCTURAL_STRATEGIES: frozenset[Strategy] = frozenset({Strategy.CSS, Strategy.XPATH})


# ----------------------------------------------------------------------------------------------
# Targets
# ----------------------------------------------------------------------------------------------


class Point(StrictModel):
    x: float
    y: float


class Viewport(StrictModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class TableCellSpec(StrictModel):
    """A cell addressed by *meaning*: the row containing ``row_match`` and a column by header.

    Legacy back-office screens are table-heavy; header/row addressing survives column
    reordering and styling changes that break positional selectors.
    """

    row_match: str = Field(min_length=1, description="Exact normalized text of a cell in the row")
    column_header: str | None = Field(default=None, description="Header text of the wanted column")
    column_index: int | None = Field(
        default=None, ge=0, description="0-based column index, for tables without headers"
    )
    table_headers: list[str] | None = Field(
        default=None, description="Header signature used to pick the table when several exist"
    )

    @model_validator(mode="after")
    def _column_addressing(self) -> TableCellSpec:
        if (self.column_header is None) == (self.column_index is None):
            raise ValueError("table_cell needs exactly one of column_header or column_index")
        return self


class TargetSpec(StrictModel):
    """Multi-strategy description of one control. Strategies are tried in ``STRATEGY_PRIORITY``.

    ``within`` scopes the *semantic* strategies (role/name, label, attributes, text) to a
    container such as a form or navigation strip, which is how humans disambiguate "the Search
    button in the lookup form" from an identical button elsewhere. Structural strategies
    (css/xpath) are always absolute.

    ``fallbacks`` are *alternative controls* (for example a tenant whose button reads "Find"
    instead of "Search"); they are tried only after every strategy of the primary spec failed.
    """

    description: str | None = None
    within: TargetSpec | None = Field(
        default=None, description="Container the semantic strategies are scoped to"
    )
    role: str | None = None
    name: str | None = Field(default=None, description="Accessible name (with role)")
    label: str | None = Field(default=None, description="Associated form label text")
    attributes: dict[str, str] | None = Field(
        default=None, description="Stable semantic attributes, e.g. {'tag':'input','name':'q'}"
    )
    text: str | None = Field(default=None, description="Exact normalized visible text")
    table_cell: TableCellSpec | None = None
    css: str | None = None
    xpath: str | None = None
    coordinates: Point | None = None
    viewport: Viewport | None = Field(default=None, description="Viewport the coordinates assume")
    frame: str | None = Field(
        default=None,
        description="Reserved for framed legacy apps (iframe/frameset selector chain). "
        "Not supported by the v1 resolver, which rejects it explicitly.",
    )
    fallbacks: list[TargetSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _at_least_one_strategy(self) -> TargetSpec:
        if not self.available_strategies():
            raise ValueError("target must define at least one locator strategy")
        if self.coordinates is not None and self.viewport is None:
            raise ValueError("coordinates require the viewport they were captured in")
        if self.within is not None and self.within.within is not None:
            raise ValueError("container scopes do not nest")
        return self

    def semantic_strategies(self) -> list[Strategy]:
        return [s for s in self.available_strategies() if s in SEMANTIC_STRATEGIES]

    def available_strategies(self) -> list[Strategy]:
        """Strategies this spec can attempt, in priority order."""
        present = {
            Strategy.ROLE_NAME: self.role is not None and self.name is not None,
            Strategy.LABEL: self.label is not None,
            Strategy.ATTRIBUTES: bool(self.attributes),
            Strategy.TEXT: self.text is not None,
            Strategy.TABLE_CELL: self.table_cell is not None,
            Strategy.CSS: self.css is not None,
            Strategy.XPATH: self.xpath is not None,
            Strategy.COORDINATES: self.coordinates is not None,
        }
        return [s for s in STRATEGY_PRIORITY if present[s]]

    def summary(self) -> str:
        """Short human-readable identification used in logs and the operator console."""
        if self.description:
            return self.description
        if self.role and self.name:
            return f"{self.role} '{self.name}'"
        if self.label:
            return f"field labelled '{self.label}'"
        if self.table_cell:
            col = self.table_cell.column_header or f"column {self.table_cell.column_index}"
            return f"table cell [{self.table_cell.row_match} / {col}]"
        if self.text:
            return f"text '{self.text}'"
        if self.attributes:
            tag = self.attributes.get("tag", "*")
            rest = "".join(f"[{k}={v}]" for k, v in self.attributes.items() if k != "tag")
            return f"{tag}{rest}"
        return self.css or self.xpath or "coordinates"


# ----------------------------------------------------------------------------------------------
# Conditions (checkpoints, pre/postconditions, rule triggers)
# ----------------------------------------------------------------------------------------------


class UrlMatches(StrictModel):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str = Field(min_length=1, description="Regex over the current URL; may use ${...}")


class TextPresent(StrictModel):
    kind: Literal["text_present"] = "text_present"
    text: str = Field(min_length=1, description="Case-sensitive substring of visible page text")


class TextAbsent(StrictModel):
    kind: Literal["text_absent"] = "text_absent"
    text: str = Field(min_length=1)


class HeadingPresent(StrictModel):
    """A visible heading (h1-h6) reads exactly ``text``. Distinguishes pages that share nav text."""

    kind: Literal["heading_present"] = "heading_present"
    text: str = Field(min_length=1)


class ElementVisible(StrictModel):
    kind: Literal["element_visible"] = "element_visible"
    target: TargetSpec


class ElementHidden(StrictModel):
    kind: Literal["element_hidden"] = "element_hidden"
    target: TargetSpec


class ElementEnabled(StrictModel):
    kind: Literal["element_enabled"] = "element_enabled"
    target: TargetSpec


class ElementValue(StrictModel):
    """A form control currently holds ``value`` (after substitution). Verifies type/select."""

    kind: Literal["element_value"] = "element_value"
    target: TargetSpec
    value: str


class AllOf(StrictModel):
    kind: Literal["all_of"] = "all_of"
    conditions: list[Condition] = Field(min_length=1)


class AnyOf(StrictModel):
    kind: Literal["any_of"] = "any_of"
    conditions: list[Condition] = Field(min_length=1)


class Not(StrictModel):
    kind: Literal["not"] = "not"
    condition: Condition


Condition = Annotated[
    UrlMatches
    | TextPresent
    | TextAbsent
    | HeadingPresent
    | ElementVisible
    | ElementHidden
    | ElementEnabled
    | ElementValue
    | AllOf
    | AnyOf
    | Not,
    Field(discriminator="kind"),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()


def describe_condition(condition: Condition) -> str:
    """Human-readable rendering used in failure messages ("expected: ...")."""
    match condition:
        case UrlMatches(pattern=p):
            return f"URL matches /{p}/"
        case TextPresent(text=t):
            return f"page text contains {t!r}"
        case TextAbsent(text=t):
            return f"page text does not contain {t!r}"
        case HeadingPresent(text=t):
            return f"page heading {t!r} is shown"
        case ElementVisible(target=t):
            return f"{t.summary()} is visible"
        case ElementHidden(target=t):
            return f"{t.summary()} is hidden"
        case ElementEnabled(target=t):
            return f"{t.summary()} is enabled"
        case ElementValue(target=t, value=v):
            return f"{t.summary()} has value {v!r}"
        case AllOf(conditions=cs):
            return "(" + " AND ".join(describe_condition(c) for c in cs) + ")"
        case AnyOf(conditions=cs):
            return "(" + " OR ".join(describe_condition(c) for c in cs) + ")"
        case Not(condition=c):
            return f"NOT {describe_condition(c)}"
    raise TypeError(f"unknown condition {condition!r}")  # pragma: no cover - exhaustive match


# ----------------------------------------------------------------------------------------------
# Runtime-condition rules
# ----------------------------------------------------------------------------------------------


class RetryStepRecovery(StrictModel):
    kind: Literal["retry_step"] = "retry_step"


class ClickRecovery(StrictModel):
    kind: Literal["click"] = "click"
    target: TargetSpec


Recovery = Annotated[RetryStepRecovery | ClickRecovery, Field(discriminator="kind")]


class ConditionRule(StrictModel):
    """Classifies an observed runtime state and says what replay should do about it.

    Rules are consulted only when a step's pre- or postcondition is not met. They come from a
    curated per-vendor application profile and are embedded so the artifact is self-contained
    and reviewable.
    """

    id: str = Field(pattern=STEP_ID_RE)
    description: str
    when: Condition
    category: OutcomeCategory
    code: str = Field(pattern=CODE_RE)
    outputs: dict[str, str] = Field(
        default_factory=dict,
        description="Output values reported for a business outcome (e.g. status=not_found)",
    )
    message_from: TargetSpec | None = Field(
        default=None, description="Where to read the application's own message, if any"
    )
    recovery: Recovery | None = None
    max_attempts: int = Field(default=1, ge=1, le=5)

    @model_validator(mode="after")
    def _recovery_matches_category(self) -> ConditionRule:
        if self.category is OutcomeCategory.RECOVERABLE and self.recovery is None:
            raise ValueError(f"recoverable rule {self.id!r} must define a recovery")
        if self.category is not OutcomeCategory.RECOVERABLE and self.recovery is not None:
            raise ValueError(f"rule {self.id!r} is not recoverable but defines a recovery")
        return self


# ----------------------------------------------------------------------------------------------
# Steps
# ----------------------------------------------------------------------------------------------


class RetryPolicy(StrictModel):
    max_attempts: int = Field(default=1, ge=1, le=5)
    backoff_s: float = Field(default=0.5, ge=0)


class StepArguments(StrictModel):
    value: str | None = Field(default=None, description="type/select value; may be ${input}")
    key: str | None = Field(default=None, description="press: key name, e.g. Enter")
    url: str | None = Field(default=None, description="navigate: absolute URL, may use ${base_url}")
    output: str | None = Field(default=None, pattern=IDENTIFIER_RE, description="extract: output")
    value_type: ValueType | None = Field(default=None, description="extract: parse as this type")
    clear_first: bool = Field(default=True, description="type: clear existing text first")


class Step(StrictModel):
    id: str = Field(pattern=STEP_ID_RE)
    action: ActionType
    description: str
    target: TargetSpec | None = None
    arguments: StepArguments = Field(default_factory=StepArguments)
    preconditions: list[Condition] = Field(default_factory=list)
    postconditions: list[Condition] = Field(default_factory=list)
    timeout_s: float = Field(default=10.0, gt=0, le=120)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    risk_class: RiskClass = RiskClass.SAFE
    on_error: OnError = OnError.FAIL
    evidence_policy: EvidencePolicy = EvidencePolicy.ON_FAILURE

    @model_validator(mode="after")
    def _arguments_match_action(self) -> Step:
        needs_target = {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.EXTRACT}
        if self.action in needs_target and self.target is None:
            raise ValueError(f"step {self.id!r}: {self.action} requires a target")
        if self.action in {ActionType.NAVIGATE, ActionType.PRESS} and self.target is not None:
            raise ValueError(f"step {self.id!r}: {self.action} must not have a target")
        args = self.arguments
        if self.action in {ActionType.TYPE, ActionType.SELECT} and args.value is None:
            raise ValueError(f"step {self.id!r}: {self.action} requires arguments.value")
        if self.action is ActionType.PRESS and not args.key:
            raise ValueError(f"step {self.id!r}: press requires arguments.key")
        if self.action is ActionType.NAVIGATE and not args.url:
            raise ValueError(f"step {self.id!r}: navigate requires arguments.url")
        if self.action is ActionType.EXTRACT and (args.output is None or args.value_type is None):
            raise ValueError(f"step {self.id!r}: extract requires arguments.output and value_type")
        return self


# ----------------------------------------------------------------------------------------------
# Contract: inputs, outputs, checkpoint
# ----------------------------------------------------------------------------------------------


class InputSpec(StrictModel):
    type: ValueType = ValueType.STRING
    description: str = ""
    required: bool = True
    sensitive: bool = Field(
        default=False,
        description="Never logged, never stored; must be supplied from a secret source",
    )
    pattern: str | None = Field(default=None, description="Regex the string value must match")
    enum: list[str] | None = None
    default: str | None = None


class StatusSource(StrictModel):
    kind: Literal["status"] = "status"


class ExtractSource(StrictModel):
    kind: Literal["extract"] = "extract"
    step_id: str


class InputSource(StrictModel):
    kind: Literal["input"] = "input"
    name: str


class RuleMessageSource(StrictModel):
    kind: Literal["rule_message"] = "rule_message"


OutputSource = Annotated[
    StatusSource | ExtractSource | InputSource | RuleMessageSource, Field(discriminator="kind")
]


class OutputSpec(StrictModel):
    type: ValueType
    description: str = ""
    nullable: bool = Field(default=False, description="Null when a business outcome pre-empts it")
    enum: list[str] | None = None
    source: OutputSource


class Checkpoint(StrictModel):
    """The success condition: all of these must hold after the last step."""

    description: str
    conditions: list[Condition] = Field(min_length=1)


# ----------------------------------------------------------------------------------------------
# Target application, compatibility, policy requirements, metadata
# ----------------------------------------------------------------------------------------------


class TargetApplication(StrictModel):
    surface: Literal["web"] = "web"
    vendor: str
    application: str
    version: str
    base_url: str = Field(description="Recorded base URL; overridable per tenant at replay")
    entry_path: str = Field(default="/", description="Path the flow starts from")


class Compatibility(StrictModel):
    vendor: str
    product: str
    supported_versions: list[str] = Field(min_length=1)
    tenant_profile: str | None = Field(
        default=None, description="Tenant override profile this artifact was specialized for"
    )
    notes: str = ""


class PolicyRequirements(StrictModel):
    """What the capability needs to be allowed to do. Replay verifies this against live policy."""

    actions: list[ActionType] = Field(min_length=1)
    url_patterns: list[str] = Field(min_length=1)
    max_risk_class: RiskClass
    requires_human_confirmation: bool = Field(
        description="True when any step is irreversible and therefore gated on a human"
    )


class ArtifactMetadata(StrictModel):
    goal: str
    discovery_run_id: str
    llm_model: str | None = None
    llm_decisions: int = Field(default=0, ge=0)
    discovery_duration_s: float = Field(default=0.0, ge=0)
    recorded_by: str = "app.agent.recorder"
    notes: list[str] = Field(default_factory=list)


class CapabilityArtifact(StrictModel):
    """A reusable, reviewable capability an agent can invoke by name with typed inputs."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    artifact_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    name: str = Field(pattern=IDENTIFIER_RE)
    version: int = Field(default=1, ge=1)
    description: str
    target: TargetApplication
    inputs: dict[str, InputSpec]
    outputs: dict[str, OutputSpec]
    steps: list[Step] = Field(min_length=1)
    checkpoint: Checkpoint
    conditions: list[ConditionRule] = Field(default_factory=list)
    policy: PolicyRequirements
    compatibility: Compatibility
    metadata: ArtifactMetadata
    created_at: datetime

    @model_validator(mode="after")
    def _semantic_integrity(self) -> CapabilityArtifact:
        for key in self.inputs:
            if not re.match(IDENTIFIER_RE, key):
                raise ValueError(f"input name {key!r} is not a valid identifier")
            if key in RESERVED_PARAMETERS:
                raise ValueError(f"input name {key!r} is reserved")
        for key in self.outputs:
            if not re.match(IDENTIFIER_RE, key):
                raise ValueError(f"output name {key!r} is not a valid identifier")

        step_ids = [s.id for s in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step ids must be unique")

        declared = set(self.inputs) | RESERVED_PARAMETERS
        for placeholder, where in self.iter_placeholders():
            if placeholder not in declared:
                raise ValueError(f"{where} references undeclared input ${{{placeholder}}}")

        extract_outputs = {
            s.arguments.output: s.id for s in self.steps if s.action is ActionType.EXTRACT
        }
        for name, spec in self.outputs.items():
            match spec.source:
                case ExtractSource(step_id=sid):
                    if sid not in step_ids:
                        raise ValueError(f"output {name!r} references unknown step {sid!r}")
                    if extract_outputs.get(name) != sid:
                        raise ValueError(f"output {name!r} is not extracted by step {sid!r}")
                case InputSource(name=inp):
                    if inp not in self.inputs:
                        raise ValueError(f"output {name!r} echoes undeclared input {inp!r}")
                case StatusSource():
                    if spec.type is not ValueType.STRING:
                        raise ValueError(f"status output {name!r} must be a string")
                case RuleMessageSource():
                    pass
        for output_name in extract_outputs:
            if output_name not in self.outputs:
                raise ValueError(f"extract step produces undeclared output {output_name!r}")

        rule_ids = [r.id for r in self.conditions]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("condition rule ids must be unique")

        irreversible = any(s.risk_class is RiskClass.IRREVERSIBLE for s in self.steps)
        if irreversible and not self.policy.requires_human_confirmation:
            raise ValueError("artifact has irreversible steps but does not declare confirmation")
        return self

    def iter_placeholders(self) -> list[tuple[str, str]]:
        """Every ``${name}`` placeholder in the artifact with a description of where it appears."""
        found: list[tuple[str, str]] = []

        def scan(text: str | None, where: str) -> None:
            if text:
                found.extend((m, where) for m in PLACEHOLDER_RE.findall(text))

        def scan_condition(condition: Condition, where: str) -> None:
            match condition:
                case UrlMatches(pattern=p):
                    scan(p, where)
                case TextPresent(text=t) | TextAbsent(text=t) | HeadingPresent(text=t):
                    scan(t, where)
                case ElementValue(value=v):
                    scan(v, where)
                case AllOf(conditions=cs) | AnyOf(conditions=cs):
                    for c in cs:
                        scan_condition(c, where)
                case Not(condition=c):
                    scan_condition(c, where)
                case _:
                    pass

        for step in self.steps:
            scan(step.arguments.value, f"step {step.id} value")
            scan(step.arguments.url, f"step {step.id} url")
            for c in step.preconditions:
                scan_condition(c, f"step {step.id} precondition")
            for c in step.postconditions:
                scan_condition(c, f"step {step.id} postcondition")
        for c in self.checkpoint.conditions:
            scan_condition(c, "checkpoint")
        return found

    def step_by_id(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)
