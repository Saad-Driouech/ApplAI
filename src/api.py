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
"""
from __future__ import annotations

import binascii
import json
import os
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response

import src.logger as logger_module
from src.config import get as get_config
import src.database as db
from src.logger import audit, get_logger
from src.pipeline import phase_deliver, phase_generate, phase_score, phase_scrape

log = get_logger(__name__)

app = FastAPI(title="ApplAI API", version="1.0")

_cfg = None


def _get_cfg():
    global _cfg
    if _cfg is None:
        _cfg = get_config()
        logger_module.setup(logs_dir=_cfg.paths.logs_dir, debug=_cfg.debug)
        db.init_db(_cfg.paths.db_path)
    return _cfg


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/stats")
def stats():
    cfg = _get_cfg()
    with db.get_conn(cfg.paths.db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
        ).fetchall()
    return {"jobs": {row["status"]: row["count"] for row in rows}}


@app.post("/scrape")
def scrape():
    cfg = _get_cfg()
    try:
        new_jobs = phase_scrape(cfg)
        return {"new_jobs": new_jobs}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/score")
def score():
    cfg = _get_cfg()
    try:
        stats = phase_score(cfg)
        return stats
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/generate")
def generate():
    cfg = _get_cfg()
    try:
        generated = phase_generate(cfg)
        return {"generated": generated}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/deliver")
def deliver():
    cfg = _get_cfg()
    try:
        delivered = phase_deliver(cfg)
        return {"delivered": delivered}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


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

    public_key_hex = os.environ.get("DISCORD_PUBLIC_KEY", "")
    if not _verify_discord_signature(
        public_key_hex,
        x_signature_ed25519 or "",
        x_signature_timestamp or "",
        body,
    ):
        return Response(content="Invalid request signature", status_code=401)

    payload = json.loads(body)
    interaction_type = payload.get("type")

    # Type 1 — Discord PING (sent when registering the URL)
    if interaction_type == 1:
        return {"type": 1}

    # Type 3 — button click
    if interaction_type == 3:
        custom_id = payload.get("data", {}).get("custom_id", "")
        msg_id = payload.get("message", {}).get("id", "")

        if custom_id.startswith("approve_"):
            app_id, decision = custom_id[len("approve_"):], "approved"
        elif custom_id.startswith("reject_"):
            app_id, decision = custom_id[len("reject_"):], "rejected"
        else:
            return Response(content="Unknown interaction", status_code=400)

        background_tasks.add_task(_process_decision, app_id, decision, msg_id)
        # Deferred message update — no loading spinner, we'll edit the message ourselves
        return {"type": 6}

    return Response(content="Unhandled interaction type", status_code=400)


def _verify_discord_signature(
    public_key_hex: str, signature_hex: str, timestamp: str, body: bytes
) -> bool:
    if not public_key_hex or not signature_hex or not timestamp:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        key = Ed25519PublicKey.from_public_bytes(binascii.unhexlify(public_key_hex))
        key.verify(binascii.unhexlify(signature_hex), timestamp.encode() + body)
        return True
    except Exception:
        return False


def _process_decision(app_id: str, decision: str, msg_id: str) -> None:
    """Background task: update DB, Notion, and Discord message after a button click."""
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

    # Handle file operations based on decision
    from src.utils.file_manager import promote_to_final, cleanup_pending
    from pathlib import Path

    if decision == "approved":
        pending = Path(app.get("cv_path", "")).parent
        candidate_name = os.environ.get("CANDIDATE_NAME", "Candidate")
        paths = promote_to_final(
            pending=pending,
            working_dir=cfg.paths.working_dir,
            country=app.get("country", ""),
            company=app.get("company", ""),
            candidate_name=candidate_name,
        )
        # Update DB with final paths
        with db.get_conn(cfg.paths.db_path) as conn:
            conn.execute(
                "UPDATE applications SET cv_path = ?, cover_letter_path = ? WHERE id = ?",
                (paths["cv_path"], paths["cover_letter_path"], app_id),
            )
    else:
        pending = Path(app.get("cv_path", "")).parent
        cleanup_pending(pending)

    # Update DB status
    new_status = "approved" if decision == "approved" else "rejected"
    with db.get_conn(cfg.paths.db_path) as conn:
        db.record_user_decision(
            conn,
            app_id=app_id,
            decision=decision,
            notion_page_id=app.get("notion_page_id"),
        )
        db.update_status(conn, app["job_id"], new_status)

    # Update Notion
    if app.get("notion_page_id"):
        from src.delivery.notion_tracker import NotionTracker
        NotionTracker(
            api_token=cfg.notion.api_token,
            database_id=cfg.notion.job_tracker_db_id,
        ).record_decision(app["notion_page_id"], decision)

    # Edit Discord message (remove buttons, show decision)
    from src.delivery.discord_bot import DiscordDelivery
    discord = DiscordDelivery(
        bot_token=cfg.discord.bot_token,
        channel_id=cfg.discord.channel_id,
    )
    try:
        discord.update_decision(msg_id, decision, job_title=app.get("title", ""))
    finally:
        discord.close()

    audit("decision_recorded", app_id=app_id, decision=decision)
    log.info("Decision recorded: app=%s decision=%s", app_id, decision)
