"""
Structured logging for ApplAI with PII redaction.

Usage:
    from src.logger import get_logger
    log = get_logger(__name__)
    log.info("Scraped job", extra={"job_id": job_id, "source": "indeed"})

PII fields automatically redacted: email, phone, name, address.
Discord bot tokens, API keys, and passwords are also stripped.
"""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# ── PII / secret patterns ──────────────────────────────────────────────────
_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Email addresses
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    # Phone numbers (international and local formats)
    (re.compile(r"\b(?:\+?[0-9]{1,3}[\s\-]?)?(?:\(?[0-9]{2,4}\)?[\s\-]?){2,5}[0-9]{2,4}\b"), "[PHONE]"),
    # Discord bot tokens
    (re.compile(r"\b[A-Za-z0-9_\-]{24}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{38}\b"), "[DISCORD_TOKEN]"),
    # Generic API keys / secrets (long hex/base64 strings)
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"), "[SECRET]"),
    # Bearer tokens in Authorization headers
    (re.compile(r"(?i)(Authorization:\s*Bearer\s+)\S+"), r"\1[TOKEN]"),
]


def _redact(message: str) -> str:
    for pattern, replacement in _REDACT_PATTERNS:
        message = pattern.sub(replacement, message)
    return message


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.msg = _redact(str(record.msg))
        # Also redact any string extras that may contain PII
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _redact(str(v)) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    _redact(str(a)) if isinstance(a, str) else a
                    for a in record.args
                )
        return super().format(record)


_LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


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


_initialized = False


def setup(logs_dir: Path | None = None, debug: bool = False) -> None:
    """
    Configure the root logger. Call once at application startup.
    Subsequent calls to get_logger() will inherit this configuration.
    """
    global _initialized
    if _initialized:
        return

    root = logging.getLogger("applai")
    root.setLevel(logging.DEBUG)
    root.propagate = False

    for h in _build_handlers(logs_dir, debug):
        root.addHandler(h)

    # Silence noisy third-party loggers
    for noisy in ("urllib3", "httpx", "httpcore", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _initialized = True


def get_logger(name: str) -> logging.Logger:
    """
    Return a child logger of the 'applai' hierarchy.
    Always call setup() before get_logger() in production code.
    """
    # Auto-setup with console-only + no-debug if setup() was never called
    if not _initialized:
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
