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
  GET  /jobs/{job_id}         — one job with its latest application

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
import time
from pathlib import Path
from datetime import datetime
from threading import Lock
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse

import src.logger as logger_module
from src.config import get as get_config
import src.database as db
from src.logger import audit, get_logger
from src.api_models import (
    COUNTRY_PATTERN,
    DuplicatesResponse,
    GenerateRequest,
    GenerateResponse,
    JobInput,
    JobListResponse,
    JobSummary,
    ResolvedJob,
    ScoreResponse,
)
from src.matching.scorer import has_ai_keywords
from src.pipeline import (
    build_generators,
    generate_one,
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
    preferences = build_preference_context(cfg.paths.db_path)
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


def _score(cfg, job: dict):
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
        job=ResolvedJob(
            title=job["title"], company=job["company"], country=job["country"], city=job["city"],
            source_url=job["source_url"], description_chars=len(job["description"]),
        ),
        duplicates=_duplicates(cfg, job["company"], job["title"]),
        sanitizer_flags=sanitizer_flags,
    )


def _queue_existing(cfg, job_id: str) -> dict:
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

    if job["score"] is None:
        # Hold the score lock so a concurrent phase_score cannot re-score this row
        with phase_lock(cfg.paths.db_path.parent, "score"):
            result = _score(cfg, job)
            with db.get_conn(cfg.paths.db_path) as conn:
                db.update_score(conn, job_id, result.score, result.reasoning, new_status="queued")
    else:
        with db.get_conn(cfg.paths.db_path) as conn:
            db.complete_stage(conn, job_id, "queued")

    with db.get_conn(cfg.paths.db_path) as conn:
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())


def _queue_new(cfg, req: JobInput) -> dict:
    from src.scrapers.base import _job_id

    job, _ = _resolve_job(req)
    if not (job["title"] and job["company"]):
        raise HTTPException(status_code=422, detail="'title' and 'company' are required to generate documents")
    if not re.fullmatch(COUNTRY_PATTERN, job["country"]):
        raise HTTPException(status_code=422, detail="'country' must be an ISO alpha-2 code or REMOTE")
    job.update(id=_job_id(MCP_SOURCE, job["external_id"]), source=MCP_SOURCE)

    def already_exists(conn) -> None:
        originals = db.find_by_dedup_key(conn, job["company"], job["title"])
        if originals or db.job_exists(conn, MCP_SOURCE, job["external_id"]):
            raise HTTPException(status_code=409, detail={
                "message": "This job already exists; pass its job_id to generate for it",
                "existing_job_id": originals[0]["id"] if originals else job["id"],
            })

    with db.get_conn(cfg.paths.db_path) as conn:
        already_exists(conn)  # before spending an LLM call

    # Score before inserting so the row is never visible to phase_score as 'new'
    result = _score(cfg, job)
    with db.get_conn(cfg.paths.db_path) as conn:
        if not db.upsert_job(conn, job):
            already_exists(conn)
        db.update_score(conn, job["id"], result.score, result.reasoning, new_status="queued")
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone())


@app.post("/jobs/generate", dependencies=_auth, response_model=GenerateResponse)
def generate_job(req: GenerateRequest):
    """Generate CV + cover letter for one job into .pending/; delivery and approval follow the normal flow."""
    cfg = _get_cfg()
    try:
        cv_template = resolve_cv_template(req.cv_template)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

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


@app.get("/jobs/{job_id}", dependencies=_auth, response_model=JobSummary)
def get_job(job_id: str):
    cfg = _get_cfg()
    with db.get_conn(cfg.paths.db_path) as conn:
        row = db.get_job_summary(conn, job_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobSummary(**dict(row))


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
