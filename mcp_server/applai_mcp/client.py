"""Async client for the ApplAI FastAPI service."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx2

from applai_mcp import errors
from applai_mcp.config import Settings
from applai_mcp.models import JobDetail, JobSummary, ScoreResult

PHASES = ("scrape", "score", "generate", "deliver")


class ApplAIClient:
    def __init__(self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None):
        self._settings = settings
        self._base_url = str(settings.applai_api_url).rstrip("/")
        self._http = httpx2.AsyncClient(
            base_url=self._base_url,
            headers={"X-API-Key": settings.applai_api_key.get_secret_value()},
            timeout=settings.request_timeout_seconds,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any) -> Any:
        timeout = timeout or self._settings.request_timeout_seconds
        try:
            resp = await self._http.request(method, path, timeout=timeout, **kwargs)
        except httpx2.RequestError as exc:
            raise errors.from_transport(exc, self._base_url, timeout) from exc
        if resp.status_code >= 400:
            raise errors.from_response(resp)
        return resp.json()

    async def score_job(self, payload: dict[str, Any]) -> ScoreResult:
        # Scoring is one LLM call; allow it the generate budget rather than the read timeout
        data = await self._request("POST", "/jobs/score", json=payload,
                                   timeout=self._settings.generate_timeout_seconds)
        return ScoreResult.model_validate(data)

    async def generate_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/jobs/generate", json=payload,
                                   timeout=self._settings.generate_timeout_seconds)

    async def get_job(self, job_id: str) -> JobDetail:
        return JobDetail.model_validate(await self._request("GET", f"/jobs/{quote(job_id, safe='')}"))

    async def list_jobs(self, **filters: Any) -> list[JobSummary]:
        params = {k: v for k, v in filters.items() if v is not None}
        data = await self._request("GET", "/jobs", params=params)
        return [JobSummary.model_validate(j) for j in data["jobs"]]

    async def duplicates(self, company: str, title: str) -> list[JobSummary]:
        data = await self._request("GET", "/jobs/duplicates", params={"company": company, "title": title})
        return [JobSummary.model_validate(j) for j in data["matches"]]

    async def stats(self) -> dict[str, int]:
        return (await self._request("GET", "/stats"))["jobs"]

    async def feedback(self) -> dict[str, Any]:
        return await self._request("GET", "/feedback")

    async def run_phase(self, phase: str) -> dict[str, Any]:
        if phase not in PHASES:
            raise ValueError(f"Unknown phase {phase!r}")
        return await self._request("POST", f"/{phase}", timeout=self._settings.phase_timeout_seconds)
