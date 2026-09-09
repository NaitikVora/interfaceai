"""FastAPI application for the synthetic LegacyCore Teller Workstation.

Deliberately legacy: server-rendered pages, table layouts, inconsistent label association,
no test IDs, and confirmation pages. A `/__admin` console injects runtime failure modes so the
replay engine's error handling can be exercised deterministically.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from demo_app.seed_data import (
    APP_PRODUCT,
    APP_VENDOR,
    APP_VERSION,
    MEMBERS,
    OPERATORS,
    SUB_ACCOUNT_TYPES,
    Account,
    Role,
    accounts_for,
    format_money,
)
from demo_app.state import AppState, Interstitial, Session

SESSION_COOKIE = "LCSESSION"
MEMBER_NO_PATTERN = re.compile(r"^\d{5}$")
NICKNAME_PATTERN = re.compile(r"^[A-Za-z0-9 ]+$")
NICKNAME_MAX_LEN = 20
INTERSTITIAL_EXEMPT_PREFIXES = (
    "/static",
    "/__admin",
    "/notice/ack",
    "/login",
    "/logout",
    "/favicon",
)
BOOLEAN_FLAGS = ("expire_session_on_next_request", "duplicate_search_form", "app_error_on_search")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(state: AppState | None = None) -> FastAPI:
    """Build the demo application around an (optionally shared) in-memory state."""
    store = state or AppState()
    app = FastAPI(title="LegacyCore Teller Workstation", docs_url=None, redoc_url=None)
    app.state.store = store
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    templates.env.filters["money"] = format_money

    def session_of(request: Request) -> Session | None:
        token = request.cookies.get(SESSION_COOKIE)
        return store.sessions.get(token) if token else None

    def render(
        request: Request,
        template: str,
        title: str,
        status_code: int = 200,
        **context: Any,
    ) -> HTMLResponse:
        session = session_of(request)
        base = {
            "title": title,
            "vendor": APP_VENDOR,
            "product": APP_PRODUCT,
            "version": APP_VERSION,
            "operator": session.operator if session else None,
            "session_short": session.token[:6] if session else "-",
        }
        return templates.TemplateResponse(
            request, template, {**base, **context}, status_code=status_code
        )

    def error_page(
        request: Request, status: int, heading: str, detail: str, reference: str | None = None
    ) -> HTMLResponse:
        return render(
            request,
            "error.html",
            heading,
            status_code=status,
            heading=heading,
            detail=detail,
            reference=reference,
        )

    def sign_in_redirect(reason: str | None = None) -> RedirectResponse:
        target = f"/login?reason={reason}" if reason else "/login"
        return RedirectResponse(target, status_code=303)

    # ----------------------------------------------------------------------------------
    # Fault-injection middleware: latency, session expiry, one-shot interstitials.
    # ----------------------------------------------------------------------------------
    @app.middleware("http")
    async def runtime_conditions(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        flags = store.flags
        if flags.response_delay_ms > 0:
            await asyncio.sleep(flags.response_delay_ms / 1000)

        path = request.url.path
        exempt = path.startswith(INTERSTITIAL_EXEMPT_PREFIXES)
        session = session_of(request)

        if session is not None and not exempt and flags.expire_session_on_next_request:
            flags.expire_session_on_next_request = False
            store.drop_session(session.token)
            response = sign_in_redirect("expired")
            response.delete_cookie(SESSION_COOKIE)
            return response

        if (
            session is not None
            and not exempt
            and request.method == "GET"
            and flags.interstitial != Interstitial.NONE
        ):
            kind = flags.interstitial
            flags.interstitial = Interstitial.NONE
            return render_interstitial(request, kind, path)

        return await call_next(request)

    def render_interstitial(request: Request, kind: Interstitial, next_path: str) -> HTMLResponse:
        if kind == Interstitial.SESSION_REFRESH:
            return render(
                request,
                "interstitial.html",
                "Session Expiring",
                heading="Session Expiring",
                body="Your session is about to expire due to inactivity. "
                "Click Continue Session to remain signed in.",
                button="Continue Session",
                next=next_path,
                hide_nav=True,
            )
        return render(
            request,
            "interstitial.html",
            "System Notice",
            heading="System Notice",
            body="Scheduled core maintenance will occur tonight between 11:00 PM and 1:00 AM. "
            "Real-time balances may be delayed during this window.",
            button="Acknowledge",
            next=next_path,
            hide_nav=True,
        )

    @app.post("/notice/ack")
    async def acknowledge_notice(next_path: str = Form("/home", alias="next")) -> RedirectResponse:
        safe = next_path.startswith("/") and not next_path.startswith("//")
        return RedirectResponse(next_path if safe else "/home", status_code=303)

    # ----------------------------------------------------------------------------------
    # Authentication
    # ----------------------------------------------------------------------------------
    @app.get("/")
    async def root(request: Request) -> RedirectResponse:
        return RedirectResponse("/home" if session_of(request) else "/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, reason: str | None = None) -> HTMLResponse:
        messages = {
            "expired": "Your session has expired. Please sign in again.",
            "signed_out": "You have been signed out.",
        }
        return render(request, "login.html", "Sign In", message=messages.get(reason or ""))

    @app.post("/login")
    async def login_submit(
        request: Request, operator_id: str = Form(""), access_code: str = Form("")
    ) -> Response:
        operator = OPERATORS.get(operator_id.strip())
        if operator is None or operator.access_code != access_code:
            return render(
                request,
                "login.html",
                "Sign In",
                status_code=401,
                error="Invalid operator ID or access code.",
                operator_id=operator_id,
            )
        session = store.create_session(operator)
        response = RedirectResponse("/home", status_code=303)
        response.set_cookie(SESSION_COOKIE, session.token, httponly=True, samesite="lax")
        return response

    @app.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        session = session_of(request)
        if session:
            store.drop_session(session.token)
        response = sign_in_redirect("signed_out")
        response.delete_cookie(SESSION_COOKIE)
        return response

    # ----------------------------------------------------------------------------------
    # Member lookup
    # ----------------------------------------------------------------------------------
    @app.get("/home", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        if session_of(request) is None:
            return sign_in_redirect()
        return render(
            request, "home.html", "Main Menu", business_date=datetime.now(UTC).strftime("%m/%d/%Y")
        )

    @app.get("/members/search", response_class=HTMLResponse)
    async def member_search_form(request: Request) -> Response:
        if session_of(request) is None:
            return sign_in_redirect()
        return render(
            request,
            "member_search.html",
            "Member Search",
            duplicate_form=store.flags.duplicate_search_form,
        )

    @app.post("/members/search")
    async def member_search_submit(request: Request, member_no: str = Form("")) -> Response:
        if session_of(request) is None:
            return sign_in_redirect()
        member_no = member_no.strip()
        flags = store.flags

        def search_page(error: str, status: int = 200) -> HTMLResponse:
            return render(
                request,
                "member_search.html",
                "Member Search",
                status_code=status,
                error=error,
                member_no=member_no,
                duplicate_form=flags.duplicate_search_form,
            )

        if flags.app_error_on_search:
            return error_page(
                request,
                500,
                "Application Error",
                "An unexpected error occurred while contacting the core system. "
                "Please contact the help desk.",
                reference="CORE-EXC-0x7F31",
            )
        if not MEMBER_NO_PATTERN.match(member_no):
            return search_page("Member number must be exactly 5 digits.")
        if flags.transient_search_failures > 0:
            flags.transient_search_failures -= 1
            return search_page(
                "The core system is temporarily unavailable (CORE-503). Please retry your request.",
                status=503,
            )
        if member_no not in MEMBERS:
            return search_page(f"No member found matching member number {member_no}.")
        return RedirectResponse(f"/members/{member_no}", status_code=303)

    @app.get("/members/{member_id}", response_class=HTMLResponse)
    async def member_details(request: Request, member_id: str) -> Response:
        if session_of(request) is None:
            return sign_in_redirect()
        member = MEMBERS.get(member_id)
        if member is None:
            return error_page(request, 404, "Member Not Found", f"No member {member_id} on file.")
        return render(
            request,
            "member_details.html",
            "Member Details",
            member=member,
            accounts=accounts_for(member_id, store.created_accounts),
        )

    # ----------------------------------------------------------------------------------
    # Sub-account opening: form -> review -> confirm (irreversible) -> confirmation
    # ----------------------------------------------------------------------------------
    def permission_denied(request: Request) -> HTMLResponse:
        return error_page(
            request,
            403,
            "Insufficient Permission",
            "You do not have permission to open sub-accounts. Contact a supervisor.",
        )

    @app.get("/members/{member_id}/subaccounts/new", response_class=HTMLResponse)
    async def subaccount_form(request: Request, member_id: str) -> Response:
        session = session_of(request)
        if session is None:
            return sign_in_redirect()
        member = MEMBERS.get(member_id)
        if member is None:
            return error_page(request, 404, "Member Not Found", f"No member {member_id} on file.")
        if session.operator.role != Role.SUPERVISOR:
            return permission_denied(request)
        return render(
            request,
            "subaccount_new.html",
            "Open New Sub-Account",
            member=member,
            account_types=SUB_ACCOUNT_TYPES,
            account_type=SUB_ACCOUNT_TYPES[0],
        )

    @app.post("/members/{member_id}/subaccounts/new")
    async def subaccount_submit(
        request: Request,
        member_id: str,
        account_type: str = Form(""),
        nickname: str = Form(""),
    ) -> Response:
        session = session_of(request)
        if session is None:
            return sign_in_redirect()
        member = MEMBERS.get(member_id)
        if member is None:
            return error_page(request, 404, "Member Not Found", f"No member {member_id} on file.")
        if session.operator.role != Role.SUPERVISOR:
            return permission_denied(request)
        nickname = nickname.strip()
        errors: list[str] = []
        if account_type not in SUB_ACCOUNT_TYPES:
            errors.append("Account type is required.")
        if not nickname:
            errors.append("Nickname is required.")
        elif len(nickname) > NICKNAME_MAX_LEN:
            errors.append(f"Nickname must be {NICKNAME_MAX_LEN} characters or fewer.")
        elif not NICKNAME_PATTERN.match(nickname):
            errors.append("Nickname may contain only letters, numbers and spaces.")
        if errors:
            return render(
                request,
                "subaccount_new.html",
                "Open New Sub-Account",
                status_code=422,
                member=member,
                account_types=SUB_ACCOUNT_TYPES,
                account_type=account_type,
                nickname=nickname,
                errors=errors,
            )
        session.pending_subaccount = {"account_type": account_type, "nickname": nickname}
        return RedirectResponse(f"/members/{member_id}/subaccounts/review", status_code=303)

    @app.get("/members/{member_id}/subaccounts/review", response_class=HTMLResponse)
    async def subaccount_review(request: Request, member_id: str) -> Response:
        session = session_of(request)
        if session is None:
            return sign_in_redirect()
        member = MEMBERS.get(member_id)
        if member is None or session.pending_subaccount is None:
            return RedirectResponse(f"/members/{member_id}/subaccounts/new", status_code=303)
        return render(
            request,
            "subaccount_review.html",
            "Review Sub-Account Request",
            member=member,
            pending=session.pending_subaccount,
        )

    @app.post("/members/{member_id}/subaccounts/confirm")
    async def subaccount_confirm(request: Request, member_id: str) -> Response:
        session = session_of(request)
        if session is None:
            return sign_in_redirect()
        member = MEMBERS.get(member_id)
        pending = session.pending_subaccount
        if member is None or pending is None:
            return RedirectResponse(f"/members/{member_id}/subaccounts/new", status_code=303)
        if session.operator.role != Role.SUPERVISOR:
            return permission_denied(request)
        account = Account(
            account_no=store.next_account_number(),
            member_id=member_id,
            account_type=pending["account_type"],
            nickname=pending["nickname"],
            balance=Decimal("0.00"),
            status="Active",
        )
        store.created_accounts.append(account)
        confirmation_no = store.next_confirmation_number()
        session.pending_subaccount = None
        session.last_confirmation = (confirmation_no, account)
        return RedirectResponse(
            f"/members/{member_id}/subaccounts/confirmation/{confirmation_no}", status_code=303
        )

    @app.get(
        "/members/{member_id}/subaccounts/confirmation/{confirmation_no}",
        response_class=HTMLResponse,
    )
    async def subaccount_confirmation(
        request: Request, member_id: str, confirmation_no: str
    ) -> Response:
        session = session_of(request)
        if session is None:
            return sign_in_redirect()
        member = MEMBERS.get(member_id)
        last = session.last_confirmation
        if member is None or last is None or last[0] != confirmation_no:
            return error_page(
                request, 404, "Confirmation Not Found", f"No confirmation {confirmation_no}."
            )
        return render(
            request,
            "subaccount_confirmation.html",
            "Sub-Account Opened",
            member=member,
            confirmation_no=confirmation_no,
            account=last[1],
        )

    # ----------------------------------------------------------------------------------
    # Fault injection console (test harness only)
    # ----------------------------------------------------------------------------------
    @app.get("/__admin", response_class=HTMLResponse)
    async def admin_page(request: Request) -> HTMLResponse:
        return render(
            request,
            "admin.html",
            "Fault Injection",
            flags=store.flags,
            interstitials=[i.value for i in Interstitial],
        )

    @app.get("/__admin/state")
    async def admin_state() -> JSONResponse:
        return JSONResponse(
            {
                "flags": {
                    "transient_search_failures": store.flags.transient_search_failures,
                    "interstitial": store.flags.interstitial.value,
                    "expire_session_on_next_request": store.flags.expire_session_on_next_request,
                    "response_delay_ms": store.flags.response_delay_ms,
                    "duplicate_search_form": store.flags.duplicate_search_form,
                    "app_error_on_search": store.flags.app_error_on_search,
                },
                "sessions": len(store.sessions),
                "created_accounts": len(store.created_accounts),
            }
        )

    @app.post("/__admin/inject")
    async def admin_inject(request: Request) -> Response:
        payload = await _read_payload(request)
        flags = store.flags
        if "transient_search_failures" in payload:
            flags.transient_search_failures = int(payload["transient_search_failures"] or 0)
        if "interstitial" in payload:
            flags.interstitial = Interstitial(str(payload["interstitial"] or "none"))
        if "response_delay_ms" in payload:
            flags.response_delay_ms = int(payload["response_delay_ms"] or 0)
        for name in BOOLEAN_FLAGS:
            if name in payload:
                setattr(flags, name, _truthy(payload[name]))
            elif payload.get("_form") == "1":
                setattr(flags, name, False)
        if payload.get("_form") == "1":
            return RedirectResponse("/__admin", status_code=303)
        return await admin_state()

    @app.post("/__admin/reset")
    async def admin_reset(request: Request) -> Response:
        store.reset_flags()
        payload = await _read_payload(request)
        if payload.get("_form") == "1":
            return RedirectResponse("/__admin", status_code=303)
        return await admin_state()

    @app.post("/__admin/reset-all")
    async def admin_reset_all() -> JSONResponse:
        store.reset_all()
        return await admin_state()

    return app


async def _read_payload(request: Request) -> dict[str, Any]:
    """Accept either a JSON body (tests/CLI) or an HTML form post (admin page)."""
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        body = await request.json()
        return dict(body) if isinstance(body, dict) else {}
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        return {**{k: str(v) for k, v in form.items()}, "_form": "1"}
    return {}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


app = create_app()
