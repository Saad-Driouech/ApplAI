"""
Job scoring orchestrator.

Reads 'new' jobs from the database, sends each through the Gemini client,
stores the score, and marks jobs as 'queued' (above threshold) or 'skipped'.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import src.database as db
from src.logger import audit, get_logger
from src.matching.gemini_client import BudgetExceeded, ScoreResult
from src.utils.jd_sanitizer import sanitize_jd

log = get_logger(__name__)


class Scorer:
    """
    Orchestrates the scoring pipeline:
      1. Fetch unscored jobs from DB
      2. Sanitize the job description (prompt-injection protection)
      3. Score via Gemini
      4. Persist score + update status
    """

    def __init__(
        self,
        db_path: Path,
        client,
        cv_summary: str,
        score_threshold: float = 6.0,
        batch_size: int = 50,
    ):
        self._db_path = db_path
        self._client = client
        self._cv_summary = cv_summary
        self._threshold = score_threshold
        self._batch_size = batch_size

    def run(self) -> dict[str, int]:
        """
        Score all 'new' jobs in the database.
        Returns a summary dict: {scored, queued, skipped, errors, quota_hit}
        """
        stats = {"scored": 0, "queued": 0, "skipped": 0, "errors": 0, "quota_hit": 0}

        with db.get_conn(self._db_path) as conn:
            jobs = db.get_jobs_by_status(conn, "new", limit=self._batch_size)

        log.info("Scoring batch: %d jobs to evaluate", len(jobs))

        for row in jobs:
            job = dict(row)
            job_id = job["id"]

            try:
                result = self._score_one(job)
            except BudgetExceeded as exc:
                log.warning("Gemini quota hit during scoring: %s", exc)
                stats["quota_hit"] += 1
                break
            except Exception as exc:
                log.error("Scoring error for job %s: %s", job_id, exc, exc_info=True)
                stats["errors"] += 1
                continue

            above_threshold = result.score >= self._threshold
            new_status = "queued" if above_threshold else "skipped"

            with db.get_conn(self._db_path) as conn:
                db.update_score(
                    conn,
                    job_id=job_id,
                    score=result.score,
                    reasoning=result.reasoning,
                    new_status=new_status,
                )

            stats["scored"] += 1
            if above_threshold:
                stats["queued"] += 1
                log.info(
                    "Job QUEUED | score=%.1f | %s @ %s",
                    result.score, job["title"], job["company"],
                )
            else:
                stats["skipped"] += 1
                log.debug(
                    "Job skipped | score=%.1f | %s @ %s",
                    result.score, job["title"], job["company"],
                )

        audit("scoring_run_complete", **stats)
        return stats

    def _score_one(self, job: dict) -> ScoreResult:
        """Sanitize the JD, then score it."""
        raw_desc = job.get("description") or ""
        if raw_desc:
            sanitized = sanitize_jd(raw_desc)
            if sanitized["blocked"]:
                log.warning(
                    "Job %s description blocked by JD sanitizer (flags: %s)",
                    job["id"],
                    sanitized["flags"],
                )
                # Proceed with empty description rather than blocked content
                job = {**job, "description": ""}
            else:
                job = {**job, "description": sanitized["clean_text"]}

        return self._client.score_job(job, self._cv_summary)
