"""Human-in-the-loop: pause, same-session takeover through the console, approval, abort, resume.

The "human" is an httpx client driving the operator console's JSON API concurrently with the run,
exactly as the ``operator`` CLI does.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from app.api.operator import ConsoleServer, create_operator_app
from app.automation.browser import launch_surface
from app.config import Settings
from app.escalation.human_recorder import install_human_recorder
from app.replay.errors import ReplayStatus
from app.replay.executor import ReplayExecutor
from app.runtime import build_run_context
from app.safety.policy import PolicyEngine

from tests.conftest import SUPERVISOR, TELLER, DemoServer, free_port
from tests.fixtures.artifacts import savings_lookup_artifact, subaccount_artifact

pytestmark = pytest.mark.browser


async def wait_for_pending(client: httpx.AsyncClient) -> dict:
    while True:
        pending = [
            i for i in (await client.get("/api/interventions")).json() if i["status"] == "pending"
        ]
        if pending:
            return pending[0]
        await asyncio.sleep(0.05)


async def attended_replay(
    settings: Settings, demo: DemoServer, artifact, inputs, operator, *, install_recorder=False
):
    policy = PolicyEngine.from_file(settings.policy_file)
    async with launch_surface(settings) as surface:
        ctx = build_run_context(
            kind="replay",
            settings=settings,
            surface=surface,
            attended=True,
            secrets=[inputs["access_code"]],
            console_logging=False,
        )
        if install_recorder:
            assert await install_human_recorder(surface, ctx.escalation)
        console = ConsoleServer(create_operator_app(ctx.escalation, surface), port=free_port())
        await console.start()
        async with httpx.AsyncClient(base_url=console.url, timeout=20) as client:
            operator_task = asyncio.create_task(operator(client, surface))
            executor = ReplayExecutor(
                surface=ctx.automation_surface,
                policy=policy,
                settings=settings,
                events=ctx.events,
                evidence=ctx.evidence,
                escalation=ctx.escalation,
                redactor=ctx.redactor,
            )
            result = await executor.replay(artifact, inputs, base_url=demo.base_url)
            await operator_task
        await console.stop()
    return result, ctx


async def test_takeover_on_ambiguity_then_resume_on_same_session(settings, demo) -> None:
    await demo.inject(duplicate_search_form=True)

    async def operator(client: httpx.AsyncClient, surface) -> None:
        pending = await wait_for_pending(client)
        assert (
            pending["kind"] == "target_unresolved" and pending["current_step"] == "s06-type-member"
        )
        state = (await client.get("/api/state")).json()
        assert state["session_state"] == "ESCALATED" and state["control_owner"] is None
        assert (await client.get(f"/interventions/{pending['id']}")).status_code == 200  # HTML page
        png = await client.get(f"/interventions/{pending['id']}/screenshot.png")
        assert png.headers["content-type"] == "image/png"

        taken = await client.post(f"/api/interventions/{pending['id']}/take-control")
        assert taken.json()["status"] == "human_control"
        assert (await client.get("/api/state")).json()["control_owner"] == "human"
        assert (await client.post(f"/api/interventions/{pending['id']}/approve")).status_code == 409

        observed = (await client.get(f"/api/interventions/{pending['id']}/observe")).json()
        fields = [c["ref"] for c in observed["controls"] if "Member Number" in c["description"]]
        buttons = [c["ref"] for c in observed["controls"] if "'Search'" in c["description"]]
        assert len(fields) == 2 and len(buttons) == 2
        typed = await client.post(
            f"/api/interventions/{pending['id']}/actions",
            json={"action": "type", "ref": fields[0], "value": "12345"},
        )
        assert typed.json()["ok"] and typed.json()["source"] == "console"
        clicked = await client.post(
            f"/api/interventions/{pending['id']}/actions",
            json={"action": "click", "ref": buttons[0]},
        )
        assert clicked.json()["url_after"].endswith("/members/12345")
        released = await client.post(
            f"/api/interventions/{pending['id']}/release", json={"note": "used the main form"}
        )
        assert released.json()["status"] == "released"

    result, ctx = await attended_replay(
        settings,
        demo,
        savings_lookup_artifact(demo.base_url),
        {"member_id": "12345", **TELLER},
        operator,
    )
    assert result.status is ReplayStatus.SUCCESS
    assert str(result.outputs["savings_balance"]) == "8432.17"
    assert result.human_interventions == 1
    assert result.human_completed_steps == ["s06-type-member", "s07-search"]
    assert [b.value for _, b in ctx.session.history] == [
        "ESCALATED",
        "HUMAN_CONTROL",
        "RESUMING",
        "AUTOMATION",
        "COMPLETED",
    ]
    intervention = json.loads((Path(result.evidence_dir) / "int-001.json").read_text())
    assert intervention["operator_note"] == "used the main form"
    assert [(a["source"], a["action"]) for a in intervention["human_action_log"]] == [
        ("console", "type"),
        ("console", "click"),
    ]
    types = [e.event_type.value for e in ctx.events.events]
    for expected in (
        "ESCALATION_CREATED",
        "HUMAN_CONTROL_GRANTED",
        "HUMAN_ACTION",
        "HUMAN_CONTROL_RELEASED",
        "ESCALATION_RESOLVED",
    ):
        assert expected in types


async def test_irreversible_step_is_approved_by_a_human(settings, demo) -> None:
    async def operator(client: httpx.AsyncClient, surface) -> None:
        pending = await wait_for_pending(client)
        assert pending["kind"] == "approval_required"
        assert pending["pending_action"] == "click button 'Confirm and Open Account'"
        assert demo.state.created_accounts == []  # nothing committed while waiting
        approved = await client.post(
            f"/api/interventions/{pending['id']}/approve", json={"note": "verified"}
        )
        assert approved.json()["status"] == "approved"

    result, ctx = await attended_replay(
        settings,
        demo,
        subaccount_artifact(demo.base_url),
        {"member_id": "12345", "nickname": "Emergency Fund", **SUPERVISOR},
        operator,
    )
    assert result.status is ReplayStatus.SUCCESS
    assert result.outputs["confirmation_id"].startswith("SA-")
    assert result.human_interventions == 1 and result.human_completed_steps == []
    assert len(demo.state.created_accounts) == 1
    assert [b.value for _, b in ctx.session.history] == ["ESCALATED", "AUTOMATION", "COMPLETED"]


async def test_operator_abort_ends_the_run_as_escalated(settings, demo) -> None:
    async def operator(client: httpx.AsyncClient, surface) -> None:
        pending = await wait_for_pending(client)
        aborted = await client.post(
            f"/api/interventions/{pending['id']}/abort", json={"note": "not today"}
        )
        assert aborted.json()["status"] == "aborted"

    result, ctx = await attended_replay(
        settings,
        demo,
        subaccount_artifact(demo.base_url),
        {"member_id": "12345", "nickname": "Emergency Fund", **SUPERVISOR},
        operator,
    )
    assert result.status is ReplayStatus.ESCALATED
    assert result.failure.details["escalation"] == "ABORTED_BY_OPERATOR"
    assert demo.state.created_accounts == []
    assert ctx.session.state.value == "ABORTED"


async def test_operator_pause_hands_over_at_the_next_step_boundary(settings, demo) -> None:
    async def operator(client: httpx.AsyncClient, surface) -> None:
        assert (await client.post("/api/pause")).json()["pause_requested"] is True
        pending = await wait_for_pending(client)
        assert pending["kind"] == "operator_pause"
        await client.post(f"/api/interventions/{pending['id']}/take-control")
        await client.post(
            f"/api/interventions/{pending['id']}/release", json={"note": "just looking"}
        )

    result, _ = await attended_replay(
        settings,
        demo,
        savings_lookup_artifact(demo.base_url),
        {"member_id": "12345", **TELLER},
        operator,
    )
    assert result.status is ReplayStatus.SUCCESS and result.human_interventions == 1


async def test_intervention_timeout_is_reported(settings, demo) -> None:
    quick = settings.model_copy(update={"escalation_timeout_s": 0.5})

    async def operator(client: httpx.AsyncClient, surface) -> None:
        await wait_for_pending(client)  # look, but never respond

    result, _ = await attended_replay(
        quick,
        demo,
        subaccount_artifact(demo.base_url),
        {"member_id": "12345", "nickname": "Emergency Fund", **SUPERVISOR},
        operator,
    )
    assert result.status is ReplayStatus.ESCALATED
    assert result.failure.details["escalation"] == "INTERVENTION_TIMEOUT"


async def test_direct_browser_actions_are_recorded_during_human_control(settings, demo) -> None:
    """With a headed browser the human can click in the window itself; those clicks are captured."""
    await demo.inject(duplicate_search_form=True)

    async def operator(client: httpx.AsyncClient, surface) -> None:
        pending = await wait_for_pending(client)
        await client.post(f"/api/interventions/{pending['id']}/take-control")
        # Simulate the human clicking the sidebar "Search" button directly in the browser window.
        await surface.page.locator("div.side input[type=submit]").click()
        await surface.page.wait_for_timeout(200)
        shown = (await client.get(f"/api/interventions/{pending['id']}")).json()
        assert any(a["source"] == "browser" for a in shown["human_action_log"])
        await client.post(f"/api/interventions/{pending['id']}/abort", json={"note": "demo"})

    result, _ = await attended_replay(
        settings,
        demo,
        savings_lookup_artifact(demo.base_url),
        {"member_id": "12345", **TELLER},
        operator,
        install_recorder=True,
    )
    assert result.status is ReplayStatus.ESCALATED
    intervention = json.loads((Path(result.evidence_dir) / "int-001.json").read_text())
    browser_actions = [a for a in intervention["human_action_log"] if a["source"] == "browser"]
    assert browser_actions and browser_actions[0]["action"] in {"click", "submit"}
    assert "Search" in (browser_actions[0]["target"] or "")
