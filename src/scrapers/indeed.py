"""
Indeed scraper.

Indeed does not offer a public API, so this scraper parses the HTML search
results page.  It sends a single GET per search and extracts job cards from
the JSON blob that Indeed embeds in the page source (<script id="mosaic-data">).

Rate limit: 2 s courtesy delay between requests (set in base).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)

# Regex to extract the embedded JSON blob from Indeed's HTML
_MOSAIC_RE = re.compile(
    r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});',
    re.DOTALL,
)

# Country-to-subdomain mapping for supported countries
_COUNTRY_DOMAINS: dict[str, str] = {
    "DE": "de.indeed.com",
    "SA": "sa.indeed.com",
    "AE": "ae.indeed.com",
    "CH": "ch.indeed.com",
    "QA": "qa.indeed.com",
    "NL": "nl.indeed.com",
}


class IndeedScraper(BaseScraper):
    source_name = "indeed"
    base_url = "https://indeed.com"
    request_delay = 3.0        # Indeed is aggressive about scraping; be polite

    def __init__(self, db_path, country: str = "DE", **kwargs):
        super().__init__(db_path, **kwargs)
        self._country = country.upper()
        domain = _COUNTRY_DOMAINS.get(self._country, "indeed.com")
        self._search_url = f"https://{domain}/jobs"

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        params = {
            "q": query,
            "l": location,
            "sort": "date",
            "fromage": kwargs.get("days_old", 7),  # only recent jobs
        }

        try:
            resp = self._get(self._search_url, params=params)
        except Exception as exc:
            raise ScraperError(f"Indeed request failed: {exc}") from exc

        return self._parse(resp.text)

    def _parse(self, html: str) -> list[dict[str, Any]]:
        match = _MOSAIC_RE.search(html)
        if not match:
            # Fallback: try plain HTML card parsing
            log.warning("[indeed] mosaic-provider-jobcards JSON not found; trying HTML fallback")
            return self._parse_html_cards(html)

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            log.error("[indeed] Failed to parse embedded JSON: %s", exc)
            return []

        jobs_list = (
            data.get("metaData", {})
            .get("mosaicProviderJobCardsModel", {})
            .get("results", [])
        )

        results = []
        for item in jobs_list:
            try:
                results.append(self._map_item(item))
            except Exception as exc:
                log.debug("[indeed] Skipped item, mapping error: %s", exc)

        return results

    def _map_item(self, item: dict) -> dict[str, Any]:
        job_key = item.get("jobkey", "")
        if not job_key:
            raise ValueError("Missing jobkey")

        domain = _COUNTRY_DOMAINS.get(self._country, "indeed.com")
        url = f"https://{domain}/viewjob?jk={job_key}"

        return {
            "external_id": job_key,
            "title": item.get("normTitle") or item.get("title", ""),
            "company": item.get("company", ""),
            "city": item.get("jobLocationCity", ""),
            "country": self._country,
            "salary_info": item.get("salarySnippet", {}).get("text", ""),
            "source_url": url,
            "description": item.get("snippet", ""),
            "posted_at": item.get("pubDate", ""),
        }

    def _parse_html_cards(self, html: str) -> list[dict[str, Any]]:
        """Minimal HTML fallback when the JSON blob isn't present."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            log.error("[indeed] beautifulsoup4 not installed; HTML fallback unavailable")
            return []

        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("div.job_seen_beacon, div[data-jk]")
        results = []

        for card in cards:
            jk = card.get("data-jk") or ""
            if not jk:
                continue
            title_el = card.select_one("h2.jobTitle span")
            company_el = card.select_one("span.companyName")
            location_el = card.select_one("div.companyLocation")

            domain = _COUNTRY_DOMAINS.get(self._country, "indeed.com")
            results.append({
                "external_id": jk,
                "title": title_el.get_text(strip=True) if title_el else "",
                "company": company_el.get_text(strip=True) if company_el else "",
                "city": location_el.get_text(strip=True) if location_el else "",
                "country": self._country,
                "source_url": f"https://{domain}/viewjob?jk={jk}",
                "description": "",
            })

        return results
