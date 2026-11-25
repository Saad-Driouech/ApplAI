"""
Public job-board APIs of applicant tracking systems (ATS), used by the company watchlist.

These APIs exist so companies can embed their job board: no key, no login, and they work from any
host. `fetch_board` returns BaseScraper-style job dicts with full descriptions; `detect_ats` finds
which ATS and board slug a company uses.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional
# Imported for the type only; all parsing goes through defusedxml
from xml.etree.ElementTree import Element  # nosec B405

import defusedxml.ElementTree as SafeET
import httpx

from src.logger import get_logger
from src.matching.scorer import title_is_relevant
from src.scrapers.location import OUTSIDE_TARGETS, REMOTE, TARGET_COUNTRIES, infer_country, remote_city

log = get_logger(__name__)

# A GET that raises for HTTP errors (BaseScraper._get or detection's own client)
Get = Callable[..., httpx.Response]

ATS_NAMES = ("greenhouse", "lever", "ashby", "smartrecruiters", "personio", "recruitee", "workable")

# Slugs end up in URLs (and as subdomains for Personio and Recruitee), so no dots, slashes or '@'
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_SMARTRECRUITERS_MAX_PAGES = 10


class AtsError(ValueError):
    """Unknown ATS, invalid slug, or a board that cannot be read."""


def check_slug(ats: str, slug: str) -> None:
    if ats not in ATS_NAMES:
        raise AtsError(f"Unknown ATS '{ats}' (supported: {', '.join(ATS_NAMES)})")
    if not _SLUG.fullmatch(slug or ""):
        raise AtsError(f"Invalid board slug '{slug}'")


# ── Mapping helpers ───────────────────────────────────────────────────────


def _iso_date(value: Any) -> str:
    """ISO-8601 from an ISO string or a millisecond epoch (Lever)."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    return str(value or "")


def _place(options: list[tuple[str, str]], remote: bool) -> tuple[str, str]:
    """
    (city, country) from (location text, ISO code or '') pairs: the first location in a target
    country or REMOTE, else the first one. Unrecognised places become OUTSIDE_TARGETS.
    """
    placed = []
    for text, code in options:
        code = (code or "").strip().upper()
        country = code if len(code) == 2 else infer_country(text, remote)
        placed.append((text or "", country))
    if not placed:
        return ("Remote", REMOTE) if remote else ("", OUTSIDE_TARGETS)
    text, country = next((p for p in placed if p[1] in TARGET_COUNTRIES or p[1] == REMOTE), placed[0])
    return (remote_city(text) if remote else text), country


def _job(job_id: Any, title: str, url: str, description: str, posted_at: Any, city: str, country: str) -> dict:
    return {
        "external_id": str(job_id), "title": title or "", "source_url": url or "", "description": description or "",
        "posted_at": _iso_date(posted_at), "city": city, "country": country, "salary_info": "",
    }


# ── Adapters ──────────────────────────────────────────────────────────────


def _greenhouse(get: Get, slug: str, details: bool) -> list[dict]:
    params = {"content": "true"} if details else None
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params=params).json()
    jobs = []
    for item in data.get("jobs", []):
        location = (item.get("location") or {}).get("name", "")
        city, country = _place([(location, "")], remote=False)
        jobs.append(_job(item["id"], item.get("title"), item.get("absolute_url"), item.get("content", ""),
                         item.get("first_published") or item.get("updated_at"), city, country))
    return jobs


def _lever(get: Get, slug: str, details: bool) -> list[dict]:
    items = _first_ok(get, [f"https://api.lever.co/v0/postings/{slug}", f"https://api.eu.lever.co/v0/postings/{slug}"],
                      params={"mode": "json"}).json()
    jobs = []
    for item in items:
        cats = item.get("categories") or {}
        others = [loc for loc in cats.get("allLocations") or [] if loc != cats.get("location")]
        options = [(cats.get("location", ""), item.get("country", ""))] + [(loc, "") for loc in others]
        city, country = _place(options, remote=item.get("workplaceType") == "remote")
        lists = "".join(f"<h3>{part.get('text', '')}</h3>{part.get('content', '')}" for part in item.get("lists") or [])
        description = f"{item.get('description', '')}{lists}{item.get('additional', '')}"
        jobs.append(_job(item["id"], item.get("text"), item.get("hostedUrl"), description,
                         item.get("createdAt"), city, country))
    return jobs


def _ashby_location(entry: dict) -> str:
    address = ((entry.get("address") or {}).get("postalAddress") or {})
    parts = [address.get("addressLocality"), address.get("addressCountry")]
    return ", ".join(p for p in parts if p) or entry.get("location", "")


def _ashby(get: Get, slug: str, details: bool) -> list[dict]:
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
    jobs = []
    for item in data.get("jobs", []):
        if item.get("isListed") is False:
            continue
        options = [(_ashby_location(item), "")] + [(_ashby_location(e), "") for e in item.get("secondaryLocations") or []]
        city, country = _place(options, remote=bool(item.get("isRemote")))
        jobs.append(_job(item["id"], item.get("title"), item.get("jobUrl"),
                         item.get("descriptionHtml") or item.get("descriptionPlain", ""),
                         item.get("publishedAt"), city, country))
    return jobs


def _smartrecruiters(get: Get, slug: str, details: bool) -> list[dict]:
    base = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    jobs = []
    for page in range(_SMARTRECRUITERS_MAX_PAGES):
        data = get(base, params={"limit": 100, "offset": page * 100}).json()
        content = data.get("content") or []
        for item in content:
            loc = item.get("location") or {}
            city, country = _place([(loc.get("fullLocation") or loc.get("city", ""), loc.get("country", ""))],
                                   remote=bool(loc.get("remote")))
            url = f"https://jobs.smartrecruiters.com/{slug}/{item['id']}"
            description = ""
            # The list has no descriptions: fetch them only for titles worth scoring
            if details and title_is_relevant(item.get("name", "")):
                try:
                    detail = get(f"{base}/{item['id']}").json()
                    sections = (detail.get("jobAd") or {}).get("sections") or {}
                    description = "".join(
                        f"<h3>{s.get('title', '')}</h3>{s.get('text', '')}" for s in sections.values() if isinstance(s, dict)
                    )
                    url = detail.get("postingUrl") or url
                except Exception as exc:
                    log.warning("[ats] SmartRecruiters detail %s/%s failed: %s", slug, item["id"], exc)
            jobs.append(_job(item["id"], item.get("name"), url, description, item.get("releasedDate"), city, country))
        if len(content) < 100 or len(jobs) >= int(data.get("totalFound") or 0):
            break
    return jobs


def _text(node: Optional[Element], tag: str) -> str:
    found = node.find(tag) if node is not None else None
    return (found.text or "").strip() if found is not None else ""


def _personio(get: Get, slug: str, details: bool) -> list[dict]:
    resp = _first_ok(get, [f"https://{slug}.jobs.personio.de/xml", f"https://{slug}.jobs.personio.com/xml"],
                     params={"language": "en"})
    root = SafeET.fromstring(resp.content)
    jobs = []
    for pos in root.iter("position"):
        offices = [_text(pos, "office")] + [(o.text or "").strip() for o in pos.findall("additionalOffices/office")]
        city, country = _place([(o, "") for o in offices if o], remote=False)
        description = "".join(
            f"<h3>{_text(d, 'name')}</h3>{_text(d, 'value')}" for d in pos.findall("jobDescriptions/jobDescription")
        )
        job_id = _text(pos, "id")
        jobs.append(_job(job_id, _text(pos, "name"), f"https://{slug}.jobs.personio.de/job/{job_id}",
                         description, _text(pos, "createdAt"), city, country))
    return jobs


def _recruitee(get: Get, slug: str, details: bool) -> list[dict]:
    data = get(f"https://{slug}.recruitee.com/api/offers/").json()
    jobs = []
    for item in data.get("offers", []):
        city, country = _place([(item.get("location", ""), item.get("country_code", ""))], remote=bool(item.get("remote")))
        description = f"{item.get('description', '')}{item.get('requirements', '')}"
        jobs.append(_job(item["id"], item.get("title"), item.get("careers_url"), description,
                         item.get("published_at"), city, country))
    return jobs


def _workable(get: Get, slug: str, details: bool) -> list[dict]:
    data = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"}).json()
    jobs = []
    for item in data.get("jobs", []):
        locations = item.get("locations") or [{"city": item.get("city"), "country": item.get("country"), "countryCode": ""}]
        options = [(", ".join(p for p in (loc.get("city"), loc.get("country")) if p), loc.get("countryCode", ""))
                   for loc in locations]
        city, country = _place(options, remote=bool(item.get("telecommuting")))
        jobs.append(_job(item.get("shortcode"), item.get("title"), item.get("url"), item.get("description", ""),
                         item.get("published_on") or item.get("created_at"), city, country))
    return jobs


_ADAPTERS = {
    "greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby, "smartrecruiters": _smartrecruiters,
    "personio": _personio, "recruitee": _recruitee, "workable": _workable,
}


def _first_ok(get: Get, urls: list[str], **kwargs) -> httpx.Response:
    """The first URL that answers; boards on an EU instance 404 on the default host."""
    last: Optional[Exception] = None
    for url in urls:
        try:
            return get(url, **kwargs)
        except httpx.HTTPStatusError as exc:
            last = exc
            if exc.response.status_code != 404:
                raise
    raise last  # type: ignore[misc]


def fetch_board(get: Get, ats: str, slug: str, details: bool = True) -> list[dict]:
    """Every job on the board. Without details, SmartRecruiters and Greenhouse skip descriptions."""
    check_slug(ats, slug)
    return _ADAPTERS[ats](get, slug, details)


_RETRY_STATUS = {429, 500, 502, 503, 504}


@contextmanager
def board_client(timeout: float = 10.0, retries: int = 2) -> Iterator[Get]:
    """
    A thread-safe Get for detection, retrying network errors and busy servers. Only ATS hosts built
    from a checked slug are requested.
    """
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ApplAI/1.1)", "Accept": "application/json, text/xml;q=0.9, */*;q=0.8"}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        def get(url: str, **kwargs: Any) -> httpx.Response:
            for attempt in range(retries + 1):
                try:
                    resp = client.get(url, **kwargs)
                    if resp.status_code not in _RETRY_STATUS or attempt == retries:
                        resp.raise_for_status()
                        return resp
                except httpx.TransportError:
                    if attempt == retries:
                        raise
                time.sleep(2 ** attempt)
            raise AssertionError("unreachable")
        yield get


# ── Detection ─────────────────────────────────────────────────────────────

_BOARD_PATTERNS = (
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([A-Za-z0-9_-]+)"),
    ("greenhouse", r"boards-api(?:\.eu)?\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("lever", r"jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_-]+)"),
    ("lever", r"api(?:\.eu)?\.lever\.co/v0/postings/([A-Za-z0-9_-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)"),
    ("ashby", r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("personio", r"//([A-Za-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
    ("recruitee", r"//([A-Za-z0-9-]+)\.recruitee\.com"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
)
# Path segments that look like slugs but are not boards
_NOT_SLUGS = {"embed", "api", "j", "o", "v0", "v1", "jobs", "job", "widget", "static", "assets", "www", "app"}


@dataclass
class Candidate:
    ats: str
    slug: str
    found_by: str                 # "url", "page" or "guess"
    job_count: int = 0
    sample_titles: list[str] = field(default_factory=list)
    error: Optional[str] = None   # the board could not be read (a missing board is not an error)


def boards_in(text: str) -> list[tuple[str, str]]:
    """(ats, slug) pairs for every ATS board link in a URL or page, in order of appearance."""
    found: dict[tuple[str, str], int] = {}
    for ats, pattern in _BOARD_PATTERNS:
        for match in re.finditer(pattern, text or "", re.IGNORECASE):
            slug = match.group(1)
            if slug.lower() not in _NOT_SLUGS and _SLUG.fullmatch(slug):
                found.setdefault((ats, slug), match.start())
    return sorted(found, key=found.get)  # type: ignore[arg-type]


def guess_slugs(name: str) -> list[str]:
    """
    Likely board slugs for a company name, with and without a legal-form suffix
    ('iba AG' -> ibaag, iba-ag, iba).
    """
    full = re.findall(r"[a-z0-9]+", name.lower().replace("'", ""))
    short = [w for w in full if w not in {"gmbh", "ag", "se", "inc", "ltd", "llc", "group", "gruppe"}]
    slugs = ["".join(full), "-".join(full), "".join(short), "-".join(short)]
    return [s for i, s in enumerate(slugs) if s and _SLUG.fullmatch(s) and s not in slugs[:i]]


def probe(get: Get, candidate: Candidate) -> Candidate:
    """Fill in the job count and sample titles (relevant ones first) by reading the board."""
    try:
        jobs = fetch_board(get, candidate.ats, candidate.slug, details=False)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:   # every ATS answers 404 for a board that does not exist
            candidate.error = f"HTTP {exc.response.status_code}"
        return candidate
    except Exception as exc:
        candidate.error = f"{type(exc).__name__}: {exc}"[:300]
        return candidate
    titles = [j["title"] for j in jobs]
    candidate.job_count = len(jobs)
    candidate.sample_titles = ([t for t in titles if title_is_relevant(t)] + [t for t in titles if not title_is_relevant(t)])[:3]
    return candidate


def detect_ats(get: Get, name: str, careers_url: Optional[str] = None,
               download: Optional[Callable[[str], tuple[str, str]]] = None) -> list[Candidate]:
    """
    Boards for a company, most reliable first: links in the careers URL itself, then links on the
    careers page (fetched with `download`, which must be SSRF-safe), then slugs guessed from the
    name. Guesses can hit a different company's board, so they must be confirmed by a person.
    """
    candidates: list[Candidate] = []
    if careers_url:
        candidates = [Candidate(ats, slug, "url") for ats, slug in boards_in(careers_url)]
        if not candidates and download:
            try:
                final_url, html = download(careers_url)
                candidates = [Candidate(ats, slug, "page") for ats, slug in boards_in(final_url + " " + html)]
            except Exception as exc:
                log.info("[ats] Careers page %s unreadable: %s", careers_url, exc)
        candidates = _probe_all(get, candidates[:5])
        if any(c.job_count for c in candidates):
            return candidates
    guesses = [Candidate(ats, slug, "guess") for slug in guess_slugs(name) for ats in ATS_NAMES]
    return candidates + [c for c in _probe_all(get, guesses) if c.job_count or c.error]


def _probe_all(get: Get, candidates: list[Candidate]) -> list[Candidate]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda c: probe(get, c), candidates))
