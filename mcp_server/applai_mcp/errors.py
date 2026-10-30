"""Translate ApplAI HTTP and transport failures into ToolErrors the model can act on."""
from __future__ import annotations

from typing import Any

import httpx2
from mcp.server.mcpserver.exceptions import ToolError

_MAX_DETAIL = 500


class ApplAIError(ToolError):
    """An anticipated ApplAI API failure; the message is shown to the model."""

    def __init__(self, message: str, status_code: int | None = None, existing_job_id: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.existing_job_id = existing_job_id


def _detail_text(detail: Any) -> str:
    if isinstance(detail, list):  # FastAPI validation errors
        parts = []
        for err in detail:
            loc = ".".join(str(p) for p in err.get("loc", []) if p not in ("body", "query"))
            parts.append(f"{loc}: {err.get('msg', '')}" if loc else str(err.get("msg", "")))
        return "; ".join(parts)
    if isinstance(detail, dict):
        return str(detail.get("message", detail))
    return str(detail)


def from_response(resp: httpx2.Response) -> ApplAIError:
    try:
        detail = resp.json().get("detail", resp.text)
    except ValueError:
        detail = resp.text
    text = _detail_text(detail)[:_MAX_DETAIL]
    code = resp.status_code

    if code == 401:
        return ApplAIError("ApplAI rejected the API key: APPLAI_API_KEY in mcp_server/.env must match the server's", code)
    if code == 503:
        return ApplAIError(f"ApplAI is not ready: {text}", code)
    if code == 409:
        existing = detail.get("existing_job_id") if isinstance(detail, dict) else None
        suffix = f" (existing job_id: {existing})" if existing else ""
        return ApplAIError(f"Conflict: {text}{suffix}", code, existing_job_id=existing)
    if code in (404, 422):
        return ApplAIError(text, code)
    return ApplAIError(f"ApplAI error {code}: {text}", code)


def from_transport(exc: httpx2.RequestError, base_url: str, timeout: float) -> ApplAIError:
    if isinstance(exc, httpx2.TimeoutException):
        return ApplAIError(
            f"ApplAI did not answer within {timeout:.0f}s ({type(exc).__name__}); "
            "the operation may still be running server-side"
        )
    if isinstance(exc, httpx2.ConnectError):
        return ApplAIError(f"ApplAI API unreachable at {base_url}: is `docker compose up` running?")
    return ApplAIError(f"Request to ApplAI failed: {type(exc).__name__}")
