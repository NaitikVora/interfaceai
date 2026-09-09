"""Parameter substitution, input validation and typed value parsing for artifacts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from app.artifacts.schema import (
    PLACEHOLDER_RE,
    RESERVED_PARAMETERS,
    CapabilityArtifact,
    InputSpec,
    ValueType,
)

ParsedValue = str | Decimal | int | bool

_MONEY_STRIP_RE = re.compile(r"[\s$,€£]")
_ACCOUNTING_NEGATIVE_RE = re.compile(r"^\((.*)\)$")


class ParameterError(ValueError):
    """A placeholder could not be resolved."""


class InvalidInputError(ValueError):
    """Caller-supplied inputs violate the artifact's input contract."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


class ValueParseError(ValueError):
    """Extracted text could not be parsed as the declared output type."""


def find_placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER_RE.findall(text))


def substitute(text: str, values: Mapping[str, str]) -> str:
    """Replace every ``${name}`` in ``text``; unknown names are an error, never left in place."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ParameterError(f"no value for parameter ${{{name}}}")
        return values[name]

    return PLACEHOLDER_RE.sub(replace, text)


def parse_value(raw: str, value_type: ValueType) -> ParsedValue:
    """Parse displayed text into the declared type. Handles legacy money formats."""
    text = raw.strip()
    match value_type:
        case ValueType.STRING:
            return text
        case ValueType.DECIMAL:
            return _parse_decimal(text)
        case ValueType.INTEGER:
            cleaned = _MONEY_STRIP_RE.sub("", text)
            if not re.fullmatch(r"-?\d+", cleaned):
                raise ValueParseError(f"{raw!r} is not an integer")
            return int(cleaned)
        case ValueType.BOOLEAN:
            lowered = text.lower()
            if lowered in {"true", "yes", "y", "1", "on", "active", "checked"}:
                return True
            if lowered in {"false", "no", "n", "0", "off", "inactive", "unchecked"}:
                return False
            raise ValueParseError(f"{raw!r} is not a boolean")
    raise ValueParseError(f"unsupported type {value_type}")  # pragma: no cover


def _parse_decimal(text: str) -> Decimal:
    negative = False
    if m := _ACCOUNTING_NEGATIVE_RE.match(text):
        negative, text = True, m.group(1)
    if text.upper().endswith(("CR", "DR")):
        text = text[:-2]
    cleaned = _MONEY_STRIP_RE.sub("", text)
    if cleaned.startswith("-"):
        negative, cleaned = not negative, cleaned[1:]
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueParseError(f"{text!r} is not a decimal amount") from exc
    return -value if negative else value


def validate_inputs(artifact: CapabilityArtifact, provided: Mapping[str, object]) -> dict[str, str]:
    """Check caller inputs against the contract and return the effective values.

    Unknown inputs are rejected (a typo must not silently become an empty field), required
    inputs must be present, values must be strings (JSON callers may send numbers), and
    pattern/enum/type constraints are enforced *before* a browser is touched.
    """
    problems: list[str] = []
    for name in provided:
        if name not in artifact.inputs:
            problems.append(f"unexpected input {name!r}")
        if name in RESERVED_PARAMETERS:
            problems.append(f"input {name!r} is reserved and set by the runtime")

    values: dict[str, str] = {}
    for name, spec in artifact.inputs.items():
        if name in provided:
            raw = provided[name]
            if not isinstance(raw, str):
                problems.append(f"input {name!r} must be a string, got {type(raw).__name__}")
                continue
            values[name] = raw
        elif spec.default is not None:
            values[name] = spec.default
        elif spec.required:
            problems.append(f"missing required input {name!r}")
            continue
        else:
            continue
        problems.extend(_check_input(name, spec, values[name]))

    if problems:
        raise InvalidInputError(problems)
    return values


def _check_input(name: str, spec: InputSpec, value: str) -> list[str]:
    problems: list[str] = []
    if spec.pattern and not re.fullmatch(spec.pattern, value):
        problems.append(f"input {name!r} does not match pattern {spec.pattern!r}")
    if spec.enum is not None and value not in spec.enum:
        problems.append(f"input {name!r} must be one of {spec.enum}")
    if spec.type is not ValueType.STRING:
        try:
            parse_value(value, spec.type)
        except ValueParseError:
            problems.append(f"input {name!r} is not a valid {spec.type}")
    return problems
