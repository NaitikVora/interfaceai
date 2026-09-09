"""Artifact schema: validation, integrity checks, serialization and path safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.artifacts.schema import (
    ActionType,
    CapabilityArtifact,
    ElementValue,
    OutcomeCategory,
    Step,
    StepArguments,
    Strategy,
    TableCellSpec,
    TargetSpec,
    TextPresent,
    describe_condition,
)
from app.artifacts.schema import ConditionRule as Rule
from app.artifacts.serializer import (
    ArtifactLoadError,
    UnsafeArtifactPathError,
    load_artifact,
    safe_artifact_path,
    save_artifact,
    to_json,
)
from pydantic import ValidationError

from tests.fixtures.artifacts import savings_lookup_artifact, subaccount_artifact

BASE = "http://localhost:8000"


def test_artifact_round_trips_through_json(tmp_path: Path) -> None:
    artifact = savings_lookup_artifact(BASE)
    path = save_artifact(artifact, tmp_path)
    assert path.name == "member_savings_lookup.v1.json"
    loaded = load_artifact(path)
    assert loaded == artifact
    assert json.loads(path.read_text())["schema_version"] == "1.0"


def test_artifact_json_is_parameterized_and_secret_free() -> None:
    text = to_json(savings_lookup_artifact(BASE))
    assert "${member_id}" in text and "${access_code}" in text
    assert "12345" not in text and "teller-pass" not in text


def test_unknown_fields_are_rejected() -> None:
    data = savings_lookup_artifact(BASE).model_dump(mode="json")
    data["surprise"] = True
    with pytest.raises(ValidationError, match="surprise"):
        CapabilityArtifact.model_validate(data)


def test_undeclared_placeholder_is_rejected() -> None:
    artifact = savings_lookup_artifact(BASE)
    data = artifact.model_dump(mode="json")
    data["steps"][5]["arguments"]["value"] = "${nope}"
    with pytest.raises(ValidationError, match="undeclared input"):
        CapabilityArtifact.model_validate(data)


def test_duplicate_step_ids_are_rejected() -> None:
    data = savings_lookup_artifact(BASE).model_dump(mode="json")
    data["steps"][1]["id"] = data["steps"][0]["id"]
    with pytest.raises(ValidationError, match="unique"):
        CapabilityArtifact.model_validate(data)


def test_output_must_be_produced_by_referenced_extract_step() -> None:
    data = savings_lookup_artifact(BASE).model_dump(mode="json")
    data["outputs"]["savings_balance"]["source"]["step_id"] = "s07-search"
    with pytest.raises(ValidationError, match="not extracted by step"):
        CapabilityArtifact.model_validate(data)


def test_irreversible_step_requires_declared_confirmation() -> None:
    data = subaccount_artifact(BASE).model_dump(mode="json")
    data["policy"]["requires_human_confirmation"] = False
    with pytest.raises(ValidationError, match="irreversible"):
        CapabilityArtifact.model_validate(data)


def test_reserved_input_name_is_rejected() -> None:
    data = savings_lookup_artifact(BASE).model_dump(mode="json")
    data["inputs"]["base_url"] = {"type": "string"}
    with pytest.raises(ValidationError, match="reserved"):
        CapabilityArtifact.model_validate(data)


def test_step_argument_consistency() -> None:
    with pytest.raises(ValidationError, match="requires a target"):
        Step(id="a", action=ActionType.CLICK, description="x")
    with pytest.raises(ValidationError, match=r"requires arguments\.value"):
        Step(id="a", action=ActionType.TYPE, description="x", target=TargetSpec(css="#x"))
    with pytest.raises(ValidationError, match="must not have a target"):
        Step(
            id="a",
            action=ActionType.NAVIGATE,
            description="x",
            target=TargetSpec(css="#x"),
            arguments=StepArguments(url="http://x"),
        )
    with pytest.raises(ValidationError, match="extract requires"):
        Step(id="a", action=ActionType.EXTRACT, description="x", target=TargetSpec(css="#x"))


def test_target_spec_needs_a_strategy_and_orders_them() -> None:
    with pytest.raises(ValidationError, match="at least one locator strategy"):
        TargetSpec(description="nothing")
    spec = TargetSpec(css="a", role="button", name="Go", xpath="//a", label="Go", text="Go")
    assert spec.available_strategies() == [
        Strategy.ROLE_NAME,
        Strategy.LABEL,
        Strategy.TEXT,
        Strategy.CSS,
        Strategy.XPATH,
    ]
    assert spec.semantic_strategies() == [Strategy.ROLE_NAME, Strategy.LABEL, Strategy.TEXT]


def test_coordinates_require_viewport_and_scopes_do_not_nest() -> None:
    with pytest.raises(ValidationError, match="viewport"):
        TargetSpec(coordinates={"x": 1, "y": 2})
    inner = TargetSpec(css="#a", within=TargetSpec(css="#b"))
    with pytest.raises(ValidationError, match="do not nest"):
        TargetSpec(css="#c", within=inner)


def test_table_cell_needs_exactly_one_column_address() -> None:
    with pytest.raises(ValidationError):
        TableCellSpec(row_match="Savings")
    with pytest.raises(ValidationError):
        TableCellSpec(row_match="Savings", column_header="A", column_index=1)


def test_recoverable_rule_requires_recovery_and_vice_versa() -> None:
    with pytest.raises(ValidationError, match="must define a recovery"):
        Rule(
            id="r",
            description="d",
            when=TextPresent(text="x"),
            category=OutcomeCategory.RECOVERABLE,
            code="X",
        )
    with pytest.raises(ValidationError, match="not recoverable"):
        Rule(
            id="r",
            description="d",
            when=TextPresent(text="x"),
            category=OutcomeCategory.HARD_FAILURE,
            code="X",
            recovery={"kind": "retry_step"},
        )


def test_describe_condition_is_human_readable() -> None:
    cond = ElementValue(target=TargetSpec(label="Member Number"), value="${member_id}")
    assert describe_condition(cond) == "field labelled 'Member Number' has value '${member_id}'"


def test_safe_artifact_path_rejects_traversal(tmp_path: Path) -> None:
    for bad in ("../x.json", "/etc/passwd.json", "a/b.json", "Weird Name.json", "x.txt"):
        with pytest.raises(UnsafeArtifactPathError):
            safe_artifact_path(tmp_path, bad)
    assert safe_artifact_path(tmp_path, "ok.v1.json").parent == tmp_path.resolve()


def test_malformed_artifact_files_are_rejected(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(ArtifactLoadError, match="cannot read"):
        load_artifact(broken)
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"schema_version": "1.0", "name": "x"}))
    with pytest.raises(ArtifactLoadError, match="invalid"):
        load_artifact(invalid)
