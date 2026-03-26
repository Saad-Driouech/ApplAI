"""
Arbeitnow scraper — free, no auth, Germany-focused tech jobs.

API docs: https://www.arbeitnow.com/api
Returns JSON directly, no auth needed, no rate limit documented.
Covers: DE (primarily), some remote roles.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)

_API_URL = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowScraper(BaseScraper):
    source_name = "arbeitnow"
    base_url = "https://www.arbeitnow.com"
    request_delay = 1.0

    def __init__(self, db_path, **kwargs):
        super().__init__(db_path, **kwargs)

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        # Arbeitnow API accepts search and location as query params
        params = urlencode({
            "search": query,
            "location": location,
        })
        url = f"{_API_URL}?{params}"

        try:
            resp = self._get(url, headers={"Accept": "application/json"})
        except Exception as exc:
            raise ScraperError(f"Arbeitnow request failed: {exc}") from exc

        try:
            data = resp.json()
        except Exception as exc:
            raise ScraperError(f"Arbeitnow JSON parse failed: {exc}") from exc

        jobs = data.get("data", [])
        results = []
        for item in jobs:
            try:
                results.append(self._map_item(item))
            except Exception as exc:
                log.debug("[arbeitnow] Skipped item: %s", exc)

        return results

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
            "country": "DE",
            "salary_info": "",
            "source_url": url,
            "description": item.get("description", ""),
            "posted_at": str(item.get("created_at", "")),
        }
