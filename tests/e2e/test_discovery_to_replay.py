"""The whole thread: goal -> (scripted) model-driven discovery -> artifact -> deterministic replay.

The ``ScriptedPlanner`` stands in for the LLM so this runs without network access; it receives
exactly the rendered observation the real model receives and answers with ``Decision`` objects.
The genuine provider path is exercised by ``test_live_llm.py`` when credentials are present.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from app.agent.loop import DiscoveryStatus
from app.agent.recorder import DiscoveryInput
from app.artifacts.schema import RiskClass, Strategy
from app.artifacts.serializer import load_artifact
from app.artifacts.validator import lint_artifact
from app.config import Settings
from app.orchestration import run_discovery, run_replay
from app.replay.errors import ReplayStatus

from tests.conftest import SUPERVISOR, TELLER, DemoServer, free_port
from tests.fakes.scripted_planner import ScriptedPlanner, savings_lookup_script, subaccount_script

pytestmark = pytest.mark.browser

GOAL = "Look up member 12345 and read their current savings balance"


def discovery_inputs(**extra: str) -> list[DiscoveryInput]:
    return [
        DiscoveryInput("member_id", "12345", False),
        DiscoveryInput("operator_id", TELLER["operator_id"], False),
        DiscoveryInput("access_code", TELLER["access_code"], True),
        *[DiscoveryInput(k, v, False) for k, v in extra.items()],
    ]


async def test_savings_lookup_discovery_then_replay(
    settings: Settings, demo: DemoServer, tmp_path: Path
) -> None:
    planner = ScriptedPlanner(savings_lookup_script)
    result = await run_discovery(
        settings=settings,
        planner=planner,
        goal=GOAL,
        entry_url=f"{demo.base_url}/login",
        inputs=discovery_inputs(),
        capability_name="member_savings_lookup",
        attended=False,
        console_logging=False,
    )
    assert result.status is DiscoveryStatus.COMPLETED, result
    assert result.llm_calls == 8 and result.steps_recorded == 8
    assert result.extracted == {"savings_balance": "8432.17"}

    # --- the artifact is a parameterized, verified, reviewable contract -------------------
    artifact_path = Path(result.artifact_path)
    artifact = load_artifact(artifact_path)
    text = artifact_path.read_text()
    assert "12345" not in text and "teller-pass" not in text and "teller1" not in text
    assert set(artifact.inputs) == {"member_id", "operator_id", "access_code"}
    assert (
        artifact.inputs["access_code"].sensitive
        and artifact.inputs["member_id"].pattern == r"^\d+$"
    )
    assert artifact.outputs["savings_balance"].type.value == "decimal"
    assert artifact.outputs["member_id"].source.kind == "input"
    assert [s.action.value for s in artifact.steps] == [
        "navigate",
        "type",
        "type",
        "click",
        "click",
        "type",
        "click",
        "extract",
    ]
    for step in artifact.steps[1:]:
        assert step.preconditions, step.id
    for step in artifact.steps:
        if step.action.value != "extract":
            assert step.postconditions, step.id
    typed = artifact.steps[5]
    assert (
        typed.arguments.value == "${member_id}" and typed.postconditions[0].kind == "element_value"
    )
    menu_link = artifact.steps[4]
    assert menu_link.target is not None and menu_link.target.within is not None
    assert Strategy.ROLE_NAME in menu_link.target.available_strategies()
    assert artifact.steps[2].risk_class is RiskClass.SENSITIVE
    assert artifact.checkpoint.conditions[0].pattern == "/members/${member_id}$"  # type: ignore[union-attr]
    assert lint_artifact(artifact) == []
    assert (
        artifact.metadata.llm_model == "scripted-test-double"
        and artifact.metadata.llm_decisions == 8
    )

    # --- discovery evidence proves the model was in the loop ------------------------------
    evidence = Path(result.evidence_dir)
    names = {p.name for p in evidence.iterdir()}
    assert {"events.jsonl", "artifact.json", "result.json", "final-screenshot.png"} <= names
    assert len([n for n in names if n.startswith("llm-call-")]) == 8
    assert len([n for n in names if n.startswith("step-")]) == 8
    events = [json.loads(line) for line in (evidence / "events.jsonl").read_text().splitlines()]
    assert sum(1 for e in events if e["event_type"] == "LLM_DECISION") == 8
    assert sum(1 for e in events if e["event_type"] == "STEP_RECORDED") == 8
    assert not any("teller-pass" in json.dumps(e) for e in events)
    llm_call = json.loads((evidence / "llm-call-02.json").read_text())
    assert "teller-pass" not in llm_call["prompt"] and '"${access_code}"' in llm_call["prompt"]

    # --- deterministic replay of the discovered artifact ----------------------------------
    for member, expected in (
        ("12345", ReplayStatus.SUCCESS),
        ("99999", ReplayStatus.BUSINESS_OUTCOME),
    ):
        replayed = await run_replay(
            settings=settings,
            artifact=artifact,
            inputs={"member_id": member, **TELLER},
            attended=False,
            base_url=demo.base_url,
            console_logging=False,
        )
        assert replayed.status is expected and replayed.llm_calls == 0
    success = await run_replay(
        settings=settings,
        artifact=artifact,
        inputs={"member_id": "23456", **TELLER},
        attended=False,
        base_url=demo.base_url,
        console_logging=False,
    )
    assert (
        str(success.outputs["savings_balance"]) == "15000.00"
    )  # a different member, same artifact
    rejected = await run_replay(
        settings=settings,
        artifact=artifact,
        inputs={"member_id": "1234", **TELLER},
        attended=False,
        base_url=demo.base_url,
        console_logging=False,
    )
    assert (
        rejected.status is ReplayStatus.BUSINESS_OUTCOME
        and rejected.failure.code == "VALIDATION_REJECTED"
    )


async def test_subaccount_discovery_requires_approval_then_replays(
    settings: Settings, demo: DemoServer
) -> None:
    """The model reaches an irreversible step; policy pauses discovery until a human approves."""
    port = free_port()

    async def approver() -> None:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
            while True:
                try:
                    items = (await client.get("/api/interventions")).json()
                except httpx.HTTPError:
                    items = []
                pending = [i for i in items if i["status"] == "pending"]
                if pending:
                    break
                await asyncio.sleep(0.05)
            assert pending[0]["kind"] == "approval_required"
            assert "Confirm and Open Account" in pending[0]["pending_action"]
            await client.post(f"/api/interventions/{pending[0]['id']}/approve", json={"note": "ok"})

    approver_task = asyncio.create_task(approver())
    result = await run_discovery(
        settings=settings,
        planner=ScriptedPlanner(subaccount_script),
        goal="Look up member 12345, open a new Savings sub-account with nickname Emergency Fund, and reach the confirmation screen",
        entry_url=f"{demo.base_url}/login",
        inputs=[
            DiscoveryInput("member_id", "12345", False),
            DiscoveryInput("nickname", "Emergency Fund", False),
            DiscoveryInput("operator_id", SUPERVISOR["operator_id"], False),
            DiscoveryInput("access_code", SUPERVISOR["access_code"], True),
        ],
        capability_name="open_savings_subaccount",
        attended=True,
        console_port=port,
        console_logging=False,
    )
    await approver_task
    assert result.status is DiscoveryStatus.COMPLETED, result
    artifact = load_artifact(Path(result.artifact_path))
    confirm = next(s for s in artifact.steps if s.risk_class is RiskClass.IRREVERSIBLE)
    assert confirm.target is not None and confirm.target.name == "Confirm and Open Account"
    assert confirm.retry_policy.max_attempts == 1
    assert artifact.policy.requires_human_confirmation is True
    assert "Emergency Fund" not in Path(result.artifact_path).read_text()
    assert artifact.outputs["confirmation_id"].type.value == "string"
    assert len(demo.state.created_accounts) == 1

    # Teller replay: permission denied is a business outcome, nothing is created.
    denied = await run_replay(
        settings=settings,
        artifact=artifact,
        inputs={"member_id": "12345", "nickname": "Vacation", **TELLER},
        attended=False,
        base_url=demo.base_url,
        console_logging=False,
    )
    assert (
        denied.status is ReplayStatus.BUSINESS_OUTCOME
        and denied.failure.code == "INSUFFICIENT_PERMISSION"
    )
    # Supervisor replay without an operator: stops at the gate, nothing is created.
    gated = await run_replay(
        settings=settings,
        artifact=artifact,
        inputs={"member_id": "12345", "nickname": "Vacation", **SUPERVISOR},
        attended=False,
        base_url=demo.base_url,
        console_logging=False,
    )
    assert gated.status is ReplayStatus.ESCALATED and gated.step_id == confirm.id
    assert len(demo.state.created_accounts) == 1
