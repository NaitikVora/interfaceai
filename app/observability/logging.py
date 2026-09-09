"""Process logging setup (structlog). Log lines pass through the process redactor."""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from app.safety.redaction import Redactor

process_redactor = Redactor()
"""Secrets registered here are scrubbed from every log line emitted by this process."""


def _redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    redacted = process_redactor.redact(dict(event_dict))
    return dict(redacted) if isinstance(redacted, dict) else event_dict


def configure_logging(level: str = "INFO", *, quiet_libraries: bool = True) -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            _redact_processor,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    if quiet_libraries:
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "openai"):
            logging.getLogger(name).setLevel(logging.WARNING)
