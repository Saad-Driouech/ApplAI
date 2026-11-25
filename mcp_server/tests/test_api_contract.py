"""
Contract between the MCP server and ApplAI's API models (src/api_models.py, loaded by path):
the mirror models in applai_mcp/models.py, the fake API data the tool tests use, and the request
bodies the tools send must all match what ApplAI defines.
"""
import importlib.util
import sys
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from applai_mcp import models
from conftest import (
    COMMITTED, SCORING_CONTEXT, FakeApi, JD, body, call, candidate, draft, job, resolve_response, watched,
)

API_MODELS = Path(__file__).resolve().parents[2] / "src" / "api_models.py"


def _load_api_models():
    spec = importlib.util.spec_from_file_location("applai_api_models", API_MODELS)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # pydantic resolves the module's postponed annotations through sys.modules
    spec.loader.exec_module(module)
    return module


api = _load_api_models()

# MCP mirror -> the ApplAI response model it parses
MIRRORS: dict[type[BaseModel], type[BaseModel]] = {
    models.JobSummary: api.JobSummary,
    models.JobDetail: api.JobDetail,
    models.ResolvedJob: api.ResolvedJob,
    models.ResolvedPosting: api.ResolveResponse,
    models.ScoringContext: api.ScoringContext,
    models.PrefilterResult: api.PrefilterResponse,
    models.JobToScore: api.JobToScore,
    models.ScoreOutcome: api.ScoreOutcome,
    models.GenerationContext: api.GenerationContext,
    models.Draft: api.DraftResponse,
    models.GenerateResult: api.GenerateResponse,
    models.IngestOutcome: api.IngestOutcome,
    models.BoardCandidate: api.BoardCandidate,
    models.WatchedCompany: api.WatchedCompany,
    models.WatchResult: api.WatchResponse,
}
# Fields the MCP server adds for Claude; they are never read from the API
MCP_ONLY = {(models.GenerateResult, "next_step")}


def _split_optional(annotation: Any) -> tuple[Any, bool]:
    if get_origin(annotation) in (Union, UnionType):
        args = get_args(annotation)
        inner = [a for a in args if a is not type(None)]
        return (inner[0] if len(inner) == 1 else Union[tuple(inner)]), type(None) in args
    return annotation, False


def _compatible(mirror: Any, api_type: Any) -> bool:
    if isinstance(mirror, type) and issubclass(mirror, BaseModel):
        return MIRRORS.get(mirror) is api_type
    if get_origin(mirror) is list:
        return get_origin(api_type) is list and _compatible(get_args(mirror)[0], get_args(api_type)[0])
    return mirror is api_type or (mirror is float and api_type is int)


@pytest.mark.parametrize("mirror", MIRRORS, ids=lambda m: m.__name__)
def test_mirror_matches_api_model(mirror):
    api_model = MIRRORS[mirror]
    problems = []
    for name, field in mirror.model_fields.items():
        if (mirror, name) in MCP_ONLY:
            continue
        api_field = api_model.model_fields.get(name)
        if api_field is None:
            problems.append(f"{name}: not in {api_model.__name__}")
            continue
        mirror_type, mirror_nullable = _split_optional(field.annotation)
        api_type, api_nullable = _split_optional(api_field.annotation)
        if api_nullable and not mirror_nullable:
            problems.append(f"{name}: ApplAI may send null, the mirror rejects it")
        if not _compatible(mirror_type, api_type):
            problems.append(f"{name}: {mirror_type} vs ApplAI {api_type}")
    assert problems == []


@pytest.mark.parametrize("model,key", [
    (api.JobsToScoreResponse, "jobs"), (api.ScoresResponse, "results"),
    (api.DuplicatesResponse, "matches"), (api.JobListResponse, "jobs"),
    (api.DetectResponse, "candidates"), (api.WatchlistResponse, "companies"), (api.IngestResponse, "results"),
], ids=lambda v: v if isinstance(v, str) else v.__name__)
def test_envelope_keys_the_client_unwraps(model, key):
    assert key in model.model_fields


def _assert_known_fields(model: type[BaseModel], data: dict) -> None:
    """Validate data against model and reject keys it doesn't define, which pydantic would silently drop."""
    model.model_validate(data)
    assert set(data) <= set(model.model_fields), f"{model.__name__}: unknown {set(data) - set(model.model_fields)}"
    for name, value in data.items():
        inner, _ = _split_optional(model.model_fields[name].annotation)
        if isinstance(value, dict) and isinstance(inner, type) and issubclass(inner, BaseModel):
            _assert_known_fields(inner, value)
        elif isinstance(value, list) and get_origin(inner) is list:
            item = get_args(inner)[0]
            if isinstance(item, type) and issubclass(item, BaseModel):
                for entry in value:
                    _assert_known_fields(item, entry)


@pytest.mark.parametrize("model,data", [
    (api.JobSummary, job()),
    (api.ResolveResponse, resolve_response()),
    (api.ScoringContext, SCORING_CONTEXT),
    (api.DraftResponse, draft()),
    (api.GenerateResponse, COMMITTED),
    (api.WatchedCompany, watched()),
    (api.BoardCandidate, candidate()),
    *[(model, FakeApi().routes[route]) for route, model in [
        ("GET /jobs", api.JobListResponse), ("GET /jobs/duplicates", api.DuplicatesResponse)]],
], ids=lambda v: v.__name__ if isinstance(v, type) else "")
def test_fake_api_data_has_the_real_shape(model, data):
    _assert_known_fields(model, data)


CV_TEX = "\\documentclass{article}\\begin{document}" + "Tailored CV. " * 20 + "\\end{document}"
BODY = "First paragraph about the role. " * 10 + "\n\n" + "Second paragraph. " * 5


@pytest.mark.anyio
class TestRequestBodies:
    async def test_resolve(self, make_client, fake_api):
        async with make_client() as client:
            await call(client, "get_scoring_context",
                       {"jd_text": JD, "title": "AI Engineer", "company": "Acme", "country": "de", "city": "Berlin"})
        _assert_known_fields(api.JobInput, body(fake_api.calls("POST /jobs/resolve")[0]))

    async def test_record_scores(self, make_client, fake_api):
        fake_api.routes["POST /jobs/scores"] = {"results": [{"job_id": "j1", "status": "queued"}]}
        async with make_client() as client:
            await call(client, "record_scores", {"scores": [
                {"job_id": "j1", "score": 7.5, "reasoning": "fit", "is_job_posting": True, "scored_by": "claude"}]})
        _assert_known_fields(api.ScoresRequest, body(fake_api.calls("POST /jobs/scores")[0]))

    async def test_add_jobs(self, make_client, fake_api):
        fake_api.routes["POST /jobs/ingest"] = {"inserted": 0, "results": []}
        async with make_client() as client:
            await call(client, "add_jobs", {"jobs": [
                {"jd_text": JD, "title": "AI Engineer", "company": "Acme", "country": "de", "city": "Berlin"},
                {"url": "https://jobs.example.com/1", "keep": True}]})
        _assert_known_fields(api.IngestRequest, body(fake_api.calls("POST /jobs/ingest")[0]))

    async def test_watchlist(self, make_client, fake_api):
        fake_api.routes["POST /watchlist/detect"] = {"candidates": [candidate()]}
        fake_api.routes["POST /watchlist"] = {"company": watched(), "added": True, "job_count": 1, "sample_titles": []}
        async with make_client() as client:
            await call(client, "watch_company", {"name": "Helsing", "careers_url": "https://helsing.ai/careers"})
        _assert_known_fields(api.DetectRequest, body(fake_api.calls("POST /watchlist/detect")[0]))
        _assert_known_fields(api.WatchRequest, body(fake_api.calls("POST /watchlist")[0]))

    @pytest.mark.parametrize("source", [
        {"job_id": "j1"},
        {"jd_text": JD, "title": "AI Engineer", "company": "Acme", "country": "de",
         "score": {"score": 7.5, "reasoning": "fit", "scored_by": "claude-opus"}},
    ], ids=["existing-job", "new-posting"])
    async def test_draft(self, make_client, fake_api, source):
        fake_api.routes["POST /drafts"] = draft()
        async with make_client() as client:
            await call(client, "draft_application",
                       {**source, "cv_tex": CV_TEX, "cover_letter_body": BODY, "cv_template": "1page"})
        _assert_known_fields(api.DraftRequest, body(fake_api.calls("POST /drafts")[0]))
