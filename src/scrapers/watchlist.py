"""
Company watchlist: polls the public ATS job board of every watched company.

Only jobs whose title passes the scorer's title and keyword pre-filters are kept. The title alone
decides: an AI company's boilerplate puts AI keywords into every description, even an accountant's.
One company's broken board is recorded on its watchlist row and does not stop the run.
"""
from __future__ import annotations

from typing import Any

from src import database as db
from src.logger import get_logger
from src.matching.scorer import title_is_relevant
from src.scrapers.ats import fetch_board
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)


class WatchlistScraper(BaseScraper):
    source_name = "ats"
    base_url = ""
    request_delay = 0.5

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        # `query` and `location` are ignored: every job on a watched board is a candidate
        with db.get_conn(self._db_path) as conn:
            companies = [dict(r) for r in db.list_watched_companies(conn)]
        jobs: list[dict[str, Any]] = []
        failures = 0
        for company in companies:
            try:
                board = fetch_board(self._get, company["ats"], company["slug"])
            except Exception as exc:
                failures += 1
                log.warning("[ats] %s (%s/%s) failed: %s", company["name"], company["ats"], company["slug"], exc)
                self._record(company["id"], None, None, f"{type(exc).__name__}: {exc}"[:500])
                continue
            relevant = [j for j in board if title_is_relevant(j["title"])]
            for job in relevant:
                job["external_id"] = f"{company['ats']}:{company['slug']}:{job['external_id']}"
                job["company"] = company["name"]
            jobs.extend(relevant)
            self._record(company["id"], len(board), len(relevant), None)
        if companies and failures == len(companies):
            raise ScraperError(f"All {failures} watched boards failed")
        log.info("[ats] %d boards, %d failed, %d relevant jobs", len(companies), failures, len(jobs))
        return jobs

    def _record(self, company_id: int, job_count, relevant_count, error) -> None:
        with db.get_conn(self._db_path) as conn:
            db.record_watch_check(conn, company_id, job_count, relevant_count, error)
