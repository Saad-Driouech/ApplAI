"""
RSS-based job scraper — works reliably without anti-bot blocks.

Supported feeds:
  - Indeed RSS (all supported countries)
  - StepStone RSS (DE)
  - Bayt RSS (SA, AE, QA)

RSS feeds are publicly accessible, require no authentication, and are
not subject to the same bot-detection as HTML scrapers.
"""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlencode

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)

# ── Feed URL templates ────────────────────────────────────────────────────

_INDEED_RSS = "https://{domain}/rss?{params}"
_INDEED_DOMAINS = {
    "DE": "de.indeed.com",
    "AE": "ae.indeed.com",
    "SA": "sa.indeed.com",
    "CH": "ch.indeed.com",
    "QA": "qa.indeed.com",
    "NL": "nl.indeed.com",
}

_STEPSTONE_RSS = "https://www.stepstone.de/rss/jobs/{keyword}/in-{location}"

_BAYT_RSS = "https://www.bayt.com/en/{country_slug}/jobs/{keyword}-jobs/?rss=1"
_BAYT_SLUGS = {
    "SA": "saudi-arabia",
    "AE": "uae",
    "QA": "qatar",
}


class IndeedRssScraper(BaseScraper):
    source_name = "indeed"
    request_delay = 1.0

    def __init__(self, db_path, country: str = "DE", **kwargs):
        super().__init__(db_path, **kwargs)
        self._country = country.upper()
        self._domain = _INDEED_DOMAINS.get(self._country, "indeed.com")

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        params = urlencode({
            "q": query,
            "l": location,
            "sort": "date",
            "limit": 50,
        })
        url = _INDEED_RSS.format(domain=self._domain, params=params)

        try:
            resp = self._get(url, headers={"Accept": "application/rss+xml, application/xml, */*"})
        except Exception as exc:
            raise ScraperError(f"Indeed RSS failed: {exc}") from exc

        return self._parse_rss(resp.text)

    def _parse_rss(self, xml_text: str) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            log.error("[indeed-rss] XML parse error: %s", exc)
            return []

        jobs = []
        for item in root.findall(".//item"):
            try:
                jobs.append(self._map_item(item))
            except Exception as exc:
                log.debug("[indeed-rss] Skipped item: %s", exc)
        return jobs

    def _map_item(self, item: ET.Element) -> dict[str, Any]:
        url = (item.findtext("link") or "").strip()
        if not url:
            raise ValueError("Missing link")

        ext_id = hashlib.md5(url.encode()).hexdigest()
        title = item.findtext("title") or ""
        # Indeed RSS title format: "Job Title - Company (Location)"
        company, city = "", ""
        if " - " in title:
            parts = title.rsplit(" - ", 1)
            title = parts[0].strip()
            rest = parts[1]
            if "(" in rest and rest.endswith(")"):
                company = rest[:rest.rfind("(")].strip()
                city = rest[rest.rfind("(")+1:-1].strip()
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


class StepStoneRssScraper(BaseScraper):
    source_name = "stepstone"
    request_delay = 1.5

    def __init__(self, db_path, country: str = "DE", **kwargs):
        super().__init__(db_path, **kwargs)
        self._country = country.upper()

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        keyword_slug = query.lower().replace(" ", "-").replace("/", "-")
        location_slug = location.lower().replace(" ", "-")
        url = _STEPSTONE_RSS.format(keyword=keyword_slug, location=location_slug)

        try:
            resp = self._get(url, headers={"Accept": "application/rss+xml, */*"})
        except Exception as exc:
            raise ScraperError(f"StepStone RSS failed: {exc}") from exc

        return self._parse_rss(resp.text)

    def _parse_rss(self, xml_text: str) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            log.error("[stepstone-rss] XML parse error: %s", exc)
            return []

        jobs = []
        for item in root.findall(".//item"):
            try:
                jobs.append(self._map_item(item))
            except Exception as exc:
                log.debug("[stepstone-rss] Skipped item: %s", exc)
        return jobs

    def _map_item(self, item: ET.Element) -> dict[str, Any]:
        url = (item.findtext("link") or "").strip()
        if not url:
            raise ValueError("Missing link")

        ext_id = hashlib.md5(url.encode()).hexdigest()
        title = item.findtext("title") or ""
        company = item.findtext("{http://www.stepstone.de/}company") or ""
        city = item.findtext("{http://www.stepstone.de/}location") or ""

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


class BaytRssScraper(BaseScraper):
    source_name = "bayt"
    request_delay = 1.5

    def __init__(self, db_path, country: str = "SA", **kwargs):
        super().__init__(db_path, **kwargs)
        self._country = country.upper()
        self._country_slug = _BAYT_SLUGS.get(self._country, "saudi-arabia")

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        keyword_slug = query.lower().replace(" ", "-").replace("/", "-")
        url = _BAYT_RSS.format(country_slug=self._country_slug, keyword=keyword_slug)

        try:
            resp = self._get(url, headers={"Accept": "application/rss+xml, */*"})
        except Exception as exc:
            raise ScraperError(f"Bayt RSS failed: {exc}") from exc

        return self._parse_rss(resp.text)

    def _parse_rss(self, xml_text: str) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            log.error("[bayt-rss] XML parse error: %s", exc)
            return []

        jobs = []
        for item in root.findall(".//item"):
            try:
                jobs.append(self._map_item(item))
            except Exception as exc:
                log.debug("[bayt-rss] Skipped item: %s", exc)
        return jobs

    def _map_item(self, item: ET.Element) -> dict[str, Any]:
        url = (item.findtext("link") or "").strip()
        if not url:
            raise ValueError("Missing link")

        ext_id = hashlib.md5(url.encode()).hexdigest()
        title = item.findtext("title") or ""
        # Bayt RSS title: "Job Title at Company in City, Country"
        company, city = "", ""
        if " at " in title.lower():
            parts = title.split(" at ", 1)
            title = parts[0].strip()
            rest = parts[1]
            if " in " in rest.lower():
                loc_parts = rest.split(" in ", 1)
                company = loc_parts[0].strip()
                city = loc_parts[1].split(",")[0].strip()
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
