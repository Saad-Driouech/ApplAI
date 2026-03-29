"""
Glassdoor scraper.

Glassdoor's public job search returns results via a JSON API endpoint that
their web app calls.  We mimic that call.  The endpoint is undocumented and
may change; if it does, see _FALLBACK_URL for the HTML fallback.

Rate limit: 3 s courtesy delay.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)

# Glassdoor country codes differ from ISO 3166-1
_GD_COUNTRY_IDS: dict[str, str] = {
    "DE": "de",
    "SA": "sa",
    "AE": "ae",
    "CH": "ch",
    "QA": "qa",
    "NL": "nl",
}

_API_URL = "https://www.glassdoor.com/graph"
_JOBS_URL = "https://www.glassdoor.com/Job/jobs.htm"


class GlassdoorScraper(BaseScraper):
    source_name = "glassdoor"
    base_url = "https://www.glassdoor.com"
    request_delay = 3.0

    def __init__(self, db_path, country: str = "DE", **kwargs):
        super().__init__(db_path, **kwargs)
        self._country = country.upper()

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        """
        Glassdoor's jobs page returns JSON when the Accept header includes
        'application/json'.  We request that and parse the results.
        """
        params = {
            "sc.keyword": query,
            "locT": "C",          # City
            "locId": "",          # leave blank — Glassdoor resolves from keyword
            "locKeyword": location,
            "jobType": "",
            "fromAge": kwargs.get("days_old", 7),
            "minSalary": 0,
            "includeNoSalaryJobs": "true",
            "radius": 25,
            "cityId": -1,
            "minRating": 0.0,
            "industryId": -1,
            "sgocId": -1,
            "seniorityType": "all",
            "companyId": -1,
            "employerSizes": 0,
            "applicationType": 0,
        }

        try:
            resp = self._get(
                _JOBS_URL,
                params=params,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://www.glassdoor.com/",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        except Exception as exc:
            raise ScraperError(f"Glassdoor request failed: {exc}") from exc

        try:
            data = resp.json()
        except Exception:
            log.warning("[glassdoor] Response is not JSON; trying HTML parse")
            return self._parse_html(resp.text)

        return self._map_results(data)

    def _map_results(self, data: dict) -> list[dict[str, Any]]:
        listings = []
        jobs = data.get("jobListings", data.get("data", {}).get("jobListings", []))
        if isinstance(jobs, dict):
            jobs = jobs.get("jobListings", [])

        for item in jobs:
            try:
                listings.append(self._map_item(item))
            except Exception as exc:
                log.debug("[glassdoor] Skipped item: %s", exc)

        return listings

    def _map_item(self, item: dict) -> dict[str, Any]:
        job = item.get("jobListing", item)
        job_id = str(job.get("jobListingId", ""))
        if not job_id:
            raise ValueError("Missing jobListingId")

        url = f"https://www.glassdoor.com/job-listing/j?jl={job_id}"
        employer = job.get("employer", {})

        return {
            "external_id": job_id,
            "title": job.get("jobTitleText", ""),
            "company": employer.get("name", job.get("employerName", "")),
            "city": job.get("locationName", ""),
            "country": self._country,
            "salary_info": job.get("salarySource", {}).get("adjustedSalary", ""),
            "source_url": url,
            "description": job.get("jobDescriptionText", ""),
            "posted_at": job.get("listingDateText", ""),
        }

    def _parse_html(self, html: str) -> list[dict[str, Any]]:
        """HTML fallback — parses Glassdoor job cards from the page source."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            log.error("[glassdoor] beautifulsoup4 not installed")
            return []

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("li[data-id]")
        results = []

        for card in cards:
            jl_id = card.get("data-id", "")
            if not jl_id:
                continue
            title_el = card.select_one("[data-test='job-title']")
            company_el = card.select_one("[data-test='employer-name']")
            location_el = card.select_one("[data-test='emp-location']")

            results.append({
                "external_id": jl_id,
                "title": title_el.get_text(strip=True) if title_el else "",
                "company": company_el.get_text(strip=True) if company_el else "",
                "city": location_el.get_text(strip=True) if location_el else "",
                "country": self._country,
                "source_url": f"https://www.glassdoor.com/job-listing/j?jl={jl_id}",
                "description": "",
            })

        return results
