"""Redaction of secrets and regulated data from everything that leaves the process.

Three layers, applied in order:

1. **Known secret values** (API key, sensitive inputs) are replaced wherever they appear.
2. **Secret-like keys** in structured data (``password``, ``token``, ``cookie``, ...) are masked.
3. **PII patterns** (SSNs, card/account numbers, emails, phone numbers) are masked in free text.

The same ``Redactor`` scrubs event logs, evidence text and the observation sent to the LLM.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|access[_-]?code"
    r"|credential|private[_-]?key)"
)

DEFAULT_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("account", re.compile(r"\b\d{9,17}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("phone", re.compile(r"\b\d{3}[-.]\d{3}[-.]\d{4}\b")),
)

MIN_SECRET_LENGTH = 4
"""Shorter values would cause rampant false positives; policy forbids such weak secrets anyway."""


class Redactor:
    def __init__(
        self,
        secrets: Iterable[str] = (),
        *,
        pii_patterns: tuple[tuple[str, re.Pattern[str]], ...] = DEFAULT_PII_PATTERNS,
    ) -> None:
        self._secrets: set[str] = set()
        self._pii_patterns = pii_patterns
        for value in secrets:
            self.add_secret(value)

    def add_secret(self, value: str | None) -> None:
        if value and len(value) >= MIN_SECRET_LENGTH:
            self._secrets.add(value)

    @property
    def secret_count(self) -> int:
        return len(self._secrets)

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        for kind, pattern in self._pii_patterns:
            text = pattern.sub(f"[REDACTED:{kind}]", text)
        return text

    def redact(self, value: Any, key: str | None = None) -> Any:
        """Recursively redact structured data (dicts, lists, strings)."""
        if key is not None and SECRET_KEY_RE.search(key) and value not in (None, "", [], {}):
            return REDACTED
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            return {str(k): self.redact(v, str(k)) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.redact(v) for v in value]
        return value

    @staticmethod
    def hash_hint(value: str) -> str:
        """Deterministic, non-reversible correlation hint for debugging redacted values."""
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
