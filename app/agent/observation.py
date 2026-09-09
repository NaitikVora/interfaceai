"""Renders a bounded, redacted observation for the model.

The model sees: where it is, what it can act on (as refs), what data is visible, what inputs it
may use (secrets only as placeholders), what it has already done, and any system feedback.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.automation.surface import ControlDescriptor, Observation, TableDescriptor
from app.safety.redaction import Redactor


@dataclass
class AgentContext:
    step: int
    max_steps: int
    inputs: dict[str, str]
    sensitive: frozenset[str]
    extracted: dict[str, str] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def render_observation(
    observation: Observation, ctx: AgentContext, redactor: Redactor, *, max_chars: int
) -> str:
    sections: list[str] = [
        f"STEP {ctx.step}/{ctx.max_steps}",
        f"URL: {observation.url}",
        f"TITLE: {observation.title}",
        "HEADINGS: " + (" | ".join(observation.headings) or "(none)"),
    ]
    if observation.messages:
        sections.append("MESSAGES:\n" + "\n".join(f"  - {m}" for m in observation.messages))
    if observation.dialogs:
        sections.append(
            "DIALOGS (auto-dismissed):\n" + "\n".join(f"  - {d}" for d in observation.dialogs)
        )

    controls = "\n".join(_render_control(c) for c in observation.controls) or "  (none)"
    if observation.controls_truncated:
        controls += "\n  ... more controls not shown"
    sections.append("CONTROLS (act on these by ref):\n" + controls)

    if observation.tables:
        sections.append("TABLES:\n" + "\n".join(_render_table(t) for t in observation.tables))

    text = observation.text + (" ...[truncated]" if observation.text_truncated else "")
    sections.append(f"VISIBLE TEXT: {text}")

    inputs = []
    for name, value in ctx.inputs.items():
        if name in ctx.sensitive:
            inputs.append(f'  {name} = <secret> (type it as "${{{name}}}")')
        else:
            inputs.append(f'  {name} = "{value}" (type it as "${{{name}}}")')
    sections.append("INPUTS AVAILABLE:\n" + ("\n".join(inputs) or "  (none)"))

    if ctx.extracted:
        sections.append(
            "EXTRACTED SO FAR:\n" + "\n".join(f"  {k} = {v}" for k, v in ctx.extracted.items())
        )
    if ctx.history:
        sections.append("RECENT ACTIONS:\n" + "\n".join(f"  {h}" for h in ctx.history))
    if ctx.notes:
        sections.append("SYSTEM FEEDBACK:\n" + "\n".join(f"  ! {n}" for n in ctx.notes))

    rendered = redactor.redact_text("\n\n".join(sections))
    if len(rendered) > max_chars:
        rendered = rendered[: max_chars - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return rendered


TRUNCATION_MARKER = "\n...[observation truncated]"


def _render_control(control: ControlDescriptor) -> str:
    parts = [f"  {control.ref} {control.role or control.tag}"]
    if control.name:
        parts.append(f'"{control.name}"')
    elif control.text:
        parts.append(f'"{control.text}"')
    attrs = control.attributes
    for key in ("name", "href", "placeholder"):
        if key in attrs:
            parts.append(f"{key}={attrs[key]}")
    if attrs.get("type") not in (None, "text"):
        parts.append(f"type={attrs['type']}")
    if "current_value" in attrs:
        parts.append(f'value="{attrs["current_value"]}"')
    if control.options:
        shown = ", ".join(control.options[:8])
        more = f", +{len(control.options) - 8} more" if len(control.options) > 8 else ""
        parts.append(f"options=[{shown}{more}]")
    if control.checked is not None:
        parts.append("checked" if control.checked else "unchecked")
    if not control.enabled:
        parts.append("(disabled)")
    if control.container is not None:
        ident = control.container.attributes
        label = ident.get("id") or ident.get("action") or ident.get("class") or ident.get("role")
        parts.append(f"[in {control.container.tag}{'.' + label if label else ''}]")
    return " ".join(parts)


def _render_table(table: TableDescriptor) -> str:
    header = f"  {table.ref} ({table.total_rows} rows)"
    lines = [header]
    if table.headers:
        lines.append("    headers: " + " | ".join(table.headers))
    for index, row in enumerate(table.rows, start=1):
        lines.append(f"    r{index}: " + " | ".join(row))
    if table.truncated:
        lines.append("    ... more rows not shown")
    return "\n".join(lines)
