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

import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

import src.database as db
from src.logger import audit, get_logger
# Re-exported: other clients and the scorer import these from here
from src.matching.response_parser import ScoreParseError, ScoreResult, parse_score_response

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
Return ONLY a JSON object with exactly these keys, in this order:
- "is_job_posting": boolean, false if this is not an actual job vacancy (e.g. a product, article, or advertisement)
- "score": float between 0.0 and 10.0
- "reasoning": string of max 100 words explaining the score
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
{user_preferences}"""


class BudgetExceeded(Exception):
    """Raised when daily or per-minute quota is exhausted."""


# Calendar day each provider's daily quota resets on
_QUOTA_TIMEZONES = {"Gemini": "America/Los_Angeles"}


class _RateLimiter:
    """RPM in memory; RPD per provider calendar day, persisted in SQLite when usage_db is set."""

    def __init__(self, rpm: int, rpd: int, name: str = "Gemini", model: str = "", usage_db: Path | None = None):
        self._name = name
        self._model = model
        self._rpm = rpm
        self._rpd = rpd
        self._usage_db = usage_db
        self._tz = ZoneInfo(_QUOTA_TIMEZONES.get(name, "UTC"))
        self._minute_count = 0
        self._minute_reset = time.time() + 60
        self._day_counts: dict[str, int] = {}  # used when usage_db is None
        self._lock = Lock()

    def _today(self) -> str:
        return datetime.now(self._tz).date().isoformat()

    def _used_today(self) -> int:
        if self._usage_db is None:
            return self._day_counts.get(self._today(), 0)
        with db.get_conn(self._usage_db) as conn:
            return db.get_llm_usage(conn, self._name, self._model, self._today())

    def _record_request(self) -> None:
        day = self._today()
        if self._usage_db is None:
            self._day_counts = {day: self._day_counts.get(day, 0) + 1}
            return
        with db.get_conn(self._usage_db) as conn:
            db.increment_llm_usage(conn, self._name, self._model, day)

    def acquire(self) -> None:
        with self._lock:
            now = time.time()
            if now >= self._minute_reset:
                self._minute_count = 0
                self._minute_reset = now + 60

            if self._used_today() >= self._rpd:
                raise BudgetExceeded(
                    f"Daily {self._name} quota exhausted ({self._rpd} requests/day)"
                )
            if self._minute_count >= self._rpm:
                sleep_for = self._minute_reset - now
                log.info("%s RPM limit reached; sleeping %.1fs", self._name, sleep_for)
                time.sleep(sleep_for + 0.1)
                self._minute_count = 0
                self._minute_reset = time.time() + 60

            self._minute_count += 1
            self._record_request()

    @property
    def daily_remaining(self) -> int:
        with self._lock:
            return max(0, self._rpd - self._used_today())


# Process-wide limiters, so the per-minute budget survives clients being rebuilt per run
_SHARED_LIMITERS: dict[tuple[str, str], _RateLimiter] = {}
_SHARED_LIMITERS_LOCK = Lock()


def shared_limiter(provider: str, model: str, rpm: int, rpd: int, usage_db: Path | None = None) -> _RateLimiter:
    with _SHARED_LIMITERS_LOCK:
        key = (provider, model)
        if key not in _SHARED_LIMITERS:
            _SHARED_LIMITERS[key] = _RateLimiter(rpm=rpm, rpd=rpd, name=provider, model=model, usage_db=usage_db)
        return _SHARED_LIMITERS[key]


_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def http_status(exc: BaseException) -> int | None:
    """HTTP status of a provider SDK error (google-genai uses .code, others .status_code)."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _is_transient(exc: BaseException) -> bool:
    if http_status(exc) in _TRANSIENT_STATUS:
        return True
    import httpx
    return isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError))


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
        usage_db: Path | None = None,
    ):
        self._model = model
        self._limiter = shared_limiter("Gemini", model, rpm=rpm, rpd=rpd, usage_db=usage_db)
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

    def _call(self, prompt: str) -> str:
        """Budget check, then the API call. A 429 that outlasts retries raises BudgetExceeded."""
        self._limiter.acquire()
        try:
            return self._generate(prompt)
        except Exception as exc:
            if http_status(exc) == 429:
                raise BudgetExceeded(f"Gemini quota exhausted (HTTP 429): {exc}") from exc
            raise

    @retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=5, max=60),
        reraise=True,
    )
    def _generate(self, prompt: str) -> str:
        from google.genai import types
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
        user_preferences: str = "",
    ) -> ScoreResult:
        """
        Score a job against the candidate's CV.

        Args:
            job: dict with keys: title, company, country, description
            cv_summary: concise text summary of the candidate's profile
            user_preferences: optional learned preference context to inject

        Returns:
            ScoreResult with score (0-10), reasoning, must_haves_met, gaps
        """
        prompt = _SCORE_PROMPT_TEMPLATE.format(
            cv_summary=cv_summary,
            title=job.get("title", ""),
            company=job.get("company", ""),
            country=job.get("country", ""),
            description=(job.get("description") or "")[:3000],  # cap at 3k chars
            user_preferences=user_preferences,
        )

        try:
            raw = self._call(prompt)
        except BudgetExceeded:
            raise
        except Exception as exc:
            log.error("Gemini scoring failed for job %s: %s", job.get("id", "?"), exc)
            raise

        result = parse_score_response(raw)
        audit(
            "gemini_score",
            job_id=job.get("id", "?"),
            score=result.score,
            remaining_quota=self._limiter.daily_remaining,
        )
        return result

    @property
    def daily_remaining(self) -> int:
        return self._limiter.daily_remaining
