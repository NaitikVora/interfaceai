"""In-memory state for the demo application: sessions, created records and injected faults."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from demo_app.seed_data import Account, Operator


class Interstitial(StrEnum):
    NONE = "none"
    SYSTEM_NOTICE = "system_notice"
    SESSION_REFRESH = "session_refresh"


@dataclass
class InjectionFlags:
    """Runtime failure modes a test or demo can switch on. Each is documented in /__admin."""

    transient_search_failures: int = 0
    """Number of upcoming member searches that return a 'core temporarily unavailable' banner."""

    interstitial: Interstitial = Interstitial.NONE
    """One-shot full-page notice shown on the next authenticated page load."""

    expire_session_on_next_request: bool = False
    """Drop the session on the next authenticated request (redirect to sign-in with 'expired')."""

    response_delay_ms: int = 0
    """Artificial latency added to every page response (simulates a slow core)."""

    duplicate_search_form: bool = False
    """Render a second, identical 'Quick Member Search' form (ambiguous controls)."""

    app_error_on_search: bool = False
    """Member search returns an HTTP 500 'Application Error' page."""


@dataclass
class Session:
    token: str
    operator: Operator
    created_at: datetime
    pending_subaccount: dict[str, str] | None = None
    last_confirmation: tuple[str, Account] | None = None


@dataclass
class AppState:
    sessions: dict[str, Session] = field(default_factory=dict)
    flags: InjectionFlags = field(default_factory=InjectionFlags)
    created_accounts: list[Account] = field(default_factory=list)
    confirmation_seq: int = 0

    def create_session(self, operator: Operator) -> Session:
        token = secrets.token_urlsafe(24)
        session = Session(token=token, operator=operator, created_at=datetime.now(UTC))
        self.sessions[token] = session
        return session

    def drop_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    def next_confirmation_number(self) -> str:
        self.confirmation_seq += 1
        return f"SA-{datetime.now(UTC).year}-{self.confirmation_seq:06d}"

    def next_account_number(self) -> str:
        return f"88099{len(self.created_accounts) + 1:05d}"

    def reset_flags(self) -> None:
        self.flags = InjectionFlags()

    def reset_all(self) -> None:
        self.sessions.clear()
        self.created_accounts.clear()
        self.confirmation_seq = 0
        self.reset_flags()
