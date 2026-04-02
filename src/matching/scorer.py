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
from src.matching.gemini_client import BudgetExceeded, ScoreResult
from src.utils.jd_sanitizer import sanitize_jd

log = get_logger(__name__)

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
    "ai/ml", "ml/ai", "ai ", " ai,", " ai.", "(ai)",
    "cv/nlp", "nlp/cv", "dl ", " dl,", " dl.",
}

# Pre-compile a single regex for speed: match any keyword as a whole word
_AI_PATTERN = re.compile(
    "|".join(re.escape(kw) for kw in sorted(_AI_KEYWORDS, key=len, reverse=True)),
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
    ):
        self._db_path = db_path
        self._client = client
        self._cv_summary = cv_summary
        self._threshold = score_threshold
        self._batch_size = batch_size
        self._user_preferences = user_preferences

    def run(self) -> dict[str, int]:
        """
        Score all 'new' jobs in the database.
        Returns a summary dict: {scored, queued, skipped, filtered, errors, quota_hit}
        """
        stats = {"scored": 0, "queued": 0, "skipped": 0, "filtered": 0, "errors": 0, "quota_hit": 0}

        with db.get_conn(self._db_path) as conn:
            jobs = db.get_jobs_by_status(conn, "new", limit=self._batch_size)

        log.info("Scoring batch: %d jobs to evaluate", len(jobs))

        for row in jobs:
            job = dict(row)
            job_id = job["id"]

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
