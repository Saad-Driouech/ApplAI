"""
Adzuna scraper — REST API with AI/ML job search across multiple countries.

API docs: https://developer.adzuna.com/docs/search
Requires free API credentials (app_id + app_key): https://developer.adzuna.com/
Free tier: 250 requests/day.
Supports: DE, AE, NL, CH (and 15+ more countries).
"""
from __future__ import annotations

from typing import Any

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)

_API_BASE = "https://api.adzuna.com/v1/api/jobs"

# Adzuna lowercase country codes → internal ISO codes
# Only include countries in our allowed_countries config
_COUNTRIES = {
    "de": "DE",
    "ae": "AE",
    "nl": "NL",
    "ch": "CH",
}

# Adzuna supports simple keyword queries, not boolean OR.
# Use a targeted phrase that covers the most relevant roles.
# The AI keyword pre-filter in the scorer handles further relevance filtering.
_SEARCH_QUERY = "machine learning AI data science"


class AdzunaScraper(BaseScraper):
    source_name = "adzuna"
    base_url = "https://www.adzuna.com"
    request_delay = 1.0

    def __init__(self, db_path, app_id: str, app_key: str, **kwargs):
        super().__init__(db_path, **kwargs)
        self._app_id = app_id
        self._app_key = app_key

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        results = []
        for adzuna_code, internal_code in _COUNTRIES.items():
            try:
                jobs = self._fetch_country(adzuna_code, internal_code)
                results.extend(jobs)
                log.info("[adzuna] %s: %d jobs fetched", adzuna_code.upper(), len(jobs))
            except Exception as exc:
                log.warning("[adzuna] Country %s failed: %s", adzuna_code.upper(), exc)
        return results

    def _fetch_country(self, adzuna_code: str, internal_code: str) -> list[dict[str, Any]]:
        url = f"{_API_BASE}/{adzuna_code}/search/1"
        try:
            resp = self._get(
                url,
                params={
                    "app_id": self._app_id,
                    "app_key": self._app_key,
                    "results_per_page": 50,
                    "what": _SEARCH_QUERY,
                    "content-type": "application/json",
                },
            )
        except Exception as exc:
            raise ScraperError(f"Adzuna {adzuna_code} request failed: {exc}") from exc

        try:
            data = resp.json()
        except Exception as exc:
            raise ScraperError(f"Adzuna {adzuna_code} JSON parse failed: {exc}") from exc

        jobs = data.get("results", [])
        results = []
        for item in jobs:
            try:
                results.append(self._map_item(item, internal_code))
            except Exception as exc:
                log.debug("[adzuna] Skipped item: %s", exc)
        return results

    def _map_item(self, item: dict, country: str) -> dict[str, Any]:
        job_id = str(item.get("id", ""))
        if not job_id:
            raise ValueError("Missing id")

        # Location: nested dict with display_name like "Berlin, Germany"
        location_obj = item.get("location") or {}
        location_display = location_obj.get("display_name", "") if isinstance(location_obj, dict) else ""
        city = location_display.split(",")[0].strip() if location_display else ""

        # Company: nested dict with display_name
        company_obj = item.get("company") or {}
        company = company_obj.get("display_name", "") if isinstance(company_obj, dict) else ""

        # Salary range (floats)
        salary_min = item.get("salary_min")
        salary_max = item.get("salary_max")
        if salary_min and salary_max:
            salary_info = f"{salary_min:.0f}–{salary_max:.0f}"
        elif salary_min:
            salary_info = f"from {salary_min:.0f}"
        else:
            salary_info = ""

        return {
            "external_id": job_id,
            "title": item.get("title", ""),
            "company": company,
            "city": city,
            "country": country,
            "salary_info": salary_info,
            "source_url": item.get("redirect_url", ""),
            "description": item.get("description", ""),
            "posted_at": item.get("created", ""),
        }
