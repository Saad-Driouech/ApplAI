"""
Fetch and parse a single job posting from a user-supplied URL.

Only public https hosts are allowed: every hop (including redirects) is
resolved and rejected if any address is private, loopback, link-local or
otherwise non-global. The response body is capped and parsed from the
schema.org JobPosting JSON-LD when present, else from the page text.

Residual risk: httpx re-resolves the host after the check (DNS rebinding);
acceptable because callers are authenticated with the API key.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from src.logger import get_logger
from src.utils.sanitize import sanitize_job, strip_html

log = get_logger(__name__)

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 15.0
MIN_DESCRIPTION_CHARS = 200

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ApplAI/1.1)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


class JobFetchError(ValueError):
    """The URL is not allowed, could not be fetched, or holds no readable job description."""


def _check_public_https(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise JobFetchError("Only https:// URLs are allowed")
    if parts.username or parts.password:
        raise JobFetchError("URLs with credentials are not allowed")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise JobFetchError(f"Cannot resolve host {parts.hostname}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise JobFetchError(f"Host {parts.hostname} resolves to a non-public address")


def _download(url: str) -> tuple[str, str]:
    """GET with manual redirect handling so every hop is re-validated. Returns (final_url, html)."""
    with httpx.Client(headers=_HEADERS, timeout=TIMEOUT_SECONDS, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _check_public_https(url)
            with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise JobFetchError("Redirect without a Location header")
                    url = urljoin(url, location)
                    continue
                if resp.status_code >= 400:
                    raise JobFetchError(f"Fetching the page failed with HTTP {resp.status_code}")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise JobFetchError("Page is larger than the 2 MB limit")
                return url, body.decode(resp.encoding or "utf-8", errors="replace")
    raise JobFetchError(f"More than {MAX_REDIRECTS} redirects")


def _iter_ld_nodes(data: Any):
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld_nodes(item)
    elif isinstance(data, dict):
        yield data
        if "@graph" in data:
            yield from _iter_ld_nodes(data["@graph"])


def _is_job_posting(node: dict) -> bool:
    types = node.get("@type")
    return "JobPosting" in (types if isinstance(types, list) else [types])


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def _from_json_ld(soup: BeautifulSoup) -> dict | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _iter_ld_nodes(data):
            if not _is_job_posting(node):
                continue
            org = _first(node.get("hiringOrganization")) or {}
            address = (_first(node.get("jobLocation")) or {}).get("address") or {}
            address = _first(address) if isinstance(address, list) else address
            country = address.get("addressCountry") if isinstance(address, dict) else ""
            if isinstance(country, dict):
                country = country.get("name", "")
            return {
                "title": node.get("title") or "",
                "company": org.get("name", "") if isinstance(org, dict) else str(org),
                "city": address.get("addressLocality", "") if isinstance(address, dict) else "",
                "country": country.upper() if isinstance(country, str) and len(country) == 2 else "",
                "description": node.get("description") or "",
                "posted_at": node.get("datePosted") or "",
            }
    return None


def _from_page_text(soup: BeautifulSoup) -> dict:
    og_title = soup.find("meta", property="og:title")
    title = og_title.get("content", "") if og_title else (soup.title.string if soup.title else "")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "svg"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    return {"title": title or "", "company": "", "city": "", "country": "",
            "description": main.get_text(" "), "posted_at": ""}


def fetch_job_posting(url: str) -> dict:
    """
    Fetch a public job posting and return a sanitized job dict
    (title, company, city, country, description, source_url, external_id, posted_at).
    Fields the page does not expose are empty strings.
    """
    final_url, html = _download(url.strip())
    soup = BeautifulSoup(html, "lxml")
    raw = _from_json_ld(soup) or _from_page_text(soup)
    raw["description"] = strip_html(raw["description"])
    if len(raw["description"]) < MIN_DESCRIPTION_CHARS:
        raise JobFetchError(
            "No readable job description on that page (it may require a login, e.g. LinkedIn) "
            "— paste the job description text instead"
        )
    raw["source_url"] = final_url
    raw["external_id"] = hashlib.sha256(final_url.encode()).hexdigest()[:32]
    log.info("Fetched job posting from %s (%d chars)", urlsplit(final_url).hostname, len(raw["description"]))
    return sanitize_job(raw)
