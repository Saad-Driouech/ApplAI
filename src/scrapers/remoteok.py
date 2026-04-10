"""
RemoteOK scraper — free public JSON API, no auth, remote tech jobs globally.

API: https://remoteok.com/api
Returns a JSON array; first element is a legal disclaimer object, rest are jobs.
No authentication or API key required.
Covers: remote ML/AI/Data Science roles worldwide.
"""
from __future__ import annotations

from typing import Any

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)

_API_URL = "https://remoteok.com/api"


class RemoteOKScraper(BaseScraper):
    source_name = "remoteok"
    base_url = "https://remoteok.com"
    request_delay = 1.5  # be polite — no documented rate limit

    def __init__(self, db_path, **kwargs):
        super().__init__(db_path, **kwargs)

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            resp = self._get(
                _API_URL,
                headers={
                    "Accept": "application/json",
                    # Identify the bot so RemoteOK can contact us if needed
                    "User-Agent": "ApplAI/1.0 (automated job aggregator; github.com/Saad-Driouech/applai)",
                },
            )
        except Exception as exc:
            raise ScraperError(f"RemoteOK request failed: {exc}") from exc

        try:
            data = resp.json()
        except Exception as exc:
            raise ScraperError(f"RemoteOK JSON parse failed: {exc}") from exc

        # Skip the first element — it's a legal disclaimer dict, not a job
        jobs = [item for item in data if isinstance(item, dict) and "id" in item]

        results = []
        for item in jobs:
            try:
                results.append(self._map_item(item))
            except Exception as exc:
                log.debug("[remoteok] Skipped item: %s", exc)

        return results

    def _map_item(self, item: dict) -> dict[str, Any]:
        job_id = str(item.get("id", ""))
        if not job_id:
            raise ValueError("Missing id")

        location = item.get("location", "") or ""
        country = self._infer_country(location)

        # RemoteOK uses "position" for job title
        title = item.get("position", "") or item.get("title", "")

        salary_min = item.get("salary_min") or 0
        salary_max = item.get("salary_max") or 0
        if salary_min and salary_max and salary_max > salary_min:
            salary_info = f"{salary_min:.0f}–{salary_max:.0f}"
        elif salary_min:
            salary_info = f"from {salary_min:.0f}"
        else:
            salary_info = ""

        return {
            "external_id": job_id,
            "title": title,
            "company": item.get("company", ""),
            "city": "Remote",
            "country": country,
            "salary_info": salary_info,
            "source_url": item.get("url", ""),
            "description": item.get("description", ""),
            "posted_at": item.get("date", ""),
        }

    @staticmethod
    def _infer_country(location: str) -> str:
        """Best-effort country tag from RemoteOK's free-text location field."""
        loc = location.lower()
        if any(k in loc for k in ("germany", "deutschland", "europe", "eu")):
            return "DE"
        if any(k in loc for k in ("uae", "dubai", "emirates")):
            return "AE"
        if any(k in loc for k in ("saudi", "riyadh", "ksa")):
            return "SA"
        if any(k in loc for k in ("netherlands", "amsterdam", "nl")):
            return "NL"
        if any(k in loc for k in ("switzerland", "zurich", "ch")):
            return "CH"
        # Worldwide / anywhere / empty → default DE
        return "DE"
