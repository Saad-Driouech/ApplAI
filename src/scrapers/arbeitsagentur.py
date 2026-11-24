"""
Bundesagentur für Arbeit job search: keyword search over every German posting, free, no account.

The API is public but unofficial (the key is the one the agency's own website sends) and its
versions drift: search works on v6, details only on v4. Searches skip staffing and private
placement agencies; details, the only source of the description, are fetched one by one, so only
for new jobs whose title already passes the scorer's title and keyword pre-filters.
"""
from __future__ import annotations

import base64
from typing import Any

from src import database as db
from src.logger import get_logger
from src.matching.scorer import title_is_relevant
from src.scrapers.base import BaseScraper, ScraperError

log = get_logger(__name__)

_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
_SEARCH_URL = f"{_BASE}/pc/v6/jobs"
_DETAIL_URL = f"{_BASE}/pc/v4/jobdetails/{{ref}}"
_PUBLIC_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}"
_HEADERS = {"X-API-Key": "jobboerse-jobsuche", "Accept": "application/json"}

_COUNTRIES = {"DEUTSCHLAND": "DE", "SCHWEIZ": "CH", "NIEDERLANDE": "NL", "ÖSTERREICH": "AT"}


class ArbeitsagenturScraper(BaseScraper):
    source_name = "arbeitsagentur"
    base_url = "https://www.arbeitsagentur.de"
    request_delay = 1.0

    def __init__(self, db_path, max_pages: int = 3, page_size: int = 100, days: int = 7, **kwargs):
        super().__init__(db_path, **kwargs)
        self._max_pages = max_pages
        self._page_size = page_size
        self._days = days

    def _fetch_jobs(self, query: str, location: str, **kwargs: Any) -> list[dict[str, Any]]:
        # `location` is ignored: the search covers all of Germany
        hits: dict[str, dict] = {}
        errors = []
        terms = [t.strip() for t in query.split(" OR ") if t.strip()]
        for term in terms:
            try:
                for item in self._search(term):
                    hits.setdefault(item["referenznummer"], item)
            except Exception as exc:
                log.warning("[arbeitsagentur] Search for %r failed: %s", term, exc)
                errors.append(exc)
        if errors and len(errors) == len(terms):
            raise ScraperError(f"Arbeitsagentur search failed: {errors[0]}")

        relevant = {ref: item for ref, item in hits.items() if title_is_relevant(item.get("stellenangebotsTitel", ""))}
        known = self._known(list(relevant))
        jobs = []
        for ref, item in relevant.items():
            if ref in known:
                continue
            try:
                detail = self._detail(ref)
            except Exception as exc:
                log.warning("[arbeitsagentur] Detail for %s failed: %s", ref, exc)
                continue
            if detail.get("istPrivateArbeitsvermittlung"):
                continue
            jobs.append(self._map_item(item, detail))
        log.info("[arbeitsagentur] %d hits, %d relevant, %d already known", len(hits), len(relevant), len(known))
        return jobs

    def _search(self, term: str) -> list[dict]:
        items: list[dict] = []
        for page in range(1, self._max_pages + 1):
            params = {
                "was": term, "angebotsart": 1, "zeitarbeit": "false", "pav": "false",
                "veroeffentlichtseit": self._days, "size": self._page_size, "page": page,
            }
            data = self._get(_SEARCH_URL, params=params, headers=_HEADERS).json()
            batch = data.get("ergebnisliste") or []
            items.extend(i for i in batch if i.get("referenznummer"))
            if len(batch) < self._page_size or len(items) >= int(data.get("maxErgebnisse") or 0):
                break
        return items

    def _detail(self, ref: str) -> dict:
        encoded = base64.b64encode(ref.encode()).decode()
        return self._get(_DETAIL_URL.format(ref=encoded), headers=_HEADERS).json()

    def _known(self, refs: list[str]) -> set[str]:
        with db.get_conn(self._db_path) as conn:
            return {ref for ref in refs if db.job_exists(conn, self.source_name, ref)}

    def _map_item(self, item: dict, detail: dict) -> dict[str, Any]:
        ref = item["referenznummer"]
        places = detail.get("stellenlokationen") or item.get("stellenlokationen") or [{}]
        address = places[0].get("adresse") or {}
        return {
            "external_id": ref,
            "title": detail.get("stellenangebotsTitel") or item.get("stellenangebotsTitel", ""),
            "company": detail.get("firma") or item.get("firma", ""),
            "city": address.get("ort", ""),
            "country": _COUNTRIES.get((address.get("land") or "DEUTSCHLAND").upper(), "XX"),
            "salary_info": "",
            "source_url": _PUBLIC_URL.format(ref=ref),
            "description": detail.get("stellenangebotsBeschreibung", ""),
            "posted_at": item.get("datumErsteVeroeffentlichung", ""),
        }
