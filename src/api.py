"""
FastAPI sidecar — exposes pipeline phases as HTTP endpoints for n8n.

Endpoints:
  POST /scrape    — run all scrapers
  POST /score     — score 'new' jobs via Gemini
  POST /generate  — generate CV + cover letter for 'queued' jobs
  POST /deliver   — send ready bundles to Discord + log to Notion
  GET  /health    — liveness check
  GET  /stats     — job counts by status
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

import src.logger as logger_module
from src.config import get as get_config
import src.database as db
from src.pipeline import phase_deliver, phase_generate, phase_score, phase_scrape

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
