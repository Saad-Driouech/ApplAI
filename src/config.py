"""
Central configuration loader for ApplAI.
Reads environment variables, validates required ones, and exposes typed settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{name}' is not set. "
            "Copy applai/config/.env.example → applai/config/.env and fill in the values."
        )
    return val


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class N8nConfig:
    user: str
    password: str


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str
    model: str = "gemini-2.5-flash"
    requests_per_day: int = 500        # free tier ceiling
    requests_per_minute: int = 15


@dataclass(frozen=True)
class DiscordConfig:
    bot_token: str
    channel_id: str
    public_key: str = ""   # from Discord Developer Portal → General Information


@dataclass(frozen=True)
class NotionConfig:
    api_token: str
    job_tracker_db_id: str
    feedback_log_db_id: str = ""   # optional — only needed for Phase 6 feedback loop


@dataclass(frozen=True)
class AnthropicConfig:
    api_key: str
    model: str = "claude-sonnet-4-6"


@dataclass(frozen=True)
class GroqConfig:
    api_key: str = ""
    model: str = "llama-3.3-70b-versatile"
    requests_per_minute: int = 30
    requests_per_day: int = 14400


@dataclass(frozen=True)
class OllamaConfig:
    endpoint: str = "http://localhost:11434/v1"
    model: str = "qwen3:8b"
    enabled: bool = False


@dataclass(frozen=True)
class AdzunaConfig:
    app_id: str = ""
    app_key: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_key)


@dataclass(frozen=True)
class PathConfig:
    working_dir: Path          # host-side job applications folder
    db_path: Path              # SQLite database
    logs_dir: Path


@dataclass(frozen=True)
class AppConfig:
    n8n: N8nConfig
    gemini: GeminiConfig
    groq: GroqConfig
    discord: DiscordConfig
    notion: NotionConfig
    anthropic: AnthropicConfig
    ollama: OllamaConfig
    adzuna: AdzunaConfig
    paths: PathConfig
    tier1_provider: str        # "gemini" | "groq" | "ollama"
    allowed_countries: frozenset = field(
        default_factory=lambda: frozenset({"DE", "SA", "AE", "CH", "QA", "NL"})
    )
    score_threshold: float = 6.0      # jobs below this score are skipped
    debug: bool = False


def load() -> AppConfig:
    """
    Load and validate all configuration from environment variables.
    Call once at startup; cache the result if needed.

    Raises EnvironmentError for any missing required variable.
    """
    n8n = N8nConfig(
        user=_require("N8N_USER"),
        password=_require("N8N_PASSWORD"),
    )

    gemini = GeminiConfig(
        api_key=_require("GOOGLE_AI_API_KEY"),
        model=_optional("GEMINI_MODEL", "gemini-2.5-flash"),
        requests_per_day=int(_optional("GEMINI_RPD", "500")),
        requests_per_minute=int(_optional("GEMINI_RPM", "15")),
    )

    discord = DiscordConfig(
        bot_token=_require("DISCORD_BOT_TOKEN"),
        channel_id=_require("DISCORD_CHANNEL_ID"),
        public_key=_optional("DISCORD_PUBLIC_KEY", ""),
    )

    notion = NotionConfig(
        api_token=_require("NOTION_API_TOKEN"),
        job_tracker_db_id=_require("NOTION_JOB_TRACKER_DB_ID"),
        feedback_log_db_id=_optional("NOTION_FEEDBACK_LOG_DB_ID", ""),
    )

    anthropic = AnthropicConfig(
        api_key=_require("ANTHROPIC_API_KEY"),
        model=_optional("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
    )

    ollama_enabled = _optional("OLLAMA_ENABLED", "false").lower() == "true"
    ollama = OllamaConfig(
        endpoint=_optional("OLLAMA_ENDPOINT", "http://localhost:11434/v1"),
        model=_optional("OLLAMA_MODEL", "qwen3:8b"),
        enabled=ollama_enabled,
    )

    working_dir = Path(_optional("APPLAI_WORKING_DIR", "/mnt/job_applications"))
    db_path = Path(_optional("APPLAI_DB_PATH", "/data/db/applai.db"))
    logs_dir = Path(_optional("APPLAI_LOGS_DIR", str(working_dir / "logs")))

    paths = PathConfig(
        working_dir=working_dir,
        db_path=db_path,
        logs_dir=logs_dir,
    )

    groq = GroqConfig(
        api_key=_optional("GROQ_API_KEY", ""),
        model=_optional("GROQ_MODEL", "llama-3.3-70b-versatile"),
        requests_per_minute=int(_optional("GROQ_RPM", "30")),
        requests_per_day=int(_optional("GROQ_RPD", "14400")),
    )

    adzuna = AdzunaConfig(
        app_id=_optional("ADZUNA_APP_ID", ""),
        app_key=_optional("ADZUNA_APP_KEY", ""),
    )

    tier1_provider = _optional("LLM_TIER1_PROVIDER", "gemini").lower()
    if tier1_provider not in {"gemini", "groq", "ollama"}:
        raise EnvironmentError(
            f"LLM_TIER1_PROVIDER must be 'gemini', 'groq', or 'ollama', got '{tier1_provider}'"
        )
    if tier1_provider == "groq" and not groq.api_key:
        raise EnvironmentError(
            "LLM_TIER1_PROVIDER=groq requires GROQ_API_KEY to be set."
        )

    return AppConfig(
        n8n=n8n,
        gemini=gemini,
        groq=groq,
        discord=discord,
        notion=notion,
        anthropic=anthropic,
        ollama=ollama,
        adzuna=adzuna,
        paths=paths,
        tier1_provider=tier1_provider,
        score_threshold=float(_optional("SCORE_THRESHOLD", "6.0")),
        debug=_optional("APPLAI_DEBUG", "false").lower() == "true",
    )


# Module-level singleton — import `cfg` everywhere.
# Populated lazily so unit tests can patch env vars before first access.
_cfg: Optional[AppConfig] = None


def get() -> AppConfig:
    global _cfg
    if _cfg is None:
        _cfg = load()
    return _cfg


def reset() -> None:
    """Reset cached config (useful in tests)."""
    global _cfg
    _cfg = None
