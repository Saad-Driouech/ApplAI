"""
LinkedIn Jobs RSS scraper — no auth needed, high quality, global.

LinkedIn exposes public RSS feeds for job searches.
URL format: https://www.linkedin.com/jobs/search/?keywords=...&location=...&f_TPR=r604800

We request the RSS feed version by adding &format=rss (or parsing the feed directly).
Covers: DE, AE, SA, QA, CH, NL.
"""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlencode

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)

_RSS_URL = "https://www.linkedin.com/jobs/search.rss"

_COUNTRY_CODES = {
    "DE": "de",
    "AE": "ae",
    "SA": "sa",
    "QA": "qa",
    "CH": "ch",
    "NL": "nl",
}

_LOCATION_NAMES = {
    "DE": "Germany",
    "AE": "United Arab Emirates",
    "SA": "Saudi Arabia",
    "QA": "Qatar",
    "CH": "Switzerland",
    "NL": "Netherlands",
}


class LinkedInRssScraper(BaseScraper):
    source_name = "linkedin"
    base_url = "https://www.linkedin.com"
    request_delay = 2.0

    def __init__(self, db_path, country: str = "DE", **kwargs):
        super().__init__(db_path, **kwargs)
        self._country = country.upper()

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        country_location = _LOCATION_NAMES.get(self._country, location)
        params = urlencode({
            "keywords": query,
            "location": country_location,
            "f_TPR": "r604800",   # past 7 days
            "count": 100,
        })
        url = f"{_RSS_URL}?{params}"

        try:
            resp = self._get(
                url,
                headers={
                    "Accept": "application/rss+xml, application/xml, */*",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                },
            )
        except Exception as exc:
            raise ScraperError(f"LinkedIn RSS failed: {exc}") from exc

        return self._parse_rss(resp.text)

    def _parse_rss(self, xml_text: str) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            log.error("[linkedin-rss] XML parse error: %s", exc)
            return []

        jobs = []
        for item in root.findall(".//item"):
            try:
                jobs.append(self._map_item(item))
            except Exception as exc:
                log.debug("[linkedin-rss] Skipped item: %s", exc)
        return jobs

    def _map_item(self, item: ET.Element) -> dict[str, Any]:
        url = (item.findtext("link") or "").strip()
        if not url:
            raise ValueError("Missing link")

        ext_id = hashlib.md5(url.encode()).hexdigest()
        title = item.findtext("title") or ""

        # LinkedIn RSS title format: "Job Title at Company (Location)"
        company, city = "", ""
        if " at " in title:
            parts = title.split(" at ", 1)
            title = parts[0].strip()
            rest = parts[1]
            if "(" in rest and rest.endswith(")"):
                company = rest[:rest.rfind("(")].strip()
                city = rest[rest.rfind("(") + 1:-1].strip()
            else:
                company = rest.strip()

        return {
            "external_id": ext_id,
            "title": title,
            "company": company,
            "city": city,
            "country": self._country,
            "source_url": url,
            "description": item.findtext("description") or "",
            "posted_at": item.findtext("pubDate") or "",
        }
