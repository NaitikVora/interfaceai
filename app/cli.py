"""Command-line interface.

    python -m app.cli demo start
    python -m app.cli discover --goal "..." --url http://localhost:8000/login --input k=v ...
    python -m app.cli replay --artifact artifacts/member_savings_lookup.v1.json --input k=v ...
    python -m app.cli inspect-artifact --artifact artifacts/member_savings_lookup.v1.json
    python -m app.cli operator list --console http://127.0.0.1:8001

Secrets are never accepted as literals: pass ``--input access_code=env:DEMO_ACCESS_CODE``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
import uvicorn

from app.agent.planner import OpenAICompatiblePlanner, PlannerError
from app.agent.recorder import DiscoveryInput
from app.artifacts.schema import ActionType, TargetSpec, ValueType
from app.artifacts.serializer import ArtifactLoadError, load_artifact
from app.artifacts.validator import lint_artifact
from app.automation.actions import ActionRequest
from app.config import Settings, get_settings
from app.observability.logging import configure_logging
from app.orchestration import run_discovery, run_replay
from app.replay.errors import ReplayStatus
from app.safety.policy import PolicyEngine

app = typer.Typer(
    help="Computer-use automation: LLM discovery -> capability artifact -> deterministic replay.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
demo = typer.Typer(help="Run and fault-inject the local LegacyCore demo application.")
operator = typer.Typer(help="Operator client for a running run's console (human handoff).")
app.add_typer(demo, name="demo")
app.add_typer(operator, name="operator")

ENV_PREFIX = "env:"

InputOption = Annotated[
    list[str] | None,
    typer.Option("--input", "-i", help="name=value or name=env:VAR (repeatable)"),
]
EscalationOption = Annotated[
    str,
    typer.Option(
        "--escalation",
        help="console: start the operator console and wait for a human; none: unattended",
    ),
]
HeadedOption = Annotated[bool, typer.Option("--headed", help="Show the browser window")]


class InputError(typer.BadParameter):
    pass


def parse_inputs(raw: list[str] | None) -> tuple[dict[str, str], set[str]]:
    """Parse ``name=value`` pairs; ``env:VAR`` values are read from the environment."""
    values: dict[str, str] = {}
    env_sourced: set[str] = set()
    for item in raw or []:
        if "=" not in item:
            raise InputError(f"--input {item!r} must be name=value")
        name, value = item.split("=", 1)
        name = name.strip()
        if value.startswith(ENV_PREFIX):
            var = value[len(ENV_PREFIX) :]
            resolved = os.environ.get(var)
            if resolved is None:
                raise InputError(f"--input {name}: environment variable {var!r} is not set")
            values[name] = resolved
            env_sourced.add(name)
        else:
            values[name] = value
    return values, env_sourced


def _settings(headed: bool) -> Settings:
    settings = get_settings()
    if headed:
        settings = settings.model_copy(update={"headless": False})
    return settings


def _fail(message: str, code: int = 2) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


# ---------------------------------------------------------------------------------- demo
@demo.command("start")
def demo_start(
    port: Annotated[int | None, typer.Option(help="Port (default DEMO_APP_PORT)")] = None,
    host: str = "127.0.0.1",
) -> None:
    """Start the LegacyCore demo application (foreground)."""
    settings = get_settings()
    chosen = port or settings.demo_app_port
    typer.echo(f"LegacyCore Teller Workstation on http://{host}:{chosen}  (Ctrl+C to stop)")
    typer.echo("Operators: teller1/teller-pass (read-only), super1/super-pass (supervisor)")
    uvicorn.run("demo_app.main:app", host=host, port=chosen, log_level="warning")


@demo.command("inject")
def demo_inject(
    flag: Annotated[list[str], typer.Option("--flag", "-f", help="key=value (repeatable)")],
    url: Annotated[str | None, typer.Option(help="Demo app base URL")] = None,
) -> None:
    """Switch on a runtime failure mode, e.g. --flag transient_search_failures=1."""
    payload: dict[str, Any] = {}
    for item in flag:
        key, _, value = item.partition("=")
        payload[key] = value if value else "true"
    base = url or get_settings().demo_app_url
    response = httpx.post(f"{base}/__admin/inject", json=payload, timeout=10)
    response.raise_for_status()
    typer.echo(json.dumps(response.json()["flags"], indent=2))


@demo.command("reset")
def demo_reset(url: Annotated[str | None, typer.Option(help="Demo app base URL")] = None) -> None:
    """Clear all injected failure modes."""
    base = url or get_settings().demo_app_url
    response = httpx.post(f"{base}/__admin/reset", timeout=10)
    response.raise_for_status()
    typer.echo(json.dumps(response.json()["flags"], indent=2))


# ------------------------------------------------------------------------------ discover
@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="Natural-language goal")],
    url: Annotated[str, typer.Option(help="Entry URL of the target application")],
    name: Annotated[str, typer.Option(help="Capability name (snake_case)")],
    input: InputOption = None,
    sensitive: Annotated[
        list[str] | None, typer.Option(help="Input names that are secrets (must be env-sourced)")
    ] = None,
    escalation: EscalationOption = "console",
    max_steps: Annotated[int | None, typer.Option(help="Override AGENT_MAX_STEPS")] = None,
    headed: HeadedOption = False,
) -> None:
    """Run a genuine LLM-driven discovery and save the resulting capability artifact."""
    configure_logging()
    settings = _settings(headed)
    if max_steps is not None:
        settings = settings.model_copy(update={"agent_max_steps": max_steps})
    values, env_sourced = parse_inputs(input)
    secret_names = set(sensitive or [])
    for secret_name in secret_names:
        if secret_name not in values:
            _fail(f"--sensitive {secret_name}: no such --input")
        if secret_name not in env_sourced:
            _fail(f"--sensitive {secret_name} must be supplied as {secret_name}=env:VAR")
    inputs = [DiscoveryInput(n, v, n in secret_names) for n, v in values.items()]
    try:
        planner = OpenAICompatiblePlanner(settings)
    except PlannerError as exc:
        _fail(f"{exc}. Set LLM_API_KEY (see .env.example).")
        return
    _check_reachable(url)
    result = asyncio.run(
        run_discovery(
            settings=settings,
            planner=planner,
            goal=goal,
            entry_url=url,
            inputs=inputs,
            capability_name=name,
            attended=escalation == "console",
        )
    )
    typer.echo(result.render())
    raise typer.Exit(0 if result.status.value == "COMPLETED" else 1)


# -------------------------------------------------------------------------------- replay
@app.command()
def replay(
    artifact: Annotated[Path, typer.Option(help="Artifact JSON path")],
    input: InputOption = None,
    base_url: Annotated[str | None, typer.Option(help="Override the recorded base URL")] = None,
    escalation: EscalationOption = "console",
    evidence_kind: Annotated[
        str, typer.Option(help="Evidence folder: replay or failure")
    ] = "replay",
    headed: HeadedOption = False,
) -> None:
    """Replay a saved artifact deterministically (no LLM) and print the structured result."""
    configure_logging()
    settings = _settings(headed)
    loaded = _load(artifact)
    values, env_sourced = parse_inputs(input)
    for name, spec in loaded.inputs.items():
        if spec.sensitive and name in values and name not in env_sourced:
            _fail(f"input {name!r} is sensitive; pass it as {name}=env:VAR, not as a literal")
    if evidence_kind not in {"replay", "failure"}:
        _fail("--evidence-kind must be replay or failure")
    _check_reachable(base_url or loaded.target.base_url)
    result = asyncio.run(
        run_replay(
            settings=settings,
            artifact=loaded,
            inputs=values,
            attended=escalation == "console",
            base_url=base_url,
            evidence_kind="failure" if evidence_kind == "failure" else "replay",
        )
    )
    typer.echo(result.render())
    typer.echo(json.dumps(result.model_dump(mode="json")["outputs"], indent=2))
    raise typer.Exit(
        0 if result.status in {ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME} else 1
    )


# ----------------------------------------------------------------------------- artifacts
@app.command("inspect-artifact")
def inspect_artifact(artifact: Annotated[Path, typer.Option(help="Artifact JSON path")]) -> None:
    """Print a human-readable summary of a capability artifact."""
    a = _load(artifact)
    typer.echo(f"{a.name} v{a.version}  (schema {a.schema_version}, id {a.artifact_id})")
    typer.echo(f"  {a.description}")
    t = a.target
    typer.echo(f"  target: {t.vendor} {t.application} {t.version} @ {t.base_url}")
    typer.echo(
        f"  compatibility: {a.compatibility.product} {a.compatibility.supported_versions}"
        f"  risk: {a.policy.max_risk_class.value}"
        f"  human confirmation: {a.policy.requires_human_confirmation}"
    )
    typer.echo("  inputs:")
    for name, spec in a.inputs.items():
        flags = " (sensitive)" if spec.sensitive else ""
        pattern = f" pattern={spec.pattern}" if spec.pattern else ""
        typer.echo(f"    {name}: {spec.type.value}{flags}{pattern}")
    typer.echo("  outputs:")
    for name, out in a.outputs.items():
        typer.echo(f"    {name}: {out.type.value} <- {out.source.kind}")
    typer.echo("  steps:")
    for index, step in enumerate(a.steps, start=1):
        strategies = (
            ",".join(s.value for s in step.target.available_strategies()) if step.target else "-"
        )
        typer.echo(
            f"    {index:02d} {step.action.value:8s} {step.description[:60]:60s} "
            f"[{step.risk_class.value}] strategies={strategies} "
            f"pre={len(step.preconditions)} post={len(step.postconditions)}"
        )
    typer.echo(f"  checkpoint: {a.checkpoint.description}")
    typer.echo(
        "  runtime rules: " + ", ".join(f"{r.code}({r.category.value[:4]})" for r in a.conditions)
    )
    findings = lint_artifact(a)
    typer.echo("  lint: " + ("clean" if not findings else ""))
    for finding in findings:
        typer.echo(f"    {finding.render()}")


@app.command("validate-artifact")
def validate_artifact(artifact: Annotated[Path, typer.Option(help="Artifact JSON path")]) -> None:
    """Validate an artifact against the schema and robustness lints; exit 1 on schema errors."""
    a = _load(artifact)
    findings = lint_artifact(a)
    typer.echo(f"schema: valid ({a.name} v{a.version}, {len(a.steps)} steps)")
    for finding in findings:
        typer.echo(finding.render())
    if not findings:
        typer.echo("lint: clean")


@app.command("test-policy")
def test_policy(
    action: Annotated[str, typer.Option(help="navigate|click|type|select|press|extract")],
    url: Annotated[str, typer.Option(help="Current page URL (or navigation target)")],
    target_name: Annotated[str | None, typer.Option(help="Target accessible name")] = None,
    target_type: Annotated[
        str | None, typer.Option(help="Target input type, e.g. password")
    ] = None,
    secret_value: Annotated[bool, typer.Option(help="The typed value is a secret")] = False,
    policy: Annotated[Path | None, typer.Option(help="Policy JSON (default POLICY_FILE)")] = None,
) -> None:
    """Evaluate a hypothetical action against the policy engine."""
    engine = PolicyEngine.from_file(policy or get_settings().policy_file)
    try:
        action_type = ActionType(action)
    except ValueError:
        _fail(f"unknown action {action!r}")
        return
    target = None
    if action_type in {ActionType.CLICK, ActionType.TYPE, ActionType.SELECT, ActionType.EXTRACT}:
        attrs = {"tag": "input", **({"type": target_type} if target_type else {})}
        target = TargetSpec(
            role="button" if action_type is ActionType.CLICK else "textbox",
            name=target_name or "control",
            attributes=attrs,
        )
    request = ActionRequest(
        action=action_type,
        target=target,
        value="x" if action_type in {ActionType.TYPE, ActionType.SELECT} else None,
        key="Enter" if action_type is ActionType.PRESS else None,
        url=url if action_type is ActionType.NAVIGATE else None,
        output="value" if action_type is ActionType.EXTRACT else None,
        value_type=ValueType.STRING if action_type is ActionType.EXTRACT else None,
    )
    decision = engine.evaluate(request, current_url=url, value_is_secret=secret_value)
    typer.echo(json.dumps(decision.as_event_data(), indent=2))
    raise typer.Exit(0 if decision.allowed else 1)


# ------------------------------------------------------------------------------ operator
ConsoleOption = Annotated[str, typer.Option(help="Operator console URL")]


def _console(base: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    try:
        response = httpx.request(method, f"{base}{path}", json=payload, timeout=30)
    except httpx.HTTPError as exc:
        _fail(f"cannot reach operator console at {base}: {exc}")
        return None
    if response.status_code >= 400:
        _fail(f"{response.status_code}: {response.text}", code=1)
    return response.json()


@operator.command("list")
def operator_list(console: ConsoleOption = "http://127.0.0.1:8001") -> None:
    """List interventions and the session state."""
    state = _console(console, "GET", "/api/state")
    typer.echo(
        f"run {state['run_id']}  session={state['session_state']}  owner={state['control_owner']}"
    )
    for item in _console(console, "GET", "/api/interventions"):
        typer.echo(
            f"  {item['id']}  {item['status']:14s} {item['kind']:18s} step={item['current_step']}  "
            f"{item['reason']}"
        )


@operator.command("show")
def operator_show(intervention: str, console: ConsoleOption = "http://127.0.0.1:8001") -> None:
    """Show one intervention in full (JSON)."""
    typer.echo(json.dumps(_console(console, "GET", f"/api/interventions/{intervention}"), indent=2))


@operator.command("take-control")
def operator_take_control(
    intervention: str, console: ConsoleOption = "http://127.0.0.1:8001"
) -> None:
    """Take control of the live session (automation stays paused)."""
    item = _console(console, "POST", f"/api/interventions/{intervention}/take-control")
    typer.echo(f"{item['id']}: {item['status']}")
    obs = _console(console, "GET", f"/api/interventions/{intervention}/observe")
    typer.echo(f"page: {obs['url']}  headings: {obs['headings']}")
    for control in obs["controls"]:
        typer.echo(f"  {control['ref']:4s} {control['description']}")


@operator.command("act")
def operator_act(
    intervention: str,
    action: Annotated[str, typer.Option(help="click|type|select|press|navigate")],
    ref: Annotated[str | None, typer.Option(help="Control ref from take-control/observe")] = None,
    value: Annotated[str | None, typer.Option()] = None,
    key: Annotated[str | None, typer.Option()] = None,
    url: Annotated[str | None, typer.Option()] = None,
    console: ConsoleOption = "http://127.0.0.1:8001",
) -> None:
    """Perform one action on the live session as the human operator."""
    payload = {"action": action, "ref": ref, "value": value, "key": key, "url": url}
    record = _console(console, "POST", f"/api/interventions/{intervention}/actions", payload)
    typer.echo(f"{record['action']} {record['target'] or ''}: ok -> {record['url_after']}")
    obs = _console(console, "GET", f"/api/interventions/{intervention}/observe")
    typer.echo(f"page: {obs['url']}  headings: {obs['headings']}")
    for control in obs["controls"]:
        typer.echo(f"  {control['ref']:4s} {control['description']}")


@operator.command("release")
def operator_release(
    intervention: str,
    note: Annotated[str | None, typer.Option()] = None,
    console: ConsoleOption = "http://127.0.0.1:8001",
) -> None:
    """Hand control back; automation resumes on the same session."""
    item = _console(console, "POST", f"/api/interventions/{intervention}/release", {"note": note})
    typer.echo(f"{item['id']}: {item['status']} ({len(item['human_action_log'])} human actions)")


@operator.command("approve")
def operator_approve(
    intervention: str,
    note: Annotated[str | None, typer.Option()] = None,
    console: ConsoleOption = "http://127.0.0.1:8001",
) -> None:
    """Approve a pending irreversible action; automation performs it."""
    item = _console(console, "POST", f"/api/interventions/{intervention}/approve", {"note": note})
    typer.echo(f"{item['id']}: {item['status']}")


@operator.command("abort")
def operator_abort(
    intervention: str,
    note: Annotated[str | None, typer.Option()] = None,
    console: ConsoleOption = "http://127.0.0.1:8001",
) -> None:
    """Abort the run."""
    item = _console(console, "POST", f"/api/interventions/{intervention}/abort", {"note": note})
    typer.echo(f"{item['id']}: {item['status']}")


@operator.command("pause")
def operator_pause(console: ConsoleOption = "http://127.0.0.1:8001") -> None:
    """Ask automation to hand over at the next step boundary."""
    typer.echo(json.dumps(_console(console, "POST", "/api/pause"), indent=2))


# ------------------------------------------------------------------------------- helpers
def _load(path: Path) -> Any:
    try:
        return load_artifact(path)
    except ArtifactLoadError as exc:
        _fail(str(exc), code=1)
        raise


def _check_reachable(url: str) -> None:
    try:
        httpx.get(url, timeout=5, follow_redirects=True)
    except httpx.HTTPError as exc:
        _fail(
            f"target application is not reachable at {url} ({exc.__class__.__name__}). "
            "Start it with: make demo"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
