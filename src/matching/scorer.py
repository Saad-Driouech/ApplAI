"""
Job scoring orchestrator.

Reads 'new' jobs from the database, sends each through the Gemini client,
stores the score, and marks jobs as 'queued' (above threshold) or 'skipped'.

A keyword pre-filter runs first: jobs whose title + description contain zero
AI/ML-related terms are auto-skipped without an LLM call (saves quota).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import src.database as db
from src.logger import audit, get_logger
from src.matching.gemini_client import BudgetExceeded, ScoreParseError, ScoreResult, http_status
from src.scrapers.location import REMOTE
from src.utils.jd_sanitizer import sanitize_jd

log = get_logger(__name__)

_CONFIG_ERROR_STATUS = {401, 403, 404}

# ── Keyword pre-filter ────────────────────────────────────────────────────
# Jobs that mention NONE of these terms are auto-skipped before LLM scoring.
# Keep this list broad — false negatives (skipping a good job) are worse than
# false positives (scoring a mediocre one).
_AI_KEYWORDS = {
    # General AI/ML terms
    "artificial intelligence", "machine learning", "deep learning",
    "neural network", "natural language processing", "nlp",
    "computer vision", "reinforcement learning", "generative ai",
    "genai", "gen ai", "llm", "large language model",
    "foundation model", "transformer", "diffusion model",
    "conversational ai", "chatbot", "recommendation system",
    "predictive model", "anomaly detection", "speech recognition",
    "image recognition", "object detection", "semantic search",
    "vector database", "retrieval augmented", "fine-tuning",
    "prompt engineering", "mlops", "aiops", "data science",
    "data scientist", "ml engineer", "ai engineer", "applied scientist",
    "research scientist", "ai researcher",
    # Frameworks and libraries
    "pytorch", "tensorflow", "keras", "scikit-learn", "sklearn",
    "hugging face", "huggingface", "langchain", "llamaindex",
    "openai", "anthropic", "spacy", "nltk", "opencv",
    "jax", "xgboost", "lightgbm", "catboost",
    "pandas", "numpy", "scipy", "matplotlib",
    "spark mllib", "sagemaker", "vertex ai", "azure ml",
    "bedrock", "databricks", "mlflow", "kubeflow", "airflow",
    "wandb", "weights and biases", "dvc",
    "onnx", "triton", "tensorrt", "vllm",
    # Abbreviations commonly found in JDs
    "ai", "ai/ml", "ml/ai", "cv/nlp", "nlp/cv", "dl",
}

# One regex matching any keyword as a whole word (letter/digit boundaries,
# so keywords containing punctuation like "ai/ml" still work)
_AI_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(re.escape(kw) for kw in sorted(_AI_KEYWORDS, key=len, reverse=True))
    + r")(?![a-z0-9])",
    re.IGNORECASE,
)


def has_ai_keywords(title: str, description: str) -> bool:
    """Return True if the job title or description contains at least one AI-related keyword."""
    text = f"{title} {description}"
    return bool(_AI_PATTERN.search(text))


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
        user_preferences: str = "",
        fallback_client=None,
        allowed_countries: Optional[frozenset] = None,
    ):
        self._db_path = db_path
        self._client = client
        self._cv_summary = cv_summary
        self._threshold = score_threshold
        self._batch_size = batch_size
        self._user_preferences = user_preferences
        self._fallback_client = fallback_client
        self._allowed_countries = allowed_countries

    def run(self) -> dict[str, int]:
        """
        Pre-filter every 'new' job, then LLM-score up to batch_size of them (newest first).
        Returns a summary dict:
        {scored, queued, skipped, filtered, not_a_job, deferred, errors, parse_errors, quota_hit}

        Deferred and failed jobs stay 'new' for the next run.
        """
        stats = {
            "scored": 0, "queued": 0, "skipped": 0, "filtered": 0, "not_a_job": 0,
            "deferred": 0, "errors": 0, "parse_errors": 0, "quota_hit": 0,
        }

        with db.get_conn(self._db_path) as conn:
            jobs = db.get_jobs_by_status(conn, "new", limit=None)

        log.info("Scoring: %d new jobs, up to %d LLM calls", len(jobs), self._batch_size)

        llm_calls = 0
        llm_stopped = False   # quota exhausted or provider misconfigured

        for row in jobs:
            job = dict(row)
            job_id = job["id"]

            # ── Location pre-filter: skip jobs outside target countries ──
            country = job.get("country")
            if self._allowed_countries and country != REMOTE and country not in self._allowed_countries:
                with db.get_conn(self._db_path) as conn:
                    db.update_score(
                        conn,
                        job_id=job_id,
                        score=0.0,
                        reasoning=f"Auto-skipped: location outside target countries ({job.get('city') or job.get('country')})",
                        new_status="skipped",
                        skip_reason="location",
                    )
                stats["filtered"] += 1
                continue

            # ── Keyword pre-filter: skip non-AI jobs without LLM call ────
            if not has_ai_keywords(job.get("title", ""), job.get("description", "")):
                with db.get_conn(self._db_path) as conn:
                    db.update_score(
                        conn,
                        job_id=job_id,
                        score=0.0,
                        reasoning="Auto-skipped: no AI/ML keywords in title or description",
                        new_status="skipped",
                        skip_reason="keyword_filter",
                    )
                stats["filtered"] += 1
                log.debug(
                    "Job filtered (no AI keywords) | %s @ %s",
                    job["title"], job["company"],
                )
                continue

            if llm_stopped or llm_calls >= self._batch_size:
                stats["deferred"] += 1
                continue
            llm_calls += 1

            try:
                result = self._score_one(job)
            except BudgetExceeded as exc:
                if self._fallback_client:
                    log.warning("Quota hit, switching to Ollama fallback: %s", exc)
                    self._client = self._fallback_client
                    self._fallback_client = None  # only switch once
                    stats["quota_hit"] += 1
                    # Retry this job with the fallback client
                    try:
                        result = self._score_one(job)
                    except Exception as fallback_exc:
                        log.error("Fallback scoring failed for job %s: %s", job_id, fallback_exc)
                        stats["errors"] += 1
                        self._record_failure(job_id, fallback_exc)
                        continue
                else:
                    log.warning("Quota hit, no fallback available: %s", exc)
                    stats["quota_hit"] += 1
                    stats["deferred"] += 1
                    llm_stopped = True
                    continue
            except ScoreParseError as exc:
                # Never record an unparseable reply as a score — leave the job for retry
                log.warning("Unusable model reply for job %s: %s", job_id, exc)
                stats["parse_errors"] += 1
                self._record_failure(job_id, exc)
                continue
            except Exception as exc:
                stats["errors"] += 1
                if http_status(exc) in _CONFIG_ERROR_STATUS:
                    # Bad key / removed model: every remaining job would fail the same way
                    log.error("LLM scoring stopped, provider configuration error: %s", exc)
                    llm_stopped = True
                    continue
                log.error("Scoring error for job %s: %s", job_id, exc, exc_info=True)
                self._record_failure(job_id, exc)
                continue

            if not result.is_job_posting:
                with db.get_conn(self._db_path) as conn:
                    db.update_score(
                        conn,
                        job_id=job_id,
                        score=result.score,
                        reasoning=result.reasoning,
                        new_status="skipped",
                        skip_reason="not_a_job",
                    )
                stats["not_a_job"] += 1
                log.info("Not a job posting, skipped | %s @ %s", job["title"], job["company"])
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
                    skip_reason="low_score" if not above_threshold else None,
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

    def _record_failure(self, job_id: str, exc: Exception) -> None:
        with db.get_conn(self._db_path) as conn:
            if db.record_failure(conn, job_id, "new", f"{type(exc).__name__}: {exc}"):
                log.error("Giving up on scoring job %s after %d failed attempts", job_id, db.MAX_ATTEMPTS)

    def score_single(self, job: dict) -> ScoreResult:
        """Score one job outside the batch flow, switching to the fallback client on quota errors."""
        try:
            return self._score_one(job)
        except BudgetExceeded as exc:
            if not self._fallback_client:
                raise
            log.warning("Quota hit, switching to fallback client: %s", exc)
            self._client, self._fallback_client = self._fallback_client, None
            return self._score_one(job)

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

        return self._client.score_job(job, self._cv_summary, self._user_preferences)
