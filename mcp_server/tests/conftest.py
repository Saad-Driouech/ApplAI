"""Shared fixtures: a fake ApplAI API (httpx2.MockTransport), a fake job_applications tree, an in-memory client."""
from __future__ import annotations

import inspect
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client

from applai_mcp.config import Settings
from applai_mcp.server import create_server

JD = "We are hiring an AI Engineer to build LLM agents in Python and ship them to customers. " * 3


def job(**overrides: Any) -> dict:
    base = {"id": "j1", "source": "arbeitnow", "title": "AI Engineer", "company": "Acme", "city": "Berlin",
            "country": "DE", "source_url": "https://jobs.example.com/1", "posted_at": None,
            "scraped_at": "2026-10-20T10:00:00+00:00", "score": 8.0, "score_reasoning": "good", "status": "queued",
            "skip_reason": None, "last_error": None, "application_id": None, "user_decision": None,
            "decision_at": None}
    return {**base, **overrides}


def score_response(**overrides: Any) -> dict:
    base = {"score": 7.5, "threshold": 6.0, "reasoning": "Strong LLM match", "must_haves_met": ["Python"],
            "gaps": ["Go"], "is_job_posting": True, "passes_keyword_filter": True,
            "job": {"title": "AI Engineer", "company": "Acme", "country": "DE", "city": "",
                    "source_url": "", "description_chars": len(JD)},
            "duplicates": [], "sanitizer_flags": []}
    return {**base, **overrides}


class FakeApi:
    """Routes '<METHOD> <path>' to a JSON body, a (status, body) tuple, or a callable(request)."""

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {
            "GET /stats": {"jobs": {"new": 4, "queued": 1, "ready": 2, "approved": 1}},
            "GET /feedback": {"total_decisions": 4, "overall_approval_rate": 0.5, "current_threshold": 6.0,
                              "recommended_threshold": 6.5, "recommendation": "Raise slightly"},
            "GET /jobs": {"count": 0, "jobs": []},
            "GET /jobs/duplicates": {"dedup_key": "acme|ai engineer", "matches": []},
            "POST /jobs/score": score_response(),
        }
        self.requests: list[httpx2.Request] = []

    def calls(self, route: str) -> list[httpx2.Request]:
        method, path = route.split(" ", 1)
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        route = self.routes.get(f"{request.method} {request.url.path}")
        if route is None:
            return httpx2.Response(404, json={"detail": "Not Found"})
        if callable(route):
            route = route(request)
        if inspect.isawaitable(route) or isinstance(route, httpx2.Response):
            return route
        status, body = route if isinstance(route, tuple) else (200, route)
        return httpx2.Response(status, json=body)


def body(request: httpx2.Request) -> dict:
    return json.loads(request.content)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fake_api() -> FakeApi:
    return FakeApi()


def write(root: Path, rel: str, text: str = "x", mtime: float | None = None) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def apps_dir(tmp_path: Path) -> Path:
    root = tmp_path / "job_applications"
    write(root, "germany/SAP/Forward_Deployed_AI_Engineer/JD.txt",
          "Company: SAP\nNOTE (2026-09-16): apply via referral\nAbout the role...")
    write(root, "germany/SAP/Forward_Deployed_AI_Engineer/Saad_Driouech_SAP_CV.pdf")
    write(root, "germany/Lio/Forward_Deployed_Engineer/JD.txt")
    write(root, "germany/Lio/Forward_Deployed_Engineer/Saad_Driouech_Lio_CV.tex")
    write(root, "germany/Lio/Forward_Deployed_Engineer/Lio_Call_Prep.md", "# Prep")
    write(root, "germany/Lio/Forward_Deployed_Engineer/Lio_Call_Prep.pdf")
    write(root, "germany/Acme/Saad_Driouech_Acme_CV.pdf")
    write(root, "germany/Acme/Saad_Driouech_Acme_Cover_Letter.pdf")
    write(root, "uae/Vatic/Research/JD.txt")
    write(root, "resumes/tex/Saad_Driouech_1page_CV.tex")
    return root


@pytest.fixture
def settings(apps_dir: Path) -> Settings:
    return Settings(applai_api_url="http://applai.test", applai_api_key="test-key",  # pragma: allowlist secret
                    applai_applications_dir=apps_dir, _env_file=None)


@pytest.fixture
def make_client(settings: Settings, fake_api: FakeApi) -> Callable[..., Client]:
    def make(**client_kwargs: Any) -> Client:
        server = create_server(settings, transport=httpx2.MockTransport(fake_api.handler))
        return Client(server, **client_kwargs)
    return make


async def call(client: Client, tool: str, args: dict | None = None) -> dict:
    """Call a tool and return its structured content, failing on tool errors."""
    result = await client.call_tool(tool, args or {})
    assert not result.is_error, result.content
    return result.structured_content


async def call_error(client: Client, tool: str, args: dict | None = None) -> str:
    result = await client.call_tool(tool, args or {})
    assert result.is_error, result.structured_content
    return result.content[0].text
