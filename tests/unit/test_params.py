"""Parameter substitution, typed parsing and input validation."""

from __future__ import annotations

from decimal import Decimal

import pytest
from app.artifacts.params import (
    InvalidInputError,
    ParameterError,
    ValueParseError,
    find_placeholders,
    parse_value,
    substitute,
    validate_inputs,
)
from app.artifacts.schema import ValueType

from tests.fixtures.artifacts import savings_lookup_artifact

BASE = "http://localhost:8000"


def test_substitute_replaces_all_placeholders() -> None:
    assert (
        substitute("${base_url}/members/${id}", {"base_url": BASE, "id": "1"})
        == f"{BASE}/members/1"
    )
    assert find_placeholders("${a} ${b} ${a}") == {"a", "b"}


def test_substitute_fails_loudly_on_missing_parameter() -> None:
    with pytest.raises(ParameterError, match="member_id"):
        substitute("/members/${member_id}", {})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$8,432.17", Decimal("8432.17")),
        ("8432.17", Decimal("8432.17")),
        ("($1,234.00)", Decimal("-1234.00")),
        ("-12.5", Decimal("-12.5")),
        (" 42 CR", Decimal("42")),
        ("€1.000,50".replace(".", "").replace(",", "."), Decimal("1000.50")),
    ],
)
def test_parse_decimal_handles_legacy_money_formats(raw: str, expected: Decimal) -> None:
    assert parse_value(raw, ValueType.DECIMAL) == expected


def test_parse_other_types() -> None:
    assert parse_value(" 12 ", ValueType.INTEGER) == 12
    assert parse_value("Active", ValueType.BOOLEAN) is True
    assert parse_value("no", ValueType.BOOLEAN) is False
    assert parse_value("  text ", ValueType.STRING) == "text"
    with pytest.raises(ValueParseError):
        parse_value("abc", ValueType.DECIMAL)
    with pytest.raises(ValueParseError):
        parse_value("1.5", ValueType.INTEGER)
    with pytest.raises(ValueParseError):
        parse_value("maybe", ValueType.BOOLEAN)


def test_validate_inputs_enforces_the_contract() -> None:
    artifact = savings_lookup_artifact(BASE)
    good = {"member_id": "12345", "operator_id": "t", "access_code": "p"}
    assert validate_inputs(artifact, good) == good

    with pytest.raises(InvalidInputError, match="unexpected input 'bogus'"):
        validate_inputs(artifact, {**good, "bogus": "1"})
    with pytest.raises(InvalidInputError, match="missing required input 'member_id'"):
        validate_inputs(artifact, {"operator_id": "t", "access_code": "p"})
    with pytest.raises(InvalidInputError, match="does not match pattern"):
        validate_inputs(artifact, {**good, "member_id": "abc"})
    with pytest.raises(InvalidInputError, match="must be a string, got int"):
        validate_inputs(artifact, {**good, "member_id": 12345})
    with pytest.raises(InvalidInputError, match="reserved"):
        validate_inputs(artifact, {**good, "base_url": "http://evil"})


def test_validate_inputs_reports_all_problems_at_once() -> None:
    artifact = savings_lookup_artifact(BASE)
    with pytest.raises(InvalidInputError) as info:
        validate_inputs(artifact, {"member_id": "x", "extra": "y"})
    assert len(info.value.problems) == 4
