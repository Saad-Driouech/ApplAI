"""Settings loaded from the environment and mcp_server/.env (environment wins)."""
from __future__ import annotations

from pathlib import Path

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    applai_api_url: AnyHttpUrl = Field(AnyHttpUrl("http://127.0.0.1:8000"), description="ApplAI FastAPI base URL")
    applai_api_key: SecretStr = Field(description="Same value as the server's APPLAI_API_KEY")
    applai_applications_dir: Path = Field(description="Host path of the job_applications folder")

    request_timeout_seconds: float = Field(30.0, gt=0, description="Reads and single-job scoring")
    generate_timeout_seconds: float = Field(600.0, gt=0, description="POST /jobs/generate (LLM + pdflatex)")
    phase_timeout_seconds: float = Field(3600.0, gt=0, description="Batch pipeline phases")
    confirmation_ttl_seconds: int = Field(600, gt=0, description="Lifetime of confirmation tokens and previews")
