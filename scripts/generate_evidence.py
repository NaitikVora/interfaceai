"""Produce the committed evidence set end to end.

    python scripts/generate_evidence.py            # genuine LLM discovery (needs LLM_API_KEY)
    python scripts/generate_evidence.py --planner scripted --evidence-root /tmp/ev   # pipeline check

Runs, in order, against the demo app (started in-process if DEMO_APP_URL is not reachable):

  1. discovery of the savings-balance lookup            -> evidence/discovery/<run>/ + artifact
  2. replay 12345 (SUCCESS)                             -> evidence/replay/<run>/
  3. replay 99999 (BUSINESS_OUTCOME / MEMBER_NOT_FOUND) -> evidence/replay/<run>/
  4. replay with one injected core outage (recovered)   -> evidence/replay/<run>/
  5. replay with injected application error (HARD)      -> evidence/failure/<run>/
  6. replay with duplicate form, no operator (ESCALATED)-> evidence/failure/<run>/
  7. replay with duplicate form, operator resolves it
     through the console API (SUCCESS after handoff)    -> evidence/replay/<run>/
  8. optionally, discovery of the sub-account flow with
     the irreversible step approved through the console -> evidence/discovery/<run>/ + artifact

and finally writes evidence/README.md indexing every run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import uvicorn

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.agent.loop import DiscoveryResult, DiscoveryStatus  # noqa: E402
from app.agent.planner import OpenAICompatiblePlanner, Planner  # noqa: E402
from app.agent.recorder import DiscoveryInput  # noqa: E402
from app.artifacts.serializer import load_artifact  # noqa: E402
from app.config import Settings  # noqa: E402
from app.observability.logging import configure_logging  # noqa: E402
from app.orchestration import run_discovery, run_replay  # noqa: E402
from app.replay.errors import ReplayResult  # noqa: E402
from demo_app.main import create_app  # noqa: E402
from demo_app.state import AppState  # noqa: E402

LOOKUP_GOAL = "Look up member 12345 and read their current savings balance"
SUBACCOUNT_GOAL = (
    "Look up member 12345, open a new Savings sub-account with nickname Emergency Fund, "
    "and reach the confirmation screen"
)


@dataclass
class Entry:
    title: str
    kind: str
    run_id: str
    status: str
    detail: str
    files: list[str] = field(default_factory=list)


@dataclass
class Report:
    entries: list[Entry] = field(default_factory=list)
    model: str = ""

    def add(self, title: str, result: ReplayResult | DiscoveryResult, detail: str) -> None:
        run_dir = Path(result.evidence_dir)
        files = sorted(p.name for p in run_dir.iterdir())
        self.entries.append(
            Entry(
                title=title,
                kind=run_dir.parent.name,
                run_id=result.run_id,
                status=result.status.value,
                detail=detail,
                files=files,
            )
        )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def ensure_demo_app(
    settings: Settings,
) -> tuple[str, uvicorn.Server | None, asyncio.Task[None] | None]:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{settings.demo_app_url}/login")
        if response.status_code == 200:
            return settings.demo_app_url, None, None
    except httpx.HTTPError:
        pass
    port = settings.demo_app_port
    server = uvicorn.Server(
        uvicorn.Config(create_app(AppState()), host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.05)
    return f"http://127.0.0.1:{port}", server, task


async def inject(base: str, **flags: object) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=10) as client:
        (await client.post("/__admin/reset")).raise_for_status()
        if flags:
            (await client.post("/__admin/inject", json=flags)).raise_for_status()


async def operator_resolves_ambiguity(console: str, member_id: str) -> None:
    """A human operator, driven through the console's JSON API, completes the search."""
    async with httpx.AsyncClient(base_url=console, timeout=30) as client:
        while True:
            try:
                items = (await client.get("/api/interventions")).json()
            except httpx.HTTPError:
                items = []
            pending = [i for i in items if i["status"] == "pending"]
            if pending:
                break
            await asyncio.sleep(0.1)
        intervention = pending[0]["id"]
        await client.post(f"/api/interventions/{intervention}/take-control")
        observed = (await client.get(f"/api/interventions/{intervention}/observe")).json()
        field_ref = next(
            c["ref"] for c in observed["controls"] if "Member Number" in c["description"]
        )
        button_ref = next(c["ref"] for c in observed["controls"] if "'Search'" in c["description"])
        await client.post(
            f"/api/interventions/{intervention}/actions",
            json={"action": "type", "ref": field_ref, "value": member_id},
        )
        await client.post(
            f"/api/interventions/{intervention}/actions",
            json={"action": "click", "ref": button_ref},
        )
        await client.post(
            f"/api/interventions/{intervention}/release",
            json={"note": "operator used the main search form and ran the search"},
        )


async def operator_approves(console: str) -> None:
    async with httpx.AsyncClient(base_url=console, timeout=30) as client:
        while True:
            try:
                items = (await client.get("/api/interventions")).json()
            except httpx.HTTPError:
                items = []
            pending = [i for i in items if i["status"] == "pending"]
            if pending:
                break
            await asyncio.sleep(0.1)
        await client.post(
            f"/api/interventions/{pending[0]['id']}/approve",
            json={"note": "reviewed the request on the review screen; approved"},
        )


def make_planner(kind: str, settings: Settings, script_name: str) -> Planner:
    if kind == "openai":
        return OpenAICompatiblePlanner(settings)
    from tests.fakes.scripted_planner import (  # noqa: PLC0415 - test double, deliberately local
        ScriptedPlanner,
        savings_lookup_script,
        subaccount_script,
    )

    script = savings_lookup_script if script_name == "lookup" else subaccount_script
    return ScriptedPlanner(script)


async def main(args: argparse.Namespace) -> int:
    configure_logging("WARNING")
    settings = Settings()
    if args.evidence_root:
        settings = settings.model_copy(update={"evidence_dir": Path(args.evidence_root)})
    if args.artifacts_dir:
        settings = settings.model_copy(update={"artifacts_dir": Path(args.artifacts_dir)})
    if args.planner == "openai" and not settings.llm_configured:
        print(
            "LLM_API_KEY is not configured; set it in .env or use --planner scripted",
            file=sys.stderr,
        )
        return 2

    base, server, server_task = await ensure_demo_app(settings)
    teller = {
        "operator_id": settings.demo_operator_id,
        "access_code": settings.demo_access_code.get_secret_value(),
    }
    supervisor = {
        "operator_id": settings.demo_supervisor_id,
        "access_code": settings.demo_supervisor_code.get_secret_value(),
    }
    report = Report(
        model=settings.llm_model if args.planner == "openai" else "scripted-test-double"
    )
    common = {"settings": settings, "attended": False, "base_url": base, "console_logging": False}

    try:
        # 1. discovery -------------------------------------------------------------------
        await inject(base)
        planner = make_planner(args.planner, settings, "lookup")
        discovery = await run_discovery(
            settings=settings,
            planner=planner,
            goal=LOOKUP_GOAL,
            entry_url=f"{base}/login",
            inputs=[
                DiscoveryInput("member_id", "12345", False),
                DiscoveryInput("operator_id", teller["operator_id"], False),
                DiscoveryInput("access_code", teller["access_code"], True),
            ],
            capability_name="member_savings_lookup",
            attended=False,
            console_logging=False,
        )
        print(discovery.render())
        report.add(
            "LLM discovery of the savings-balance lookup",
            discovery,
            f"{discovery.llm_calls} model decisions, {discovery.steps_recorded} steps recorded, "
            f"extracted {discovery.extracted}",
        )
        if discovery.status is not DiscoveryStatus.COMPLETED:
            print("discovery did not complete; stopping", file=sys.stderr)
            return 1
        artifact = load_artifact(Path(discovery.artifact_path))

        # 2..7 replays ---------------------------------------------------------------------
        await inject(base)
        result = await run_replay(
            artifact=artifact, inputs={"member_id": "12345", **teller}, **common
        )
        print(result.render())
        report.add(
            "Deterministic replay, member 12345",
            result,
            f"outputs={json.dumps(result.model_dump(mode='json')['outputs'])}",
        )

        result = await run_replay(
            artifact=artifact, inputs={"member_id": "99999", **teller}, **common
        )
        print(result.render())
        report.add(
            "Replay, member 99999: business outcome, not a crash",
            result,
            f"{result.failure.code if result.failure else ''}: {result.outputs.get('message')}",
        )

        await inject(base, transient_search_failures=1)
        result = await run_replay(
            artifact=artifact, inputs={"member_id": "12345", **teller}, **common
        )
        print(result.render())
        report.add(
            "Replay with one injected core outage: recovered",
            result,
            f"recoveries_applied={result.recoveries_applied}",
        )

        await inject(base, app_error_on_search=True)
        result = await run_replay(
            artifact=artifact,
            inputs={"member_id": "12345", **teller},
            evidence_kind="failure",
            **common,
        )
        print(result.render())
        report.add(
            "Replay with injected application error: hard failure with screenshot",
            result,
            f"{result.failure.code if result.failure else ''} at {result.step_id}; "
            f"screenshot {result.failure.screenshot_ref if result.failure else ''}",
        )

        await inject(base, duplicate_search_form=True)
        result = await run_replay(
            artifact=artifact,
            inputs={"member_id": "12345", **teller},
            evidence_kind="failure",
            **common,
        )
        print(result.render())
        report.add(
            "Replay with duplicate search form, no operator: ambiguous target, escalated",
            result,
            f"{result.failure.code if result.failure else ''} "
            f"({result.failure.details.get('escalation') if result.failure else ''}); "
            "locator diagnostics in result.json",
        )

        await inject(base, duplicate_search_form=True)
        port = free_port()
        operator_task = asyncio.create_task(
            operator_resolves_ambiguity(f"http://127.0.0.1:{port}", "12345")
        )
        result = await run_replay(
            settings=settings,
            artifact=artifact,
            inputs={"member_id": "12345", **teller},
            attended=True,
            base_url=base,
            console_port=port,
            console_logging=False,
        )
        await operator_task
        print(result.render())
        report.add(
            "Replay with duplicate form, operator takes over the same session and releases",
            result,
            f"human_interventions={result.human_interventions}, "
            f"human_completed_steps={result.human_completed_steps}; see int-001.json",
        )

        # 8. optional sub-account discovery with approval -----------------------------------
        if args.with_subaccount:
            await inject(base)
            port = free_port()
            approver = asyncio.create_task(operator_approves(f"http://127.0.0.1:{port}"))
            sub = await run_discovery(
                settings=settings,
                planner=make_planner(args.planner, settings, "subaccount"),
                goal=SUBACCOUNT_GOAL,
                entry_url=f"{base}/login",
                inputs=[
                    DiscoveryInput("member_id", "12345", False),
                    DiscoveryInput("nickname", "Emergency Fund", False),
                    DiscoveryInput("operator_id", supervisor["operator_id"], False),
                    DiscoveryInput("access_code", supervisor["access_code"], True),
                ],
                capability_name="open_savings_subaccount",
                attended=True,
                console_port=port,
                console_logging=False,
            )
            if not approver.done():
                approver.cancel()
            print(sub.render())
            report.add(
                "LLM discovery of the sub-account flow; irreversible confirm approved via console",
                sub,
                f"{sub.llm_calls} model decisions, {sub.steps_recorded} steps; "
                f"extracted {sub.extracted}",
            )
            if sub.status is DiscoveryStatus.COMPLETED:
                sub_artifact = load_artifact(Path(sub.artifact_path))
                await inject(base)
                denied = await run_replay(
                    artifact=sub_artifact,
                    inputs={"member_id": "12345", "nickname": "Vacation", **teller},
                    **common,
                )
                print(denied.render())
                report.add(
                    "Replay of the sub-account flow as a teller: permission denied is a business outcome",
                    denied,
                    f"{denied.failure.code if denied.failure else ''}: {denied.outputs.get('message')}",
                )
    finally:
        await inject(base)
        if server is not None and server_task is not None:
            server.should_exit = True
            await server_task

    write_index(settings.evidence_dir, report)
    print(f"\nwrote {settings.evidence_dir / 'README.md'} with {len(report.entries)} runs")
    return 0


def write_index(root: Path, report: Report) -> None:
    lines = [
        "# Evidence",
        "",
        f"Generated by `python scripts/generate_evidence.py` against the local DemoBank LegacyCore demo. "
        f"Model used for discovery: `{report.model}`. All data is synthetic; secrets and PII are redacted.",
        "",
        "Each run directory contains `events.jsonl` (structured event log), `result.json`, screenshots, "
        "and for discovery runs the per-step `llm-call-NN.json` (redacted prompt + raw model response) and "
        "the produced `artifact.json`. Replay runs contain **no** `LLM_*` events and report `llm_calls: 0`.",
        "",
        "| # | Run | Kind | Status | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]
    for index, entry in enumerate(report.entries, start=1):
        lines.append(
            f"| {index} | [`{entry.run_id}`]({entry.kind}/{entry.run_id}/) | {entry.kind} | "
            f"`{entry.status}` | {entry.title}. {entry.detail} |"
        )
    lines.append("")
    lines.append("## Files per run")
    lines.append("")
    for entry in report.entries:
        lines.append(
            f"- `{entry.kind}/{entry.run_id}/`: " + ", ".join(f"`{f}`" for f in entry.files)
        )
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--planner", choices=["openai", "scripted"], default="openai")
    parser.add_argument("--evidence-root", default=None, help="Override EVIDENCE_DIR")
    parser.add_argument("--artifacts-dir", default=None, help="Override ARTIFACTS_DIR")
    parser.add_argument(
        "--with-subaccount", action="store_true", help="Also discover the sub-account flow"
    )
    sys.exit(asyncio.run(main(parser.parse_args())))
