"""
FastAPI sidecar — exposes pipeline phases as HTTP endpoints for n8n.

Endpoints:
  POST /scrape                — run all scrapers
  POST /score                 — score 'new' jobs via Gemini/Groq
  POST /generate              — generate CV + cover letter for 'queued' jobs
  POST /deliver               — send ready bundles to Discord + log to Notion
  POST /discord/interactions  — Discord button interaction webhook
  GET  /health                — liveness check
  GET  /stats                 — job counts by status
  POST /jobs/score            — score one job (text or URL) without storing it
  POST /jobs/generate         — generate a bundle for one job into .pending/
  GET  /jobs                  — filtered job listing
  GET  /jobs/duplicates       — jobs matching a company + title
  GET  /jobs/{job_id}         — one job with its description and latest application

Claude-authored flow (Claude scores and writes through the MCP server, ApplAI stores and renders):
  POST /jobs/resolve          — fetch/sanitize a posting and report filters and duplicates
  GET  /scoring/context       — rubric, profile, learned preferences and threshold
  POST /jobs/prefilter        — expire stale jobs and auto-skip 'new' jobs failing a pre-filter
  GET  /jobs/to-score         — 'new' jobs with sanitized descriptions
  POST /jobs/scores           — store Claude's scores (queued or skipped by threshold)
  GET  /generation/context    — CV template, writing rules and profile
  POST /drafts                — render Claude's CV + cover letter into .pending/<draft_id>/
  GET  /drafts/{draft_id}     — a draft's page counts and job
  POST /drafts/{id}/commit    — turn a draft into an application and deliver it to Discord + Notion

Without LLM keys, /score, /generate, /jobs/score and /jobs/generate return 503.
All routes except /health and /discord/interactions (Ed25519-verified)
require the X-API-Key header.
"""
from __future__ import annotations

import binascii
import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from datetime import datetime
from threading import Lock
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse

import src.drafts as drafts
import src.logger as logger_module
from src.config import get as get_config
import src.database as db
from src.logger import audit, get_logger
from src.api_models import (
    COUNTRY_PATTERN,
    CV_TEMPLATE_NAME,
    DraftRequest,
    DraftResponse,
    DuplicatesResponse,
    GenerateRequest,
    GenerateResponse,
    GenerationContext,
    JobInput,
    JobDetail,
    JobListResponse,
    JobsToScoreResponse,
    JobSummary,
    JobToScore,
    PrefilterResponse,
    ResolvedJob,
    ResolveResponse,
    ScoreOutcome,
    ScoreRecord,
    ScoreResponse,
    ScoresRequest,
    ScoresResponse,
    ScoringContext,
)
from src.documents.cover_letter import CL_RULES, CoverLetterError
from src.documents.cv_generator import CV_RULES, CVGenerationError, CVGenerator
from src.matching.gemini_client import SCORING_RUBRIC, location_text
from src.matching.response_parser import ScoreResult
from src.matching.scorer import apply_score, has_ai_keywords, is_excluded_title, prefilter_new_jobs
from src.pipeline import (
    LLMDisabled,
    build_generators,
    cleanup_stale_bundles,
    expire_stale,
    generate_one,
    load_cv_summary,
    phase_deliver,
    phase_generate,
    phase_score,
    phase_scrape,
    resolve_cv_template,
    score_single_job,
)
from src.scrapers.single_url import JobFetchError, fetch_job_posting
from src.utils.jd_sanitizer import sanitize_jd
from src.utils.locks import PhaseBusy, phase_lock
from src.utils.sanitize import sanitize_job

log = get_logger(__name__)

app = FastAPI(title="ApplAI API", version="1.0")

_cfg = None
_cfg_lock = Lock()

# Discord signs the timestamp; reject anything older to stop replays
_INTERACTION_MAX_AGE_SECONDS = 300


def _get_cfg():
    global _cfg
    with _cfg_lock:
        if _cfg is None:
            cfg = get_config()
            logger_module.setup(logs_dir=cfg.paths.logs_dir, debug=cfg.debug)
            db.init_db(cfg.paths.db_path).close()
            _cfg = cfg
    return _cfg


def require_api_key(x_api_key: str | None = Header(None)) -> None:
    expected = _get_cfg().api_key
    if not expected:
        raise HTTPException(status_code=503, detail="APPLAI_API_KEY is not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


_auth = [Depends(require_api_key)]


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    """Serve the web dashboard."""
    html_path = Path(__file__).parent / "static" / "index.html"
    return html_path.read_text(encoding="utf-8")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stats", dependencies=_auth)
def stats():
    cfg = _get_cfg()
    with db.get_conn(cfg.paths.db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
        ).fetchall()
    return {"jobs": {row["status"]: row["count"] for row in rows}}


@app.get("/feedback", dependencies=_auth)
def feedback():
    """Analyze approval/rejection history, recommend threshold adjustments, and suggest keywords."""
    from src.feedback.analyzer import analyze
    from src.feedback.preferences import build_preference_context, suggest_keyword_additions
    cfg = _get_cfg()
    report = analyze(cfg.paths.db_path, current_threshold=cfg.score_threshold)
    preferences = build_preference_context(cfg.paths.db_path, since=cfg.preferences_since)
    keyword_suggestions = suggest_keyword_additions(cfg.paths.db_path)
    return {
        "total_decisions": report.total_decisions,
        "overall_approval_rate": round(report.overall_approval_rate, 2),
        "approved": report.total_approved,
        "rejected": report.total_rejected,
        "current_threshold": report.current_threshold,
        "recommended_threshold": report.recommended_threshold,
        "recommendation": report.recommendation_reason,
        "bands": [
            {
                "range": b.label,
                "total": b.total,
                "approved": b.approved,
                "rejected": b.rejected,
                "approval_rate": round(b.approval_rate, 2),
            }
            for b in report.bands
        ],
        "learned_preferences": preferences or "Not enough decisions yet",
        "keyword_suggestions": keyword_suggestions,
    }


@app.get("/scrape-runs", dependencies=_auth)
def scrape_runs():
    """Return the last 30 scrape runs for the dashboard."""
    cfg = _get_cfg()
    with db.get_conn(cfg.paths.db_path) as conn:
        rows = conn.execute("""
            SELECT source, started_at, finished_at, jobs_found, jobs_new, error
            FROM scrape_runs
            ORDER BY started_at DESC
            LIMIT 30
        """).fetchall()
    return {"runs": [dict(r) for r in rows]}


# ── Single-job routes (used by the MCP server) ───────────────────────────

_JOB_STATUSES = r"^(new|queued|generating|ready|delivered|approved|rejected|skipped|failed)$"
_DONE_STATUSES = {"generating", "ready", "delivered", "approved", "rejected"}
MCP_SOURCE = "mcp"


def _resolve_job(req: JobInput) -> tuple[dict, list[str]]:
    """
    Sanitized job dict from pasted text or a fetched URL (explicit fields override fetched ones),
    plus the JD sanitizer flags. Severe prompt injection is rejected with 422.
    """
    if req.url:
        try:
            raw = fetch_job_posting(req.url)
        except JobFetchError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    else:
        raw = {"description": req.description}
    for field in ("title", "company", "city", "country"):
        if getattr(req, field):
            raw[field] = getattr(req, field)
    if not raw.get("external_id"):
        key = f"{raw.get('company', '')}|{raw.get('title', '')}|{raw['description']}"
        raw["external_id"] = hashlib.sha256(key.encode()).hexdigest()[:32]
    job = sanitize_job(raw)

    flags = sanitize_jd(job["description"])
    if flags["blocked"]:
        raise HTTPException(
            status_code=422,
            detail=f"Job description blocked by the prompt-injection filter ({', '.join(flags['flags'])})",
        )
    return job, flags["flags"]


_LLM_DISABLED = "The LLM pipeline is disabled (no API key); Claude scores and writes through the MCP server"


def _score(cfg, job: dict):
    if not cfg.scoring_llm_enabled:
        raise HTTPException(status_code=503, detail=_LLM_DISABLED)
    try:
        return score_single_job(cfg, job)
    except Exception as exc:
        log.error("Single-job scoring failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Scoring failed: {type(exc).__name__}: {exc}")


def _duplicates(cfg, company: str, title: str) -> list[JobSummary]:
    if not (company and title):
        return []
    with db.get_conn(cfg.paths.db_path) as conn:
        return [JobSummary(**dict(r)) for r in db.find_by_dedup_key(conn, company, title)]


@app.post("/jobs/score", dependencies=_auth, response_model=ScoreResponse)
def score_job(req: JobInput):
    """Score one job against the profile. Nothing is stored except the LLM usage counter."""
    cfg = _get_cfg()
    job, sanitizer_flags = _resolve_job(req)
    result = _score(cfg, job)
    return ScoreResponse(
        score=result.score,
        threshold=cfg.score_threshold,
        reasoning=result.reasoning,
        must_haves_met=result.must_haves_met,
        gaps=result.gaps,
        is_job_posting=result.is_job_posting,
        passes_keyword_filter=has_ai_keywords(job["title"], job["description"]),
        passes_title_filter=not is_excluded_title(job["title"]),
        job=_resolved(job),
        duplicates=_duplicates(cfg, job["company"], job["title"]),
        sanitizer_flags=sanitizer_flags,
    )


def _resolved(job: dict) -> ResolvedJob:
    return ResolvedJob(
        title=job["title"], company=job["company"], country=job["country"], city=job["city"],
        source_url=job["source_url"], description_chars=len(job["description"]),
    )


def _check_existing(cfg, job_id: str) -> dict:
    """The job row, if documents can still be generated for it (404/409 otherwise)."""
    with db.get_conn(cfg.paths.db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    job = dict(row)
    if job["status"] in _DONE_STATUSES:
        raise HTTPException(status_code=409, detail=f"Job {job_id} already has documents (status '{job['status']}')")
    if job["skip_reason"] == "duplicate":
        originals = _duplicates(cfg, job["company"], job["title"])
        raise HTTPException(status_code=409, detail={
            "message": f"Job {job_id} is a duplicate; generate for the original instead",
            "existing_job_id": originals[0].id if originals else None,
        })
    return job


def _queue_existing(cfg, job_id: str, claude_score: Optional[ScoreResult] = None) -> dict:
    """Queue an existing job for documents, scoring it first (with *claude_score* if given) when unscored."""
    job = _check_existing(cfg, job_id)
    if job["score"] is None:
        # Hold the score lock so a concurrent phase_score cannot re-score this row
        with phase_lock(cfg.paths.db_path.parent, "score"):
            result = claude_score or _score(cfg, job)
            with db.get_conn(cfg.paths.db_path) as conn:
                db.update_score(conn, job_id, result.score, result.reasoning, new_status="queued")
    else:
        with db.get_conn(cfg.paths.db_path) as conn:
            db.complete_stage(conn, job_id, "queued")

    with db.get_conn(cfg.paths.db_path) as conn:
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def _reject_existing(conn, job: dict) -> None:
    originals = db.find_by_dedup_key(conn, job["company"], job["title"])
    if originals or db.job_exists(conn, MCP_SOURCE, job["external_id"]):
        raise HTTPException(status_code=409, detail={
            "message": "This job already exists; pass its job_id to generate for it",
            "existing_job_id": originals[0]["id"] if originals else job["id"],
        })


def _resolve_new(cfg, req: JobInput) -> dict:
    """A new job ready to insert with source 'mcp' (422 for missing fields, 409 if it already exists)."""
    from src.scrapers.base import _job_id

    job, _ = _resolve_job(req)
    if not (job["title"] and job["company"]):
        raise HTTPException(status_code=422, detail="'title' and 'company' are required to generate documents")
    if not re.fullmatch(COUNTRY_PATTERN, job["country"]):
        raise HTTPException(status_code=422, detail="'country' must be an ISO alpha-2 code or REMOTE")
    job.update(id=_job_id(MCP_SOURCE, job["external_id"]), source=MCP_SOURCE)
    with db.get_conn(cfg.paths.db_path) as conn:
        _reject_existing(conn, job)
    return job


def _insert_new(cfg, job: dict, result: ScoreResult) -> dict:
    """Insert a resolved new job already scored, as 'queued'."""
    with db.get_conn(cfg.paths.db_path) as conn:
        if not db.upsert_job(conn, job):
            _reject_existing(conn, job)
        db.update_score(conn, job["id"], result.score, result.reasoning, new_status="queued")
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone())


def _queue_new(cfg, req: JobInput) -> dict:
    job = _resolve_new(cfg, req)  # before spending an LLM call
    # Score before inserting so the row is never visible to phase_score as 'new'
    return _insert_new(cfg, job, _score(cfg, job))


@app.post("/jobs/generate", dependencies=_auth, response_model=GenerateResponse)
def generate_job(req: GenerateRequest):
    """Generate CV + cover letter for one job into .pending/; delivery and approval follow the normal flow."""
    cfg = _get_cfg()
    if not cfg.documents_llm_enabled:
        raise HTTPException(status_code=503, detail=_LLM_DISABLED)
    cv_template = _cv_template_path(req.cv_template)

    try:
        with phase_lock(cfg.paths.db_path.parent, "generate"):
            generators = build_generators(cfg, cv_template)
            if generators is None:
                raise HTTPException(status_code=503, detail="CV or cover letter template is not configured")
            job = _queue_existing(cfg, req.job_id) if req.job_id else _queue_new(cfg, req.job)
            try:
                app_id = generate_one(cfg, job, *generators)
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"Generation failed: {type(exc).__name__}: {exc}")
            if app_id is None:
                raise HTTPException(status_code=409, detail=f"Job {job['id']} was claimed by another run")
    except PhaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    with db.get_conn(cfg.paths.db_path) as conn:
        app_row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
        status = conn.execute("SELECT status FROM jobs WHERE id = ?", (job["id"],)).fetchone()["status"]
    audit("job_generated_on_demand", job_id=job["id"], app_id=app_id)
    return GenerateResponse(
        job_id=job["id"],
        application_id=app_id,
        status=status,
        score=job["score"],
        cv_path=app_row["cv_path"],
        cover_letter_path=app_row["cover_letter_path"],
        approved_dir=_approved_dir(cfg, job),
    )


def _approved_dir(cfg, job: dict) -> str:
    """Where approving the bundle files it, relative to the working directory (same rule as promote_to_final)."""
    from src.utils.file_manager import final_dir

    folder = final_dir(cfg.paths.working_dir, job["country"] or "", job["company"] or "")
    return str(folder.relative_to(cfg.paths.working_dir))


# ── Claude-authored flow: Claude scores and writes, ApplAI stores and renders ──

def _cv_template_path(name: Optional[str]) -> Path:
    try:
        return resolve_cv_template(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _clean_description(text: Optional[str]) -> str:
    """The description as the scorer sees it: injection stripped, blocked text dropped."""
    if not text:
        return ""
    result = sanitize_jd(text)
    return "" if result["blocked"] else result["clean_text"]


def _claude_score(record: ScoreRecord) -> ScoreResult:
    return ScoreResult(score=record.score, reasoning=record.reasoning, must_haves_met=[], gaps=[],
                       raw_response="", is_job_posting=record.is_job_posting)


@app.post("/jobs/resolve", dependencies=_auth, response_model=ResolveResponse)
def resolve_job(req: JobInput):
    """Fetch or take a posting, sanitize it, and report the pre-filters and duplicates. No LLM call."""
    cfg = _get_cfg()
    job, sanitizer_flags = _resolve_job(req)
    return ResolveResponse(
        job=_resolved(job),
        description=_clean_description(job["description"]),
        location=location_text(job),
        passes_keyword_filter=has_ai_keywords(job["title"], job["description"]),
        passes_title_filter=not is_excluded_title(job["title"]),
        duplicates=_duplicates(cfg, job["company"], job["title"]),
        sanitizer_flags=sanitizer_flags,
    )


@app.get("/scoring/context", dependencies=_auth, response_model=ScoringContext)
def scoring_context():
    from src.feedback.preferences import build_preference_context

    cfg = _get_cfg()
    return ScoringContext(
        rubric=SCORING_RUBRIC,
        profile=load_cv_summary(cfg),
        learned_preferences=build_preference_context(cfg.paths.db_path, since=cfg.preferences_since),
        threshold=cfg.score_threshold,
        target_countries=sorted(cfg.allowed_countries),
    )


@app.post("/jobs/prefilter", dependencies=_auth, response_model=PrefilterResponse)
def prefilter_jobs():
    """Expire stale jobs, then skip 'new' jobs that fail the location, title or keyword pre-filter."""
    cfg = _get_cfg()
    try:
        with phase_lock(cfg.paths.db_path.parent, "score"):
            expired = expire_stale(cfg)
            skipped, remaining = prefilter_new_jobs(cfg.paths.db_path, cfg.allowed_countries)
    except PhaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return PrefilterResponse(expired=expired, skipped=skipped, remaining=len(remaining))


@app.get("/jobs/to-score", dependencies=_auth, response_model=JobsToScoreResponse)
def jobs_to_score(limit: int = Query(30, ge=1, le=50)):
    """Newest 'new' jobs with sanitized descriptions; run POST /jobs/prefilter first."""
    cfg = _get_cfg()
    with db.get_conn(cfg.paths.db_path) as conn:
        rows = [dict(r) for r in db.get_jobs_by_status(conn, "new", limit=limit)]
    jobs = [
        JobToScore(
            id=r["id"], title=r["title"], company=r["company"], location=location_text(r),
            source_url=r["source_url"], posted_at=r["posted_at"],
            description=_clean_description(r["description"])[:3000],
        )
        for r in rows
    ]
    return JobsToScoreResponse(count=len(jobs), jobs=jobs)


@app.post("/jobs/scores", dependencies=_auth, response_model=ScoresResponse)
def record_scores(req: ScoresRequest):
    """Store Claude's scores for 'new' jobs: queued at or above the threshold, skipped otherwise."""
    cfg = _get_cfg()
    results = []
    try:
        with phase_lock(cfg.paths.db_path.parent, "score"):
            for item in req.scores:
                with db.get_conn(cfg.paths.db_path) as conn:
                    row = conn.execute("SELECT status FROM jobs WHERE id = ?", (item.job_id,)).fetchone()
                    if row is None or row["status"] != "new":
                        error = "not found" if row is None else f"status is '{row['status']}'; only 'new' jobs are scored"
                        results.append(ScoreOutcome(job_id=item.job_id, error=error))
                        continue
                    status, skip_reason = apply_score(conn, item.job_id, _claude_score(item), cfg.score_threshold)
                audit("job_scored", job_id=item.job_id, score=item.score, scored_by=item.scored_by)
                results.append(ScoreOutcome(job_id=item.job_id, status=status, skip_reason=skip_reason))
    except PhaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return ScoresResponse(results=results)


@app.get("/generation/context", dependencies=_auth, response_model=GenerationContext)
def generation_context(
    job_id: Optional[str] = Query(None, max_length=64),
    cv_template: Optional[str] = Query(None, max_length=50, pattern=CV_TEMPLATE_NAME),
):
    """What Claude needs to write the documents: the CV template, the writing rules and the profile."""
    cfg = _get_cfg()
    template_path = _cv_template_path(cv_template)
    if not template_path.is_file():
        raise HTTPException(status_code=503, detail="CV template is not configured")
    job = None
    if job_id:
        job = _job_detail(cfg, job_id)
        job.description = _clean_description(job.description)
    return GenerationContext(
        job=job,
        cv_template=cv_template or "default",
        cv_max_pages=CVGenerator(template_path, cfg.paths.working_dir).template_pages() or 1,
        cv_template_tex=template_path.read_text(encoding="utf-8"),
        cv_rules=CV_RULES,
        cover_letter_rules=CL_RULES,
        profile=load_cv_summary(cfg),
    )


def _draft_response(record: dict) -> DraftResponse:
    job, score = record["job"], record["score"] or {}
    cv_max_pages = record.get("cv_max_pages") or 1
    warnings = []
    for name, pages, limit in (("CV", record["cv_pages"], cv_max_pages),
                               ("Cover letter", record["cover_letter_pages"], 1)):
        if pages is None:
            warnings.append(f"{name} page count unknown; check the PDF")
        elif pages > limit:
            warnings.append(f"{name} is {pages} pages; it must fit on {limit} page{'s' if limit > 1 else ''}")
    return DraftResponse(
        draft_id=record["draft_id"],
        job_id=record["job_id"],
        title=job["title"],
        company=job["company"],
        country=job["country"] or "",
        city=job.get("city") or "",
        score=score.get("score", job.get("score")),
        score_reasoning=score.get("reasoning", job.get("score_reasoning")),
        cv_template=record["cv_template"] or "default",
        cv_pages=record["cv_pages"],
        cv_max_pages=cv_max_pages,
        cover_letter_pages=record["cover_letter_pages"],
        warnings=warnings,
        committed=bool(record["committed_at"]),
    )


def _load_draft(cfg, draft_id: str) -> dict:
    try:
        return drafts.load_draft(cfg.paths.working_dir, draft_id)
    except drafts.DraftNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/drafts", dependencies=_auth, response_model=DraftResponse)
def create_draft(req: DraftRequest):
    """
    Render Claude's CV LaTeX and cover letter body into .pending/<draft_id>/ and report page counts.
    The database is not touched until the draft is committed.
    """
    cfg = _get_cfg()
    generators = build_generators(cfg, _cv_template_path(req.cv_template))
    if generators is None:
        raise HTTPException(status_code=503, detail="CV or cover letter template is not configured")
    if req.job_id:
        job, new_job = _check_existing(cfg, req.job_id), None
    else:
        job = new_job = _resolve_new(cfg, req.job)
    if job.get("score") is None and req.score is None:
        raise HTTPException(status_code=422, detail="This job has no score yet; score it and pass 'score'")

    cleanup_stale_bundles(cfg)
    meta = {
        "job_id": req.job_id,
        "new_job": new_job,
        "job": {k: job.get(k) for k in ("title", "company", "country", "city", "score", "score_reasoning")},
        "score": req.score.model_dump() if req.score else None,
        "cv_template": req.cv_template,
        "cv_max_pages": generators[0].template_pages(),
    }
    try:
        record = drafts.create_draft(cfg.paths.working_dir, *generators, job, req.cv_tex, req.cover_letter_body, meta)
    except (CVGenerationError, CoverLetterError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=422, detail=f"Rendering failed: {exc}")
    return _draft_response(record)


@app.get("/drafts/{draft_id}", dependencies=_auth, response_model=DraftResponse)
def get_draft(draft_id: str):
    return _draft_response(_load_draft(_get_cfg(), draft_id))


@app.post("/drafts/{draft_id}/commit", dependencies=_auth, response_model=GenerateResponse)
def commit_draft(draft_id: str, deliver: bool = Query(True, description="Post it to Discord and Notion now")):
    """
    Create or queue the draft's job, register the draft as a 'ready' application, and deliver it.
    A delivery failure leaves it 'ready' for the next deliver run and is reported in delivery_error.
    """
    cfg = _get_cfg()
    record = _load_draft(cfg, draft_id)
    try:
        with phase_lock(cfg.paths.db_path.parent, "generate"):
            if record["committed_at"]:
                raise HTTPException(status_code=409, detail=f"Draft {draft_id} was already committed")
            claude_score = _claude_score(ScoreRecord(**record["score"])) if record["score"] else None
            if record["job_id"]:
                job = _queue_existing(cfg, record["job_id"], claude_score)
            else:
                job = _insert_new(cfg, record["new_job"], claude_score)
            with db.get_conn(cfg.paths.db_path) as conn:
                if not db.start_generation(conn, job["id"]):
                    raise HTTPException(status_code=409, detail=f"Job {job['id']} was claimed by another run")
                db.create_application(conn, {
                    "id": draft_id,
                    "job_id": job["id"],
                    "cv_path": record["cv_path"],
                    "cover_letter_path": record["cover_letter_path"],
                })
                db.complete_stage(conn, job["id"], "ready")
            drafts.mark_committed(cfg.paths.working_dir, record)
    except PhaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    audit("draft_committed", draft_id=draft_id, job_id=job["id"])

    status, delivery_error = "ready", None
    if deliver:
        try:
            if phase_deliver(cfg, app_ids=[draft_id]):
                status = "delivered"
            else:
                delivery_error = "Sending to Discord failed (see the ApplAI log); the next 'deliver' run retries it"
        except PhaseBusy:
            delivery_error = "Another delivery is running; the next 'deliver' run sends it"
        except Exception as exc:
            log.error("Delivering draft %s failed", draft_id, exc_info=True)
            delivery_error = f"Delivery failed ({type(exc).__name__}); the next 'deliver' run retries it"

    return GenerateResponse(
        job_id=job["id"],
        application_id=draft_id,
        status=status,
        score=job["score"],
        cv_path=record["cv_path"],
        cover_letter_path=record["cover_letter_path"],
        approved_dir=_approved_dir(cfg, job),
        delivery_error=delivery_error,
    )


@app.get("/jobs", dependencies=_auth, response_model=JobListResponse)
def list_jobs(
    status: Optional[str] = Query(None, pattern=_JOB_STATUSES),
    country: Optional[str] = Query(None, pattern=COUNTRY_PATTERN),
    company: Optional[str] = Query(None, min_length=1, max_length=150),
    since: Optional[str] = Query(None, description="ISO date or datetime (UTC) on scraped_at"),
    min_score: Optional[float] = Query(None, ge=0, le=10),
    limit: int = Query(50, ge=1, le=500),
):
    if since:
        try:
            datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(status_code=422, detail="'since' must be an ISO date or datetime")
    cfg = _get_cfg()
    with db.get_conn(cfg.paths.db_path) as conn:
        rows = db.list_jobs(
            conn, status=status, country=country, company=company,
            since=since, min_score=min_score, limit=limit,
        )
    jobs = [JobSummary(**dict(r)) for r in rows]
    return JobListResponse(count=len(jobs), jobs=jobs)


@app.get("/jobs/duplicates", dependencies=_auth, response_model=DuplicatesResponse)
def job_duplicates(
    company: str = Query(..., min_length=1, max_length=150),
    title: str = Query(..., min_length=1, max_length=200),
):
    cfg = _get_cfg()
    return DuplicatesResponse(
        dedup_key=db._compute_dedup_key(company, title),
        matches=_duplicates(cfg, company, title),
    )


def _job_detail(cfg, job_id: str) -> JobDetail:
    with db.get_conn(cfg.paths.db_path) as conn:
        row = db.get_job_summary(conn, job_id)
        if row:
            description = conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    if not row:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobDetail(**dict(row), description=description)


@app.get("/jobs/{job_id}", dependencies=_auth, response_model=JobDetail)
def get_job(job_id: str, clean: bool = Query(False, description="Description after the prompt-injection filter")):
    detail = _job_detail(_get_cfg(), job_id)
    if clean:
        detail.description = _clean_description(detail.description)
    return detail


@app.post("/scrape", dependencies=_auth)
def scrape():
    cfg = _get_cfg()
    try:
        new_jobs = phase_scrape(cfg)
        return {"new_jobs": new_jobs}
    except PhaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/score", dependencies=_auth)
def score():
    cfg = _get_cfg()
    try:
        stats = phase_score(cfg)
        return stats
    except LLMDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except PhaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/generate", dependencies=_auth)
def generate():
    cfg = _get_cfg()
    try:
        generated = phase_generate(cfg)
        return {"generated": generated}
    except LLMDisabled as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except PhaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/deliver", dependencies=_auth)
def deliver():
    cfg = _get_cfg()
    try:
        delivered = phase_deliver(cfg)
        return {"delivered": delivered}
    except PhaseBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/digest", dependencies=_auth)
def digest():
    """Send a digest of recently skipped jobs to Discord for review."""
    cfg = _get_cfg()
    from src.delivery.discord_bot import DiscordDelivery

    with db.get_conn(cfg.paths.db_path) as conn:
        rows = db.get_recent_skipped(conn, days=7)

    jobs = [dict(r) for r in rows]
    if not jobs:
        return {"sent": 0, "message": "No recently skipped jobs to review"}

    discord = DiscordDelivery(
        bot_token=cfg.discord.bot_token,
        channel_id=cfg.discord.channel_id,
    )
    try:
        discord.send_skipped_digest(jobs)
    finally:
        discord.close()

    return {"sent": len(jobs)}


@app.post("/discord/interactions")
async def discord_interactions(
    request: Request,
    background_tasks: BackgroundTasks,
    x_signature_ed25519: str = Header(None),
    x_signature_timestamp: str = Header(None),
):
    """
    Receives Discord button interactions (Approve / Reject).

    Discord requires:
      1. Ed25519 signature verification on every request.
      2. A response within 3 seconds.

    We ACK immediately (type 6 = deferred message update) and process
    the decision in a background task.
    """
    body = await request.body()
    cfg = _get_cfg()

    if not _verify_discord_signature(
        cfg.discord.public_key,
        x_signature_ed25519 or "",
        x_signature_timestamp or "",
        body,
    ):
        return Response(content="Invalid request signature", status_code=401)
    if not _timestamp_fresh(x_signature_timestamp):
        return Response(content="Stale request timestamp", status_code=401)

    payload = json.loads(body)
    interaction_type = payload.get("type")

    # Type 1 — Discord PING (sent when registering the URL)
    if interaction_type == 1:
        return {"type": 1}

    # Type 3 — button click
    if interaction_type == 3:
        user = payload.get("member", {}).get("user") or payload.get("user") or {}
        if cfg.discord.allowed_user_ids and user.get("id") not in cfg.discord.allowed_user_ids:
            # Ephemeral reply, visible only to the clicker
            return {"type": 4, "data": {"content": "You are not allowed to decide on applications.", "flags": 64}}

        custom_id = payload.get("data", {}).get("custom_id", "")
        msg_id = payload.get("message", {}).get("id", "")

        if custom_id.startswith("approve_"):
            app_id, decision = custom_id[len("approve_"):], "approved"
        elif custom_id.startswith("reject_"):
            app_id, decision = custom_id[len("reject_"):], "rejected"
        elif custom_id.startswith("rescue_"):
            job_id = custom_id[len("rescue_"):]
            background_tasks.add_task(_process_rescue, job_id, msg_id)
            return {"type": 6}
        else:
            return Response(content="Unknown interaction", status_code=400)

        background_tasks.add_task(_process_decision, app_id, decision, msg_id)
        # Deferred message update — no loading spinner, we'll edit the message ourselves
        return {"type": 6}

    return Response(content="Unhandled interaction type", status_code=400)


def _timestamp_fresh(timestamp: str | None) -> bool:
    try:
        return abs(time.time() - int(timestamp)) <= _INTERACTION_MAX_AGE_SECONDS
    except (TypeError, ValueError):
        return False


def _verify_discord_signature(
    public_key_hex: str, signature_hex: str, timestamp: str, body: bytes
) -> bool:
    if not public_key_hex or not signature_hex or not timestamp:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = Ed25519PublicKey.from_public_bytes(binascii.unhexlify(public_key_hex))
        key.verify(binascii.unhexlify(signature_hex), timestamp.encode() + body)
        return True
    except Exception:
        return False


def _process_rescue(job_id: str, msg_id: str) -> None:
    """Background task: rescue a skipped job by changing its status to 'queued'."""
    cfg = _get_cfg()

    with db.get_conn(cfg.paths.db_path) as conn:
        row = conn.execute(
            "SELECT id, title, company, status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()

    if not row:
        log.warning("Rescue received for unknown job_id=%s", job_id)
        return

    with db.get_conn(cfg.paths.db_path) as conn:
        if not db.rescue_job(conn, job_id):
            log.info("Job %s is no longer skipped, ignoring rescue", job_id)
            return
        db.record_feedback_event(conn, job_id, "rescue")

    # Update Discord message to confirm rescue
    from src.delivery.discord_bot import DiscordDelivery
    discord = DiscordDelivery(
        bot_token=cfg.discord.bot_token,
        channel_id=cfg.discord.channel_id,
    )
    try:
        discord._http.patch(
            f"{discord._base_url}/channels/{discord._channel_id}/messages/{msg_id}",
            headers={**discord._headers, "Content-Type": "application/json"},
            content=json.dumps({
                "embeds": [{
                    "title": "Job Rescued",
                    "description": f"**{row['title']}** @ {row['company']} has been queued for document generation.",
                    "color": 0x57F287,  # green
                }],
                "components": [],
            }),
        )
    finally:
        discord.close()

    audit("job_rescued", job_id=job_id, title=row["title"], company=row["company"])
    log.info("Job rescued: %s — %s @ %s", job_id, row["title"], row["company"])


def _process_decision(app_id: str, decision: str, msg_id: str) -> None:
    """Background task: apply a button click. Idempotent — repeated clicks only re-sync Discord."""
    cfg = _get_cfg()

    with db.get_conn(cfg.paths.db_path) as conn:
        row = conn.execute(
            """SELECT a.*, j.title, j.company, j.country, j.id as job_id
               FROM applications a
               JOIN jobs j ON a.job_id = j.id
               WHERE a.id = ?""",
            (app_id,),
        ).fetchone()

    if not row:
        log.warning("Decision received for unknown app_id=%s", app_id)
        return

    app = dict(row)

    # Atomically claim the decision — the conditional UPDATE lets only one click win
    with db.get_conn(cfg.paths.db_path) as conn:
        claimed = db.record_user_decision(conn, app_id=app_id, decision=decision)
        existing = conn.execute(
            "SELECT user_decision FROM applications WHERE id = ?", (app_id,)
        ).fetchone()["user_decision"]

    if not claimed:
        log.info("App %s already decided (%s); ignoring repeated click", app_id, existing)
        _update_discord_decision(cfg, msg_id, existing, app.get("title", ""))
        return

    # Documents always live in .pending/<app_id> until a decision is applied
    from src.utils.file_manager import cleanup_pending, pending_dir, promote_to_final

    pending = pending_dir(cfg.paths.working_dir, app_id)
    try:
        if decision == "approved":
            paths = promote_to_final(
                pending=pending,
                working_dir=cfg.paths.working_dir,
                country=app.get("country") or "",
                company=app.get("company") or "",
                candidate_name=os.environ.get("CANDIDATE_NAME", "Candidate"),
            )
            with db.get_conn(cfg.paths.db_path) as conn:
                conn.execute(
                    "UPDATE applications SET cv_path = ?, cover_letter_path = ? WHERE id = ?",
                    (paths["cv_path"], paths["cover_letter_path"], app_id),
                )
                db.update_status(conn, app["job_id"], "approved")
        else:
            cleanup_pending(pending)
            with db.get_conn(cfg.paths.db_path) as conn:
                db.update_status(conn, app["job_id"], "rejected")
    except Exception:
        log.error("Applying decision failed for app %s; releasing claim", app_id, exc_info=True)
        with db.get_conn(cfg.paths.db_path) as conn:
            db.release_user_decision(conn, app_id)
        raise

    # Notion is a secondary record — its failure must not leave Discord buttons live
    if app.get("notion_page_id"):
        from src.delivery.notion_tracker import NotionTracker
        try:
            NotionTracker(
                api_token=cfg.notion.api_token,
                database_id=cfg.notion.job_tracker_db_id,
            ).record_decision(app["notion_page_id"], decision)
        except Exception:
            log.error("Notion update failed for app %s", app_id, exc_info=True)

    _update_discord_decision(cfg, msg_id, decision, app.get("title", ""))

    audit("decision_recorded", app_id=app_id, decision=decision)
    log.info("Decision recorded: app=%s decision=%s", app_id, decision)


def _update_discord_decision(cfg, msg_id: str, decision: str, job_title: str) -> None:
    """Edit the Discord message to show the decision and remove the buttons."""
    from src.delivery.discord_bot import DiscordDelivery
    discord = DiscordDelivery(
        bot_token=cfg.discord.bot_token,
        channel_id=cfg.discord.channel_id,
    )
    try:
        discord.update_decision(msg_id, decision, job_title=job_title)
    finally:
        discord.close()
