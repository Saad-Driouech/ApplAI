"""
Job scoring orchestrator.

Reads 'new' jobs from the database, sends each through the Gemini client,
stores the score, and marks jobs as 'queued' (above threshold) or 'skipped'.

Pre-filters run first and auto-skip without an LLM call (saves quota): jobs outside
the target countries, working-student and above-senior titles, and jobs with no AI/ML
keyword in the title and no specific AI/ML term in the description.
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

# Source of jobs Claude added through the MCP server (pasted, found on the web, from job alerts)
MCP_SOURCE = "mcp"

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
    "research scientist", "ai researcher", "künstliche intelligenz", "künstlicher intelligenz",
    "forward deployed", "ml", "research engineer", "perception", "agentic",
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
    "ai", "ai/ml", "ml/ai", "cv/nlp", "nlp/cv", "dl", "ki",
}

# Too generic to signal an AI/ML role when they only appear in the description
# (most postings now mention "AI" in passing); they still count in the title.
_TITLE_ONLY_KEYWORDS = {
    "ai", "dl", "ki", "pandas", "numpy", "scipy", "matplotlib", "airflow", "databricks", "forward deployed",
    "ml", "research engineer", "perception",
}


def _keyword_pattern(keywords: set[str]) -> re.Pattern:
    """Regex matching any keyword as a whole word (letter/digit boundaries, so "ai/ml" still works)."""
    return re.compile(
        r"(?<![a-z0-9])(?:"
        + "|".join(re.escape(kw) for kw in sorted(keywords, key=len, reverse=True))
        + r")(?![a-z0-9])",
        re.IGNORECASE,
    )


_AI_PATTERN = _keyword_pattern(_AI_KEYWORDS)
_AI_DESCRIPTION_PATTERN = _keyword_pattern(_AI_KEYWORDS - _TITLE_ONLY_KEYWORDS)


def has_ai_keywords(title: str, description: str) -> bool:
    """True if the title has any AI keyword or the description has a specific (non-generic) one."""
    return bool(_AI_PATTERN.search(title or "") or _AI_DESCRIPTION_PATTERN.search(description or ""))


# Titles the candidate never wants: working-student jobs and roles above senior.
# German "-leiter"/"-leitung" (Teamleiter, Projektleitung) count as lead roles.
_EXCLUDED_TITLE_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:werks?student\w*|working[\s-]student|studentische[rn]?\s+hilfskraft|hiwi"
    r"|lead|staff|principal|head\s+of|director|chief|vp|vice\s+president)(?![a-z0-9])"
    r"|(?:leiter(?:in)?|leitung)(?![a-z0-9])",
    re.IGNORECASE,
)


def is_excluded_title(title: str) -> bool:
    """True for working-student and above-senior titles, which are skipped without an LLM call."""
    return bool(_EXCLUDED_TITLE_PATTERN.search(title or ""))


def title_is_relevant(title: str) -> bool:
    """True if the title alone passes the title and keyword pre-filters; gates per-job detail requests."""
    return not is_excluded_title(title) and has_ai_keywords(title, "")


def prefilter_reason(job: dict, allowed_countries: Optional[frozenset]) -> Optional[tuple[str, str]]:
    """(skip_reason, reasoning) when a pre-filter skips the job without scoring, else None."""
    country = job.get("country")
    if allowed_countries and country != REMOTE and country not in allowed_countries:
        return "location", f"Auto-skipped: location outside target countries ({job.get('city') or country})"
    if is_excluded_title(job.get("title", "")):
        return "title_filter", "Auto-skipped: working-student or above-senior title"
    if not has_ai_keywords(job.get("title", ""), job.get("description", "")):
        return "keyword_filter", "Auto-skipped: no AI/ML keywords in title or description"
    return None


def prefilter_new_jobs(db_path: Path, allowed_countries: Optional[frozenset]) -> tuple[int, list[dict]]:
    """
    Skip every 'new' job a pre-filter rejects. Returns (skipped count, remaining 'new' jobs, newest first).
    Jobs Claude added were checked when added (or kept on purpose), so they are never skipped here.
    """
    with db.get_conn(db_path) as conn:
        jobs = [dict(r) for r in db.get_jobs_by_status(conn, "new", limit=None)]

    remaining, skipped = [], 0
    for job in jobs:
        skip = None if job.get("source") == MCP_SOURCE else prefilter_reason(job, allowed_countries)
        if skip is None:
            remaining.append(job)
            continue
        with db.get_conn(db_path) as conn:
            db.update_score(conn, job["id"], 0.0, skip[1], new_status="skipped", skip_reason=skip[0])
        skipped += 1
        log.debug("Job pre-filtered (%s) | %s @ %s", skip[0], job["title"], job["company"])
    return skipped, remaining


def apply_score(conn, job_id: str, result: ScoreResult, threshold: float) -> tuple[str, Optional[str]]:
    """Store a score and move the job on. Returns (status, skip_reason): 'queued' at or above threshold."""
    if not result.is_job_posting:
        status, skip_reason = "skipped", "not_a_job"
    elif result.score >= threshold:
        status, skip_reason = "queued", None
    else:
        status, skip_reason = "skipped", "low_score"
    db.update_score(conn, job_id, result.score, result.reasoning, new_status=status, skip_reason=skip_reason)
    return status, skip_reason


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
        Pre-filter every 'new' job, then LLM-score up to batch_size of the rest (newest first).
        Returns a summary dict:
        {scored, queued, skipped, filtered, not_a_job, deferred, errors, parse_errors, quota_hit}

        Deferred and failed jobs stay 'new' for the next run.
        """
        stats = {
            "scored": 0, "queued": 0, "skipped": 0, "filtered": 0, "not_a_job": 0,
            "deferred": 0, "errors": 0, "parse_errors": 0, "quota_hit": 0,
        }

        stats["filtered"], jobs = prefilter_new_jobs(self._db_path, self._allowed_countries)

        log.info("Scoring: %d new jobs after pre-filters, up to %d LLM calls", len(jobs), self._batch_size)

        llm_calls = 0
        llm_stopped = False   # quota exhausted or provider misconfigured

        for job in jobs:
            job_id = job["id"]

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

            with db.get_conn(self._db_path) as conn:
                status, skip_reason = apply_score(conn, job_id, result, self._threshold)

            if skip_reason == "not_a_job":
                stats["not_a_job"] += 1
                log.info("Not a job posting, skipped | %s @ %s", job["title"], job["company"])
                continue

            stats["scored"] += 1
            if status == "queued":
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
