"""
Abstract base scraper with rate-limiting, retry, and dedup logic.

All concrete scrapers inherit from BaseScraper and implement `_fetch_jobs()`.
The `run()` method handles the full pipeline:
  fetch → sanitize → dedup → persist → return new jobs
"""
from __future__ import annotations

import hashlib
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src import database as db
from src.logger import audit, get_logger
from src.utils.sanitize import sanitize_job

log = get_logger(__name__)

# Shared HTTP client settings
_DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
}

_RETRY_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


def _job_id(source: str, external_id: str) -> str:
    """Deterministic primary key from source + external job ID."""
    raw = f"{source}:{external_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


class ScraperError(Exception):
    """Raised when a scraper encounters a non-retryable error."""


class BaseScraper(ABC):
    """
    Abstract base for all job board scrapers.

    Subclasses must set:
      - `source_name`: str  — unique identifier, e.g. "indeed"
      - `base_url`: str     — root URL of the job board

    Subclasses must implement:
      - `_fetch_jobs(query, location, **kwargs) -> list[dict]`
        Return raw dicts; the base class handles sanitisation and persistence.
    """

    source_name: str = ""
    base_url: str = ""

    # Courtesy delay between requests in seconds (overridable per scraper)
    request_delay: float = 2.0

    def __init__(
        self,
        db_path,                            # pathlib.Path — injected from config
        timeout: float = 20.0,
        max_retries: int = 3,
    ):
        if not self.source_name:
            raise NotImplementedError("Subclasses must set `source_name`")

        self._db_path = db_path
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: Optional[httpx.Client] = None

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers=_DEFAULT_HEADERS,
                timeout=self._timeout,
                follow_redirects=True,
            )
        return self._client

    @retry(
        retry=retry_if_exception_type(_RETRY_EXCEPTIONS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET with automatic retry on transient network errors."""
        log.debug("GET %s", url)
        resp = self._get_client().get(url, **kwargs)
        resp.raise_for_status()
        time.sleep(self.request_delay)
        return resp

    def _close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    # ── Abstract interface ────────────────────────────────────────────────

    @abstractmethod
    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        """
        Scrape raw job listings.

        Each returned dict MUST contain at minimum:
          - external_id: str   (board-specific job ID or URL hash)
          - title: str
          - company: str
          - country: str       (ISO 3166-1 alpha-2)
          - source_url: str    (direct link to the job posting)

        Optional fields: city, salary_info, description, posted_at
        """

    # ── Public pipeline ───────────────────────────────────────────────────

    def run(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        """
        Full scrape → sanitize → dedup → persist pipeline.
        Returns only newly inserted jobs (not duplicates).
        """
        log.info("[%s] Starting scrape | query=%r location=%r", self.source_name, query, location)

        with db.get_conn(self._db_path) as conn:
            run_id = db.start_scrape_run(conn, self.source_name)

        jobs_found = 0
        jobs_new = 0
        error_msg = None
        new_jobs: list[dict[str, Any]] = []

        try:
            raw_jobs = self._fetch_jobs(query, location, **kwargs)
            jobs_found = len(raw_jobs)

            with db.get_conn(self._db_path) as conn:
                for raw in raw_jobs:
                    try:
                        sanitized = sanitize_job(raw)
                        sanitized["source"] = self.source_name
                        sanitized["id"] = _job_id(self.source_name, sanitized["external_id"])

                        inserted = db.upsert_job(conn, sanitized)
                        if inserted:
                            jobs_new += 1
                            new_jobs.append(sanitized)
                    except Exception as exc:
                        log.warning("[%s] Skipped malformed job record: %s", self.source_name, exc)

        except Exception as exc:
            error_msg = str(exc)
            log.error("[%s] Scrape failed: %s", self.source_name, exc, exc_info=True)
        finally:
            with db.get_conn(self._db_path) as conn:
                db.finish_scrape_run(conn, run_id, jobs_found, jobs_new, error_msg)
            self._close()

        audit(
            "scrape_complete",
            source=self.source_name,
            jobs_found=jobs_found,
            jobs_new=jobs_new,
            error=error_msg or "none",
        )
        log.info(
            "[%s] Done | found=%d new=%d",
            self.source_name, jobs_found, jobs_new,
        )
        return new_jobs
