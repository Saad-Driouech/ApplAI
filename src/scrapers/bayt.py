"""
Bayt.com scraper (Saudi Arabia / UAE / Qatar focus).

Bayt is the dominant job board across the GCC region.
Their search results page embeds structured data in JSON-LD and also renders
standard HTML cards — we parse both, preferring JSON-LD when available.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlencode

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)

_SEARCH_URL = "https://www.bayt.com/en/{country}/jobs/{keyword}-jobs/"

_COUNTRY_SLUGS: dict[str, str] = {
    "SA": "saudi-arabia",
    "AE": "uae",
    "QA": "qatar",
    "KW": "kuwait",
    "BH": "bahrain",
}

_JSON_LD_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


class BaytScraper(BaseScraper):
    source_name = "bayt"
    base_url = "https://www.bayt.com"
    request_delay = 2.5

    def __init__(self, db_path, country: str = "SA", **kwargs):
        super().__init__(db_path, **kwargs)
        self._country = country.upper()
        self._country_slug = _COUNTRY_SLUGS.get(self._country, "saudi-arabia")

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        keyword_slug = query.lower().replace(" ", "-")
        url = _SEARCH_URL.format(country=self._country_slug, keyword=keyword_slug)

        params: dict[str, Any] = {}
        if location:
            params["filters[country_code][]"] = self._country
        if kwargs.get("days_old"):
            params["filters[date_posted][]"] = kwargs["days_old"]

        try:
            resp = self._get(url, params=params or None)
        except Exception as exc:
            raise ScraperError(f"Bayt request failed: {exc}") from exc

        return self._parse(resp.text)

    def _parse(self, html: str) -> list[dict[str, Any]]:
        # Try JSON-LD first (most reliable structured data)
        jobs = self._parse_json_ld(html)
        if jobs:
            return jobs
        # Fallback to HTML cards
        return self._parse_html(html)

    def _parse_json_ld(self, html: str) -> list[dict[str, Any]]:
        results = []
        for match in _JSON_LD_RE.finditer(html):
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue

            if isinstance(data, list):
                items = data
            elif isinstance(data, dict) and data.get("@type") == "ItemList":
                items = data.get("itemListElement", [])
            else:
                items = [data]

            for item in items:
                if isinstance(item, dict) and item.get("@type") in ("JobPosting", "ListItem"):
                    actual = item.get("item", item)
                    try:
                        results.append(self._map_json_ld(actual))
                    except Exception as exc:
                        log.debug("[bayt] Skipped JSON-LD item: %s", exc)

        return results

    def _map_json_ld(self, item: dict) -> dict[str, Any]:
        url = item.get("url", "")
        if not url:
            raise ValueError("Missing url in JSON-LD item")

        # Use URL path hash as external_id since Bayt doesn't always expose IDs
        ext_id = hashlib.md5(url.encode()).hexdigest()

        loc = item.get("jobLocation", {})
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        address = loc.get("address", {})

        org = item.get("hiringOrganization", {})
        company = org.get("name", "") if isinstance(org, dict) else str(org)

        return {
            "external_id": ext_id,
            "title": item.get("title", ""),
            "company": company,
            "city": address.get("addressLocality", ""),
            "country": self._country,
            "salary_info": "",
            "source_url": url,
            "description": item.get("description", ""),
            "posted_at": item.get("datePosted", ""),
        }

    def _parse_html(self, html: str) -> list[dict[str, Any]]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            log.error("[bayt] beautifulsoup4 not installed")
            return []

        soup = BeautifulSoup(html, "lxml")
        # Bayt wraps each job in <li> tags with class "has-pointer-d"
        cards = soup.select("li[data-js-job]") or soup.select("li.has-pointer-d")
        results = []

        for card in cards:
            link_el = card.select_one("a[href*='/job/']")
            if not link_el:
                continue
            url = link_el.get("href", "")
            if url and not url.startswith("http"):
                url = "https://www.bayt.com" + url

            ext_id = hashlib.md5(url.encode()).hexdigest()
            title_el = card.select_one("h2, .t-large")
            company_el = card.select_one("[data-js-company-name], .t-default")
            location_el = card.select_one("[data-js-location], .t-mute")

            results.append({
                "external_id": ext_id,
                "title": title_el.get_text(strip=True) if title_el else "",
                "company": company_el.get_text(strip=True) if company_el else "",
                "city": location_el.get_text(strip=True) if location_el else "",
                "country": self._country,
                "source_url": url,
                "description": "",
            })

        return results
