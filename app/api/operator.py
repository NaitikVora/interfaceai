"""Operator console: a minimal but real handoff surface bound to the live run.

It runs *inside the run's process* on a second port, because the human must operate the very
same browser page automation is using. Every mutating request goes through the
``EscalationManager``, which enforces session ownership and records what the human did.

HTML pages exist for a person; the ``/api`` routes exist for the ``operator`` CLI and tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, ValidationError

from app.artifacts.schema import ActionType
from app.automation.surface import ComputerSurface
from app.escalation.manager import EscalationManager, OperatorAction, OperatorError

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class NotePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = None


def create_operator_app(manager: EscalationManager, surface: ComputerSurface) -> FastAPI:
    app = FastAPI(title="Operator Console", docs_url=None, redoc_url=None)

    def context(request: Request, **extra: Any) -> dict[str, Any]:
        return {
            "run_id": manager.session.session_id,
            "session_state": manager.session.state.value,
            "owner": manager.session.control_owner.value
            if manager.session.control_owner
            else "nobody (paused)",
            **extra,
        }

    def state_json() -> dict[str, Any]:
        return {
            "run_id": manager.session.session_id,
            "session_state": manager.session.state.value,
            "control_owner": manager.session.control_owner.value
            if manager.session.control_owner
            else None,
            "open_interventions": [i.id for i in manager.open_interventions()],
            "pause_requested": manager.pause_requested,
        }

    def detail(request: Request, intervention_id: str, flash: str | None = None) -> HTMLResponse:
        intervention = manager.get(intervention_id)
        return _TEMPLATES.TemplateResponse(
            request,
            "intervention.html",
            context(
                request,
                i=intervention,
                flash=flash,
                observation=manager.latest_observation(),
            ),
        )

    @app.exception_handler(OperatorError)
    async def operator_error(request: Request, exc: OperatorError) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": str(exc)}, status_code=409)
        parts = request.url.path.split("/")
        if len(parts) > 2 and parts[1] == "interventions" and parts[2] in manager.interventions:
            return detail(request, parts[2], flash=str(exc))
        return HTMLResponse(f"<p>{exc}</p><p><a href='/'>back</a></p>", status_code=409)

    # ------------------------------------------------------------------ HTML
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        interventions = sorted(
            manager.interventions.values(), key=lambda i: (not i.is_open, i.created_at)
        )
        return _TEMPLATES.TemplateResponse(
            request, "index.html", context(request, interventions=interventions)
        )

    @app.get("/interventions/{intervention_id}", response_class=HTMLResponse)
    async def intervention_page(request: Request, intervention_id: str) -> HTMLResponse:
        return detail(request, intervention_id)

    @app.get("/interventions/{intervention_id}/screenshot.png")
    async def screenshot(intervention_id: str) -> Response:
        manager.get(intervention_id)
        return Response(await surface.screenshot(), media_type="image/png")

    @app.post("/interventions/{intervention_id}/take-control")
    async def take_control_form(intervention_id: str) -> RedirectResponse:
        manager.take_control(intervention_id)
        return RedirectResponse(f"/interventions/{intervention_id}", status_code=303)

    @app.post("/interventions/{intervention_id}/approve")
    async def approve_form(intervention_id: str, note: str = Form("")) -> RedirectResponse:
        manager.approve(intervention_id, note or None)
        return RedirectResponse(f"/interventions/{intervention_id}", status_code=303)

    @app.post("/interventions/{intervention_id}/abort")
    async def abort_form(intervention_id: str, note: str = Form("")) -> RedirectResponse:
        manager.abort(intervention_id, note or None)
        return RedirectResponse(f"/interventions/{intervention_id}", status_code=303)

    @app.post("/interventions/{intervention_id}/release")
    async def release_form(intervention_id: str, note: str = Form("")) -> RedirectResponse:
        manager.release(intervention_id, note or None)
        return RedirectResponse(f"/interventions/{intervention_id}", status_code=303)

    @app.post("/interventions/{intervention_id}/observe")
    async def observe_form(intervention_id: str) -> RedirectResponse:
        await manager.observe_for_operator(intervention_id)
        return RedirectResponse(f"/interventions/{intervention_id}", status_code=303)

    @app.post("/interventions/{intervention_id}/actions")
    async def action_form(
        request: Request,
        intervention_id: str,
        *,
        action: str = Form(...),
        ref: str = Form(""),
        value: str = Form(""),
        key: str = Form(""),
        url: str = Form(""),
    ) -> Response:
        try:
            operator_action = OperatorAction(
                action=ActionType(action),
                ref=ref or None,
                value=value or None,
                key=key or None,
                url=url or None,
            )
        except (ValueError, ValidationError) as exc:
            return detail(request, intervention_id, flash=f"invalid action: {exc}")
        record = await manager.perform_human_action(intervention_id, operator_action)
        await manager.observe_for_operator(intervention_id)
        return detail(request, intervention_id, flash=f"performed {record.action}: ok")

    @app.post("/pause")
    async def pause_form() -> RedirectResponse:
        manager.request_pause()
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------------ JSON API
    @app.get("/api/state")
    async def api_state() -> JSONResponse:
        return JSONResponse(state_json())

    @app.get("/api/interventions")
    async def api_list() -> JSONResponse:
        return JSONResponse([i.model_dump(mode="json") for i in manager.interventions.values()])

    @app.get("/api/interventions/{intervention_id}")
    async def api_get(intervention_id: str) -> JSONResponse:
        return JSONResponse(manager.get(intervention_id).model_dump(mode="json"))

    @app.post("/api/interventions/{intervention_id}/take-control")
    async def api_take_control(intervention_id: str) -> JSONResponse:
        return JSONResponse(manager.take_control(intervention_id).model_dump(mode="json"))

    @app.post("/api/interventions/{intervention_id}/approve")
    async def api_approve(intervention_id: str, payload: NotePayload | None = None) -> JSONResponse:
        note = payload.note if payload else None
        return JSONResponse(manager.approve(intervention_id, note).model_dump(mode="json"))

    @app.post("/api/interventions/{intervention_id}/abort")
    async def api_abort(intervention_id: str, payload: NotePayload | None = None) -> JSONResponse:
        note = payload.note if payload else None
        return JSONResponse(manager.abort(intervention_id, note).model_dump(mode="json"))

    @app.post("/api/interventions/{intervention_id}/release")
    async def api_release(intervention_id: str, payload: NotePayload | None = None) -> JSONResponse:
        note = payload.note if payload else None
        return JSONResponse(manager.release(intervention_id, note).model_dump(mode="json"))

    @app.get("/api/interventions/{intervention_id}/observe")
    async def api_observe(intervention_id: str) -> JSONResponse:
        observation = await manager.observe_for_operator(intervention_id)
        return JSONResponse(
            {
                "url": observation.url,
                "headings": observation.headings,
                "controls": [
                    {"ref": c.ref, "description": c.describe(), "enabled": c.enabled}
                    for c in observation.controls
                ],
            }
        )

    @app.post("/api/interventions/{intervention_id}/actions")
    async def api_action(intervention_id: str, action: OperatorAction) -> JSONResponse:
        record = await manager.perform_human_action(intervention_id, action)
        return JSONResponse(record.model_dump(mode="json"))

    @app.post("/api/pause")
    async def api_pause() -> JSONResponse:
        manager.request_pause()
        return JSONResponse(state_json())

    return app


class ConsoleServer:
    """Runs the operator console in the current event loop for the duration of a run."""

    def __init__(self, app: FastAPI, *, host: str = "127.0.0.1", port: int) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
        )
        self._task: asyncio.Task[None] | None = None
        self.url = f"http://{host}:{port}"

    async def start(self) -> str:
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.02)
        return self.url

    async def stop(self) -> None:
        if self._task is None:
            return
        self._server.should_exit = True
        await self._task
        self._task = None
