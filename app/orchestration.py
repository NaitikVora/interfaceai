"""Top-level run orchestration used by the CLI and the end-to-end tests.

Wires: browser surface -> run context (events, evidence, session, escalation) -> optional
operator console -> agent loop *or* replay executor. The discovery path needs a ``Planner``; the
replay path never receives one, which is enforced by construction rather than by convention.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import structlog

from app.agent.loop import AgentLoop, DiscoveryResult
from app.agent.planner import Planner
from app.agent.recorder import DiscoveryInput
from app.api.operator import ConsoleServer, create_operator_app
from app.artifacts.profile import ApplicationProfile
from app.artifacts.schema import CapabilityArtifact
from app.automation.browser import launch_surface
from app.config import Settings
from app.escalation.human_recorder import install_human_recorder
from app.escalation.manager import InterventionRequest
from app.observability.evidence import EvidenceKind
from app.replay.errors import ReplayResult
from app.replay.executor import ReplayExecutor
from app.runtime import build_run_context
from app.safety.policy import PolicyEngine

log = structlog.get_logger("orchestration")


def _announce(console_url: str | None) -> Callable[[InterventionRequest], None] | None:
    if console_url is None:
        return None

    def on_created(request: InterventionRequest) -> None:
        log.warning(
            "HUMAN INTERVENTION NEEDED",
            intervention=request.id,
            kind=request.kind.value,
            reason=request.reason,
            console=f"{console_url}/interventions/{request.id}",
        )

    return on_created


async def run_discovery(
    *,
    settings: Settings,
    planner: Planner,
    goal: str,
    entry_url: str,
    inputs: list[DiscoveryInput],
    capability_name: str,
    attended: bool,
    console_port: int | None = None,
    evidence_root: Path | None = None,
    artifacts_dir: Path | None = None,
    console_logging: bool = True,
) -> DiscoveryResult:
    policy = PolicyEngine.from_file(settings.policy_file)
    profile = ApplicationProfile.load(settings.profile_file)
    secrets = [i.value for i in inputs if i.sensitive]
    async with launch_surface(settings) as surface:
        port = console_port or settings.operator_port
        console_url = f"http://127.0.0.1:{port}" if attended else None
        ctx = build_run_context(
            kind="discovery",
            settings=settings,
            surface=surface,
            attended=attended,
            secrets=secrets,
            evidence_root=evidence_root,
            console_logging=console_logging,
            on_intervention=_announce(console_url),
        )
        console: ConsoleServer | None = None
        if attended:
            console = ConsoleServer(create_operator_app(ctx.escalation, surface), port=port)
            await console.start()
            await install_human_recorder(surface, ctx.escalation)
            log.info("operator console ready", url=console.url)
        try:
            loop = AgentLoop(
                surface=ctx.automation_surface,
                planner=planner,
                policy=policy,
                profile=profile,
                settings=settings,
                events=ctx.events,
                evidence=ctx.evidence,
                escalation=ctx.escalation,
                redactor=ctx.redactor,
                artifacts_dir=artifacts_dir or settings.artifacts_dir,
            )
            return await loop.discover(
                goal=goal, entry_url=entry_url, inputs=inputs, capability_name=capability_name
            )
        finally:
            if console is not None:
                await console.stop()


async def run_replay(
    *,
    settings: Settings,
    artifact: CapabilityArtifact,
    inputs: Mapping[str, object],
    attended: bool,
    base_url: str | None = None,
    evidence_kind: EvidenceKind = "replay",
    console_port: int | None = None,
    evidence_root: Path | None = None,
    console_logging: bool = True,
) -> ReplayResult:
    policy = PolicyEngine.from_file(settings.policy_file)
    secrets = [
        str(inputs[name])
        for name, spec in artifact.inputs.items()
        if spec.sensitive and name in inputs
    ]
    async with launch_surface(settings) as surface:
        console_url = None
        port = console_port or settings.operator_port
        if attended:
            console_url = f"http://127.0.0.1:{port}"
        ctx = build_run_context(
            kind=evidence_kind,
            settings=settings,
            surface=surface,
            attended=attended,
            secrets=secrets,
            evidence_root=evidence_root,
            console_logging=console_logging,
            on_intervention=_announce(console_url),
        )
        console: ConsoleServer | None = None
        if attended:
            console = ConsoleServer(create_operator_app(ctx.escalation, surface), port=port)
            await console.start()
            await install_human_recorder(surface, ctx.escalation)
            log.info("operator console ready", url=console.url)
        try:
            executor = ReplayExecutor(
                surface=ctx.automation_surface,
                policy=policy,
                settings=settings,
                events=ctx.events,
                evidence=ctx.evidence,
                escalation=ctx.escalation,
                redactor=ctx.redactor,
            )
            return await executor.replay(artifact, inputs, base_url=base_url)
        finally:
            if console is not None:
                await console.stop()
