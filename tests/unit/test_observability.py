"""Events, evidence store and the validator lints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.artifacts.schema import ActionType, RetryPolicy, RiskClass, Step, TargetSpec
from app.artifacts.validator import Severity, lint_artifact
from app.observability.events import EventLog, EventType, JsonlEventSink
from app.observability.evidence import EvidenceStore, UnsafeEvidenceNameError, run_directory
from app.safety.redaction import Redactor

from tests.fixtures.artifacts import savings_lookup_artifact, subaccount_artifact

BASE = "http://localhost:8000"


def test_event_log_redacts_and_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog("run-1", redactor=Redactor(["teller-pass"]), sinks=[JsonlEventSink(path)])
    log.emit(EventType.ACTION_REQUESTED, step_id="s3", value="teller-pass", password="x", note="ok")
    log.emit(EventType.REPLAY_COMPLETED, status="SUCCESS")
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2 and lines[0]["event_type"] == "ACTION_REQUESTED"
    assert lines[0]["data"] == {"value": "[REDACTED]", "password": "[REDACTED]", "note": "ok"}
    assert lines[0]["step_id"] == "s3" and lines[0]["run_id"] == "run-1"
    assert log.count(EventType.REPLAY_COMPLETED) == 1


def test_evidence_store_names_are_safe_and_redacted(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "run", Redactor(["teller-pass"]))
    ref = store.save_text("page", "typed teller-pass for acct 8801238765")
    assert ref == "page.txt"
    content = (tmp_path / "run" / "page.txt").read_text()
    assert "teller-pass" not in content and "[REDACTED:account]" in content
    store.save_json("result", {"access_code": "secret-value", "ok": True})
    assert json.loads((tmp_path / "run" / "result.json").read_text())["access_code"] == "[REDACTED]"
    assert store.save_png("shot", b"png") == "shot.png"
    for bad in ("../x", "/abs", "a/b", "UPPER", ""):
        with pytest.raises(UnsafeEvidenceNameError):
            store.save_text(bad, "x")
    assert (
        EvidenceStore.step_file_name(3, "s03-Type Code", "failed") == "step-03-s03-type-code-failed"
    )
    assert store.refs == ["page.txt", "result.json", "shot.png"]


def test_run_directory_rejects_traversal(tmp_path: Path) -> None:
    assert run_directory(tmp_path, "replay", "run-1") == (tmp_path / "replay" / "run-1").resolve()
    with pytest.raises(UnsafeEvidenceNameError):
        run_directory(tmp_path, "replay", "../escape")


def test_lint_flags_brittle_targets_and_risky_retries() -> None:
    assert lint_artifact(savings_lookup_artifact(BASE)) == []
    artifact = subaccount_artifact(BASE)
    brittle = artifact.steps[10].model_copy(
        update={
            "target": TargetSpec(css="#confirm", frame="iframe"),
            "retry_policy": RetryPolicy(max_attempts=3),
        }
    )
    assert brittle.risk_class is RiskClass.IRREVERSIBLE
    steps = [*artifact.steps[:10], brittle, *artifact.steps[11:]]
    findings = lint_artifact(artifact.model_copy(update={"steps": steps}))
    messages = [f.message for f in findings if f.severity is Severity.WARNING]
    assert any("only structural strategies" in m for m in messages)
    assert any("frames" in m for m in messages)
    assert any("double-submit" in m for m in messages)
    no_post = Step(id="x", action=ActionType.CLICK, description="d", target=TargetSpec(css="#a"))
    findings = lint_artifact(artifact.model_copy(update={"steps": [*artifact.steps, no_post]}))
    assert any("no postcondition" in f.message for f in findings)
