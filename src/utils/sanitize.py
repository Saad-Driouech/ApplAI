"""
Sanitize all data coming from external job boards.
Prevents XSS, SQL injection, and path traversal.
"""
import re
import html
from pathlib import PurePosixPath

LIMITS = {
    "title": 200,
    "company": 150,
    "city": 100,
    "country": 5,
    "description": 50_000,
    "salary_info": 200,
    "source_url": 2000,
}


def strip_html(text: str) -> str:
    """Unescape HTML entities and strip all HTML tags, leaving plain text."""
    if not isinstance(text, str):
        return ""
    # Unescape entities first (&lt; → <, &amp; → &, etc.)
    text = html.unescape(text)
    # Strip tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sanitize_text(text: str, field: str) -> str:
    """Remove dangerous content from a text field."""
    if not isinstance(text, str):
        return ""
    text = text.replace("\x00", "")
    text = html.escape(text)
    max_len = LIMITS.get(field, 1000)
    return text[:max_len].strip()


def sanitize_company_for_path(company: str) -> str:
    """Make company name safe for use as a directory name."""
    safe = re.sub(r'[^a-zA-Z0-9\s\-.]', '', company)
    safe = safe.strip().replace(' ', '_')
    safe = safe.replace('..', '').lstrip('.').lstrip('/')

    test_path = PurePosixPath(safe)
    if test_path.is_absolute() or '..' in test_path.parts:
        raise ValueError(f"Unsafe company name: {company}")
    if not safe:
        raise ValueError(f"Company name sanitizes to empty: {company}")
    return safe[:100]


def sanitize_url(url: str) -> str:
    """Validate URL is actually HTTP(S)."""
    if not isinstance(url, str):
        return ""
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        return ""
    url = re.sub(r'[\r\n\t]', '', url)
    return url[:2000]


def sanitize_job(raw: dict) -> dict:
    """Sanitize an entire job record from a scraper."""
    return {
        "external_id": sanitize_text(raw.get("external_id", ""), "title"),
        "title": sanitize_text(raw.get("title", ""), "title"),
        "company": sanitize_text(raw.get("company", ""), "company"),
        "city": sanitize_text(raw.get("city", ""), "city"),
        "country": sanitize_text(raw.get("country", ""), "country"),
        "description": sanitize_text(strip_html(raw.get("description", "")), "description"),
        "salary_info": sanitize_text(raw.get("salary_info", ""), "salary_info"),
        "source_url": sanitize_url(raw.get("source_url", "")),
        "posted_at": sanitize_text(raw.get("posted_at", ""), "title"),
    }
