"""
Remotive scraper — free public API, no auth, remote tech jobs globally.

API docs: https://remotive.com/api
Covers: remote ML/AI/Data Science roles worldwide.
Free, no authentication, no rate limit documented.
"""
from __future__ import annotations

from typing import Any

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError
from src.scrapers.location import infer_remote_country, remote_city

log = get_logger(__name__)

_API_URL = "https://remotive.com/api/remote-jobs"

# Remotive category IDs relevant to the candidate's profile
_CATEGORIES = [
    "software-dev",
    "data",
    "ai-machine-learning",
]


class RemotiveScraper(BaseScraper):
    source_name = "remotive"
    base_url = "https://remotive.com"
    request_delay = 1.0

    def __init__(self, db_path, **kwargs):
        super().__init__(db_path, **kwargs)

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        results = []
        for category in _CATEGORIES:
            try:
                jobs = self._fetch_category(query, category)
                results.extend(jobs)
            except Exception as exc:
                log.warning("[remotive] Category %s failed: %s", category, exc)

        return results

    def _fetch_category(self, query: str, category: str) -> list[dict[str, Any]]:
        # Remotive's search param doesn't support boolean OR queries and returns 0
        # results for complex strings. The category filter is sufficient — the AI
        # keyword pre-filter in the scorer handles relevance filtering downstream.
        try:
            resp = self._get(
                _API_URL,
                params={
                    "category": category,
                    "limit": 50,
                },
                headers={"Accept": "application/json"},
            )
        except Exception as exc:
            raise ScraperError(f"Remotive request failed: {exc}") from exc

        try:
            data = resp.json()
        except Exception as exc:
            raise ScraperError(f"Remotive JSON parse failed: {exc}") from exc

        jobs = data.get("jobs", [])
        results = []
        for item in jobs:
            try:
                results.append(self._map_item(item))
            except Exception as exc:
                log.debug("[remotive] Skipped item: %s", exc)
        return results

    def _map_item(self, item: dict) -> dict[str, Any]:
        job_id = str(item.get("id", ""))
        if not job_id:
            raise ValueError("Missing id")

        candidate_region = item.get("candidate_required_location", "")

        return {
            "external_id": job_id,
            "title": item.get("title", ""),
            "company": item.get("company_name", ""),
            "city": remote_city(candidate_region),
            "country": infer_remote_country(candidate_region),
            "salary_info": item.get("salary", ""),
            "source_url": item.get("url", ""),
            "description": item.get("description", ""),
            "posted_at": item.get("publication_date", ""),
        }
