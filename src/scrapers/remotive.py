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
        try:
            resp = self._get(
                _API_URL,
                params={
                    "category": category,
                    "search": query,
                    "limit": 50,
                },
                headers={"Accept": "application/json"},
            )
        except Exception as exc:
            raise ScraperError(f"Remotive request failed: {exc}") from exc

        try:
            import json
            text = resp.content.decode(resp.encoding or "utf-8", errors="replace")
            data = json.loads(text)
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

        # Remotive is remote-only — use "REMOTE" as country placeholder
        # but tag with "DE" if the company is Europe-based (best effort)
        candidate_region = item.get("candidate_required_location", "")
        country = self._infer_country(candidate_region)

        return {
            "external_id": job_id,
            "title": item.get("title", ""),
            "company": item.get("company_name", ""),
            "city": "Remote",
            "country": country,
            "salary_info": item.get("salary", ""),
            "source_url": item.get("url", ""),
            "description": item.get("description", ""),
            "posted_at": item.get("publication_date", ""),
        }

    @staticmethod
    def _infer_country(region: str) -> str:
        """Best-effort country tag from Remotive's free-text region field."""
        region_lower = region.lower()
        if any(k in region_lower for k in ("germany", "deutschland", "de", "europe", "eu")):
            return "DE"
        if any(k in region_lower for k in ("uae", "dubai", "emirates")):
            return "AE"
        if any(k in region_lower for k in ("saudi", "riyadh", "ksa")):
            return "SA"
        if "worldwide" in region_lower or "anywhere" in region_lower or not region:
            return "DE"   # default to DE for worldwide remote
        return "DE"
