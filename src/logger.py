"""
Structured logging for ApplAI with PII redaction.

Usage:
    from src.logger import get_logger
    log = get_logger(__name__)
    log.info("Scraped job", extra={"job_id": job_id, "source": "indeed"})

Emails, phone numbers, API keys and tokens are redacted from every line
(tracebacks included). Timestamps are UTC.
"""
from __future__ import annotations

import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ── PII / secret patterns ──────────────────────────────────────────────────
# Lowercase-hex job IDs and UUIDs must stay readable so jobs can be traced.
_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    # Phone numbers: international (+49 …) or local with a leading 0 (0911 …)
    (re.compile(r"\+\d{1,3}[\s\-]?\(?\d{1,4}\)?(?:[\s\-]?\d{2,4}){2,4}"), "[PHONE]"),
    (re.compile(r"\b0\d{2,4}[\s/\-]\d{3,}(?:[\s\-]\d{2,})*\b"), "[PHONE]"),
    (re.compile(r"(?i)(Authorization:\s*Bearer\s+)\S+"), r"\1[TOKEN]"),
    (re.compile(r"(?i)\b(app_key|api_key|apikey|key|token|access_token)=[^&\s'\"]+"), r"\1=[SECRET]"),
    # Known provider key formats (Anthropic, Groq, Google, Notion)
    (re.compile(r"\b(?:sk-ant-|gsk_|AIza|secret_|ntn_)[A-Za-z0-9_\-]{16,}"), "[SECRET]"),
    (re.compile(r"\b[A-Za-z0-9_\-]{24}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,38}\b"), "[DISCORD_TOKEN]"),
    # Other long random tokens: mixed case and digits
    (re.compile(r"\b(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[a-z])(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{32,}\b"), "[SECRET]"),
]


def _redact(message: str) -> str:
    for pattern, replacement in _REDACT_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


class RedactingFormatter(logging.Formatter):
    """Redacts the fully formatted line (including tracebacks); timestamps in UTC."""

    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record))


_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _build_handlers(logs_dir: Path | None, debug: bool) -> list[logging.Handler]:
    handlers: list[logging.Handler] = []

    # Console handler — always present
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if debug else logging.INFO)
    console.setFormatter(RedactingFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    handlers.append(console)

    # Rotating file handler — only when logs_dir is provided
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / "applai.log"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,   # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(RedactingFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
        handlers.append(file_handler)

    return handlers


def setup(logs_dir: Path | None = None, debug: bool = False) -> None:
    """Configure the 'applai' logger. Each call replaces the previous handlers."""
    root = logging.getLogger("applai")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in _build_handlers(logs_dir, debug):
        root.addHandler(h)

    # Silence noisy third-party loggers
    for noisy in ("urllib3", "httpx", "httpcore", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger of 'applai'; console-only until setup() is called."""
    if not logging.getLogger("applai").handlers:
        setup()
    return logging.getLogger(f"applai.{name}")


def audit(action: str, **kwargs: Any) -> None:
    """
    Emit a structured audit log entry at INFO level.
    Audit events are always written (never filtered out).
    """
    log = get_logger("audit")
    payload = " | ".join(f"{k}={v}" for k, v in kwargs.items())
    log.info("[AUDIT] action=%s | %s", action, payload)
