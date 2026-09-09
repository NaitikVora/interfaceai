"""Genuine LLM discovery against the live demo app. Skipped unless LLM_API_KEY is configured.

Run with ``make test-live``. This is the one test that talks to a real model provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.agent.loop import DiscoveryStatus
from app.agent.planner import OpenAICompatiblePlanner
from app.agent.recorder import DiscoveryInput
from app.artifacts.serializer import load_artifact
from app.config import Settings
from app.orchestration import run_discovery, run_replay
from app.replay.errors import ReplayStatus

from tests.conftest import TELLER, DemoServer

pytestmark = [pytest.mark.live_llm, pytest.mark.browser]


@pytest.fixture
def live_settings(settings: Settings) -> Settings:
    real = Settings()  # reads .env / environment
    if not real.llm_configured:
        pytest.skip("LLM_API_KEY not configured")
    return settings.model_copy(
        update={
            "llm_api_key": real.llm_api_key,
            "llm_model": real.llm_model,
            "llm_base_url": real.llm_base_url,
            "llm_timeout_s": real.llm_timeout_s,
        }
    )


async def test_real_model_discovers_and_artifact_replays(
    live_settings: Settings, demo: DemoServer
) -> None:
    result = await run_discovery(
        settings=live_settings,
        planner=OpenAICompatiblePlanner(live_settings),
        goal="Look up member 12345 and read their current savings balance",
        entry_url=f"{demo.base_url}/login",
        inputs=[
            DiscoveryInput("member_id", "12345", False),
            DiscoveryInput("operator_id", TELLER["operator_id"], False),
            DiscoveryInput("access_code", TELLER["access_code"], True),
        ],
        capability_name="member_savings_lookup_live",
        attended=False,
        console_logging=False,
    )
    assert result.status is DiscoveryStatus.COMPLETED, result
    assert result.llm_calls >= 5
    assert result.extracted.get("savings_balance") == "8432.17"
    artifact = load_artifact(Path(result.artifact_path))
    assert "teller-pass" not in Path(result.artifact_path).read_text()
    events_path = Path(result.evidence_dir) / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert any(
        e["event_type"] == "LLM_DECISION" and e["data"]["model"] == live_settings.llm_model
        for e in events
    )

    replayed = await run_replay(
        settings=live_settings,
        artifact=artifact,
        inputs={"member_id": "12345", **TELLER},
        attended=False,
        base_url=demo.base_url,
        console_logging=False,
    )
    assert replayed.status is ReplayStatus.SUCCESS and replayed.llm_calls == 0
    assert str(replayed.outputs["savings_balance"]) == "8432.17"
