"""
StepStone (Germany) scraper.

StepStone provides a public search JSON API at their jobs endpoint.
We query it directly, parsing the structured results.
Country focus: DE (primary), CH (Switzerland).
"""
from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlencode

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)

_SEARCH_URL = "https://www.stepstone.de/jobs/{keyword}/in-{location}"
_API_URL = "https://www.stepstone.de/5/results.html"

_COUNTRY_MAP = {
    "DE": "de",
    "CH": "ch",
    "AT": "at",
}


class StepStoneScraper(BaseScraper):
    source_name = "stepstone"
    base_url = "https://www.stepstone.de"
    request_delay = 2.5

    def __init__(self, db_path, country: str = "DE", **kwargs):
        super().__init__(db_path, **kwargs)
        self._country = country.upper()

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        params = {
            "what": query,
            "where": location,
            "radius": 30,
            "action": "facet_selected",
            "resultlistAction": "pagination",
            "offset": 0,
            "sort": "date",
            "ag": kwargs.get("days_old", 7),
        }

        try:
            resp = self._get(
                _API_URL,
                params=params,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://www.stepstone.de/",
                },
            )
        except Exception as exc:
            raise ScraperError(f"StepStone request failed: {exc}") from exc

        try:
            data = resp.json()
        except Exception:
            log.warning("[stepstone] Non-JSON response; trying HTML parse")
            return self._parse_html(resp.text)

        return self._map_results(data)

    def _map_results(self, data: dict) -> list[dict[str, Any]]:
        # StepStone wraps jobs in data["results"] or data["resultList"]
        jobs = data.get("results", data.get("resultList", []))
        results = []
        for item in jobs:
            try:
                results.append(self._map_item(item))
            except Exception as exc:
                log.debug("[stepstone] Skipped item: %s", exc)
        return results

    def _map_item(self, item: dict) -> dict[str, Any]:
        job_id = str(item.get("id", item.get("jobId", "")))
        if not job_id:
            raise ValueError("Missing job ID")

        url = item.get("url", f"https://www.stepstone.de/stellenangebote--{job_id}.html")
        if not url.startswith("http"):
            url = "https://www.stepstone.de" + url

        return {
            "external_id": job_id,
            "title": item.get("jobTitle", item.get("title", "")),
            "company": item.get("companyName", item.get("company", "")),
            "city": item.get("location", item.get("city", "")),
            "country": self._country,
            "salary_info": item.get("salary", ""),
            "source_url": url,
            "description": item.get("jobDescription", item.get("teaser", "")),
            "posted_at": item.get("date", item.get("publishedAt", "")),
        }

    def _parse_html(self, html: str) -> list[dict[str, Any]]:
        """HTML fallback for StepStone job cards."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            log.error("[stepstone] beautifulsoup4 not installed")
            return []

        soup = BeautifulSoup(html, "lxml")
        # StepStone uses article tags with data-id for job cards
        cards = soup.select("article[data-id]")
        results = []

        for card in cards:
            job_id = card.get("data-id", "")
            if not job_id:
                continue
            title_el = card.select_one("h2[data-genesis-element='BASE_JOB_TITLE']")
            company_el = card.select_one("[data-genesis-element='COMPANY_NAME']")
            location_el = card.select_one("[data-genesis-element='LOCATION']")
            link_el = card.select_one("a[href]")

            url = link_el["href"] if link_el else ""
            if url and not url.startswith("http"):
                url = "https://www.stepstone.de" + url

            results.append({
                "external_id": job_id,
                "title": title_el.get_text(strip=True) if title_el else "",
                "company": company_el.get_text(strip=True) if company_el else "",
                "city": location_el.get_text(strip=True) if location_el else "",
                "country": self._country,
                "source_url": url or f"https://www.stepstone.de/stellenangebote--{job_id}.html",
                "description": "",
            })

        return results
