"""
Arbeitnow scraper — free, no auth, Germany-focused tech jobs.

API docs: https://www.arbeitnow.com/api
Returns JSON directly, no auth needed, no rate limit documented.
Covers: DE (primarily), some remote roles.

The API has no search: it returns the newest jobs of every kind, 300 per page
(about two days), so relevance is left to the scorer's keyword pre-filter.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError
from src.scrapers.location import infer_onsite_country

log = get_logger(__name__)

_API_URL = "https://www.arbeitnow.com/api/job-board-api"


def _iso_from_epoch(value) -> str:
    """Arbeitnow sends Unix timestamps; store ISO-8601 like the other sources."""
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return ""


class ArbeitnowScraper(BaseScraper):
    source_name = "arbeitnow"
    base_url = "https://www.arbeitnow.com"
    request_delay = 1.0

    def __init__(self, db_path, max_pages: int = 3, **kwargs):
        super().__init__(db_path, **kwargs)
        self._max_pages = max_pages

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        # `query` and `location` are ignored: the API returns every job regardless
        results: dict[str, dict[str, Any]] = {}
        for page in range(1, self._max_pages + 1):
            try:
                resp = self._get(_API_URL, params={"page": page}, headers={"Accept": "application/json"})
            except Exception as exc:
                raise ScraperError(f"Arbeitnow request failed: {exc}") from exc

            try:
                data = resp.json()
            except Exception as exc:
                raise ScraperError(f"Arbeitnow JSON parse failed: {exc}") from exc

            for item in data.get("data", []):
                try:
                    job = self._map_item(item)
                except Exception as exc:
                    log.debug("[arbeitnow] Skipped item: %s", exc)
                    continue
                # Pages overlap, so the same job can appear twice
                results.setdefault(job["external_id"], job)

            if not (data.get("links") or {}).get("next"):
                break

        return list(results.values())

    def _map_item(self, item: dict) -> dict[str, Any]:
        slug = item.get("slug", "")
        if not slug:
            raise ValueError("Missing slug")

        url = f"https://www.arbeitnow.com/jobs/{slug}"

        return {
            "external_id": slug,
            "title": item.get("title", ""),
            "company": item.get("company_name", ""),
            "city": item.get("location", ""),
            "country": infer_onsite_country(item.get("location")),
            "salary_info": "",
            "source_url": url,
            "description": item.get("description", ""),
            "posted_at": _iso_from_epoch(item.get("created_at")),
        }
