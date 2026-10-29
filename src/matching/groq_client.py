"""
Groq client for job scoring (Tier 1 LLM — alternative to Gemini).

Uses the official Groq Python SDK. Shares ScoreResult / BudgetExceeded /
_RateLimiter / build_score_prompt from gemini_client.py so both clients
are drop-in replacements for each other.

Free-tier limits vary by model: https://console.groq.com/settings/limits
"""
from __future__ import annotations

from pathlib import Path

from src.matching.gemini_client import (
    BudgetExceeded,
    ScoreResult,
    build_score_prompt,
    shared_limiter,
)
from src.matching.response_parser import parse_score_response
from src.logger import audit, get_logger

log = get_logger(__name__)


class GroqClient:
    """
    Thread-safe Groq client with the same interface as GeminiClient.
    Both expose score_job(job, cv_summary) → ScoreResult.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-oss-120b",
        rpm: int = 30,
        rpd: int = 14400,
        usage_db: Path | None = None,
    ):
        try:
            from groq import Groq
        except ImportError as exc:
            raise ImportError(
                "groq is not installed. Run: pip install groq"
            ) from exc

        self._model = model
        self._limiter = shared_limiter("Groq", model, rpm=rpm, rpd=rpd, usage_db=usage_db)
        self._client = Groq(api_key=api_key)

    def _call(self, prompt: str) -> str:
        self._limiter.acquire()
        # Reasoning models spend completion tokens on thinking; keep it short
        extra = {"reasoning_effort": "low"} if self._model.startswith("openai/gpt-oss") else {}
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1024,
                **extra,
            )
        except Exception as exc:
            # The SDK already retried 429s, so this means the quota is exhausted
            exc_type = type(exc).__name__
            if "RateLimitError" in exc_type or getattr(exc, "status_code", None) == 429:
                raise BudgetExceeded(str(exc)) from exc
            raise
        return response.choices[0].message.content

    def score_job(self, job: dict, cv_summary: str, user_preferences: str = "") -> ScoreResult:
        prompt = build_score_prompt(job, cv_summary, user_preferences)

        try:
            raw = self._call(prompt)
        except BudgetExceeded:
            raise
        except Exception as exc:
            log.error("Groq scoring failed for job %s: %s", job.get("id", "?"), exc)
            raise

        result = parse_score_response(raw)
        audit(
            "groq_score",
            job_id=job.get("id", "?"),
            score=result.score,
            remaining_quota=self._limiter.daily_remaining,
        )
        return result

    @property
    def daily_remaining(self) -> int:
        return self._limiter.daily_remaining
