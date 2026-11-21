"""Settings loaded from the environment and mcp_server/.env (environment wins)."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    applai_api_url: AnyHttpUrl = Field(AnyHttpUrl("http://127.0.0.1:8000"), description="ApplAI FastAPI base URL")
    applai_api_key: SecretStr = Field(description="Same value as the server's APPLAI_API_KEY")
    applai_applications_dir: Path = Field(description="Host path of the job_applications folder")

    request_timeout_seconds: float = Field(30.0, gt=0, description="Reads and single-job scoring")
    generate_timeout_seconds: float = Field(600.0, gt=0, description="POST /drafts (pdflatex)")
    phase_timeout_seconds: float = Field(3600.0, gt=0, description="Batch pipeline phases")
    confirmation_ttl_seconds: int = Field(600, gt=0, description="Lifetime of confirmation tokens and previews")
    confirmation_mode: Literal["auto", "token"] = Field(
        "auto", description="auto: elicit when the client supports it; token: always the two-step token flow")


class HttpSettings(BaseSettings):
    """Remote-connector settings, loaded only with --transport http."""
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    mcp_public_url: AnyHttpUrl = Field(description="Public origin of this server, e.g. https://mcp.example.com")
    authkit_issuer: AnyHttpUrl = Field(description="WorkOS AuthKit issuer, e.g. https://<subdomain>.authkit.app")
    allowed_user_id: str = Field(min_length=1, description="The only WorkOS user ('sub') allowed in")
    jwks_cache_seconds: int = Field(3600, gt=0, description="How long signing keys are cached")
    http_host: str = Field("127.0.0.1", description="Bind address")
    http_port: int = Field(8080, gt=0, lt=65536, description="Bind port")

    @property
    def issuer(self) -> str:
        return str(self.authkit_issuer).rstrip("/")

    @property
    def resource_url(self) -> str:
        """The MCP endpoint: the OAuth resource and the expected token audience."""
        return str(self.mcp_public_url).rstrip("/") + "/mcp"
