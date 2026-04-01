"""
Gemini 2.5 Flash wrapper for job scoring (Tier 1 LLM).

Responsibilities:
  - Manage a rate-limited Gemini client (RPM + RPD budgets)
  - Expose a single `score_job(job_text, cv_summary)` function
  - Return a structured ScoreResult (score 0-10 + reasoning)
  - Fall back to Ollama if configured and Gemini quota is exhausted

Budget tracking is in-memory; for multi-process deployments, use Redis instead.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.logger import audit, get_logger

log = get_logger(__name__)

_SCORE_PROMPT_TEMPLATE = """\
You are a job-fit evaluator for an AI/ML professional. Score how well the candidate fits this job.

## Candidate Profile
{cv_summary}

## Job Posting
Title: {title}
Company: {company}
Country: {country}
Description:
{description}

## Scoring Instructions
Return ONLY a JSON object with exactly these keys:
- "score": float between 0.0 and 10.0
- "reasoning": string of max 150 words explaining the score
- "must_haves_met": list of strings (key requirements the candidate satisfies)
- "gaps": list of strings (important missing skills or experience)

## Scoring Rubric
  9-10: Near-perfect match — core stack, seniority, and domain all align
  7-9:  Strong fit — meets most requirements, minor gaps are learnable
  5-7:  Decent fit — relevant AI/ML background, some requirements unmet
  3-5:  Weak fit — tangential overlap, significant gaps
  0-3:  No fit — completely different domain or seniority

## Scoring Guidelines
- AI/ML roles where the candidate has relevant experience should start at 5.0 minimum, \
even if not every listed requirement is met. Transferable ML skills matter.
- Weigh core technical skills (Python, ML frameworks, model development) more than \
nice-to-haves (specific cloud certifications, niche domain knowledge).
- Years-of-experience requirements are soft guidelines, not hard cutoffs. \
Strong demonstrated skills can compensate for fewer years.
- Visa/work-authorization mismatches are a real blocker — penalize heavily.
- "Preferred" or "nice to have" requirements should NOT lower the score significantly.
- Return raw JSON, no markdown fences.
"""


@dataclass
class ScoreResult:
    score: float
    reasoning: str
    must_haves_met: list[str]
    gaps: list[str]
    raw_response: str


class BudgetExceeded(Exception):
    """Raised when daily or per-minute quota is exhausted."""


class _RateLimiter:
    """Token-bucket style limiter for RPM and RPD."""

    def __init__(self, rpm: int, rpd: int):
        self._rpm = rpm
        self._rpd = rpd
        self._minute_count = 0
        self._day_count = 0
        self._minute_reset = time.time() + 60
        self._day_reset = time.time() + 86400
        self._lock = Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.time()
            if now >= self._minute_reset:
                self._minute_count = 0
                self._minute_reset = now + 60
            if now >= self._day_reset:
                self._day_count = 0
                self._day_reset = now + 86400

            if self._day_count >= self._rpd:
                raise BudgetExceeded(
                    f"Daily Gemini quota exhausted ({self._rpd} requests/day)"
                )
            if self._minute_count >= self._rpm:
                sleep_for = self._minute_reset - now
                log.info("Gemini RPM limit reached; sleeping %.1fs", sleep_for)
                time.sleep(sleep_for + 0.1)
                self._minute_count = 0
                self._minute_reset = time.time() + 60

            self._minute_count += 1
            self._day_count += 1

    @property
    def daily_remaining(self) -> int:
        with self._lock:
            return max(0, self._rpd - self._day_count)


class GeminiClient:
    """
    Thread-safe Gemini Flash client with budget management.
    Instantiate once and reuse across the application.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        rpm: int = 15,
        rpd: int = 500,
    ):
        self._model = model
        self._limiter = _RateLimiter(rpm=rpm, rpd=rpd)
        self._client = self._build_client(api_key)

    @staticmethod
    def _build_client(api_key: str):
        try:
            from google import genai
        except ImportError as exc:
            raise ImportError(
                "google-genai is not installed. "
                "Run: pip install google-genai"
            ) from exc
        return genai.Client(api_key=api_key)

    @retry(
        retry=retry_if_exception_type(Exception),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        reraise=True,
    )
    def _call(self, prompt: str) -> str:
        from google.genai import types
        self._limiter.acquire()
        response = self._client.models.generate_content(
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=512,
            ),
        )
        return response.text

    def score_job(
        self,
        job: dict,
        cv_summary: str,
    ) -> ScoreResult:
        """
        Score a job against the candidate's CV.

        Args:
            job: dict with keys: title, company, country, description
            cv_summary: concise text summary of the candidate's profile

        Returns:
            ScoreResult with score (0-10), reasoning, must_haves_met, gaps
        """
        prompt = _SCORE_PROMPT_TEMPLATE.format(
            cv_summary=cv_summary,
            title=job.get("title", ""),
            company=job.get("company", ""),
            country=job.get("country", ""),
            description=(job.get("description") or "")[:3000],  # cap at 3k chars
        )

        try:
            raw = self._call(prompt)
        except BudgetExceeded:
            raise
        except Exception as exc:
            log.error("Gemini scoring failed for job %s: %s", job.get("id", "?"), exc)
            raise

        result = self._parse_response(raw)
        audit(
            "gemini_score",
            job_id=job.get("id", "?"),
            score=result.score,
            remaining_quota=self._limiter.daily_remaining,
        )
        return result

    @staticmethod
    def _parse_response(raw: str) -> ScoreResult:
        # Strip markdown fences if the model wrapped its output
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            log.warning("Gemini returned non-JSON; attempting regex extraction. Error: %s", exc)
            # Last-resort: extract a score with regex
            score_match = re.search(r'"score"\s*:\s*([0-9.]+)', cleaned)
            score = float(score_match.group(1)) if score_match else 0.0
            return ScoreResult(
                score=score,
                reasoning="Parse error — see raw_response",
                must_haves_met=[],
                gaps=[],
                raw_response=raw,
            )

        return ScoreResult(
            score=float(data.get("score", 0.0)),
            reasoning=str(data.get("reasoning", "")),
            must_haves_met=list(data.get("must_haves_met", [])),
            gaps=list(data.get("gaps", [])),
            raw_response=raw,
        )

    @property
    def daily_remaining(self) -> int:
        return self._limiter.daily_remaining
