"""Shared pytest fixtures.

Browser-backed fixtures run the demo application in-process on a free port and launch a real
headless Chromium. The session-scoped event loop lets Playwright, uvicorn and the tests share
one loop, which is exactly how the production process is structured.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from app.automation.browser import PlaywrightSurface, launch_surface
from app.config import Settings
from app.safety.policy import PolicyEngine
from demo_app.main import create_app
from demo_app.state import AppState

REPO_ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class DemoServer:
    base_url: str
    state: AppState

    async def inject(self, **flags: object) -> None:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            response = await client.post("/__admin/inject", json=flags)
            response.raise_for_status()

    async def reset(self) -> None:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            (await client.post("/__admin/reset-all")).raise_for_status()


@pytest.fixture(scope="session")
async def demo_server() -> AsyncIterator[DemoServer]:
    port = free_port()
    state = AppState()
    server = uvicorn.Server(
        uvicorn.Config(create_app(state), host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.02)
    yield DemoServer(base_url=f"http://127.0.0.1:{port}", state=state)
    server.should_exit = True
    await task


@pytest.fixture
async def demo(demo_server: DemoServer) -> AsyncIterator[DemoServer]:
    """The demo app with a clean fault-injection state for each test."""
    await demo_server.reset()
    yield demo_server
    await demo_server.reset()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        headless=True,
        llm_api_key=None,
        evidence_dir=tmp_path / "evidence",
        artifacts_dir=tmp_path / "artifacts",
        policy_file=REPO_ROOT / "policies" / "demobank.json",
        profile_file=REPO_ROOT / "profiles" / "demobank_legacycore.json",
        replay_default_step_timeout_s=5.0,
        replay_max_runtime_s=120.0,
        escalation_timeout_s=30.0,
        agent_max_steps=30,
    )


@pytest.fixture(scope="session")
def policy() -> PolicyEngine:
    return PolicyEngine.from_file(REPO_ROOT / "policies" / "demobank.json")


@pytest.fixture
async def surface(settings: Settings) -> AsyncIterator[PlaywrightSurface]:
    async with launch_surface(settings) as browser_surface:
        yield browser_surface


TELLER = {"operator_id": "teller1", "access_code": "teller-pass"}
SUPERVISOR = {"operator_id": "super1", "access_code": "super-pass"}
