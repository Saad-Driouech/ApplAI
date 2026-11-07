"""
EnglishJobs.de scraper — English-speaking jobs in Germany, aggregated from other boards.

No API: parses the server-rendered search results (/jobs/<query>). Each card links
to a /clickout/ redirect, which robots.txt disallows, so only the listing is scraped
and the description is the result snippet. /clickout/<id> redirects without the
signed query string (also for /clickout_alt/ cards), so that short form is stored.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from src.logger import get_logger
from src.scrapers.base import BaseScraper, ScraperError
from src.scrapers.location import infer_onsite_country

log = get_logger(__name__)

_SEARCH_URL = "https://englishjobs.de/jobs/{query}"

_DATE_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})(?:,\s*(\d{4}))?$"
)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _parse_listing_date(text: str, today: date | None = None) -> str:
    """Turn "November 2" into an ISO date, assuming the most recent past occurrence."""
    m = _DATE_RE.match(text.strip())
    if not m:
        return ""
    today = today or datetime.now(timezone.utc).date()
    month, day, year = m.groups()
    try:
        parsed = datetime.strptime(f"{month} {day} {year or today.year}", "%B %d %Y").date()
    except ValueError:
        return ""
    if not year and parsed > today + timedelta(days=1):
        parsed = parsed.replace(year=parsed.year - 1)
    return parsed.isoformat()


class EnglishJobsScraper(BaseScraper):
    source_name = "englishjobs"
    base_url = "https://englishjobs.de"
    request_delay = 2.0

    def __init__(self, db_path, max_pages: int = 3, **kwargs):
        super().__init__(db_path, **kwargs)
        self._max_pages = max_pages

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        # The board is Germany-only, so `location` is ignored; "A OR B" runs one search per term
        terms = [t.strip() for t in re.split(r"\s+OR\s+", query) if t.strip()]
        jobs: dict[str, dict[str, Any]] = {}

        for term in terms:
            url = _SEARCH_URL.format(query=quote(term.replace(" ", "_")))
            for page in range(1, self._max_pages + 1):
                params = {"sort": "date"}
                if page > 1:
                    params["page"] = page
                try:
                    resp = self._get(url, params=params)
                except Exception as exc:
                    raise ScraperError(f"EnglishJobs request failed: {exc}") from exc

                page_jobs = self._parse(resp.text)
                for job in page_jobs:
                    jobs.setdefault(job["external_id"], job)
                if not page_jobs:
                    break

        return list(jobs.values())

    def _parse(self, html: str) -> list[dict[str, Any]]:
        soup = BeautifulSoup(html, "lxml")
        results = []
        for card in soup.select("div.js-job[id]"):
            try:
                results.append(self._map_card(card))
            except Exception as exc:
                log.debug("[englishjobs] Skipped card: %s", exc)
        return results

    def _map_card(self, card) -> dict[str, Any]:
        link = card.select_one("a.js-joblink[href]")
        title_el = card.select_one("h3")
        if not link or not title_el:
            raise ValueError("Missing link or title")

        company_el = card.select_one("ul li.break-words")
        facts = [_collapse(li.get_text()) for li in card.select("ul li:not(.break-words)")]
        posted_at = next((d for d in map(_parse_listing_date, facts) if d), "")
        city = facts[0] if facts and not _parse_listing_date(facts[0]) else ""
        tags = [f for f in facts[1:] if not _parse_listing_date(f)]

        snippet_el = card.select_one("div.text-gray-400")
        description = _collapse(snippet_el.get_text()) if snippet_el else ""
        if tags:
            description = f"{description}\n\nTags: {', '.join(tags)}".strip()

        return {
            "external_id": card["id"],
            "title": _collapse(title_el.get_text()),
            "company": _collapse(company_el.get_text()) if company_el else "",
            "city": city,
            "country": infer_onsite_country(city),
            "salary_info": "",
            "source_url": f"{self.base_url}/clickout/{card['id']}",
            "description": description,
            "posted_at": posted_at,
        }
