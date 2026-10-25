"""
Sanitize all data coming from external job boards.

Text is normalized to plain Unicode and stored unescaped; each output sink
(SQL, LaTeX, HTML) applies its own escaping. URLs are restricted to http(s).
"""
import re
import html
import unicodedata
from pathlib import PurePosixPath

import ftfy

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
    # Unescape entities first; repeat for sources that double-encode them
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    # Strip tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def clean_text(text: str) -> str:
    """Repair mis-decoded UTF-8 and drop control characters; otherwise leave text unchanged."""
    text = ftfy.fix_text(
        text,
        unescape_html=False,
        uncurl_quotes=False,
        fix_character_width=False,
    )
    return "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))


def sanitize_text(text: str, field: str, decode_html: bool = True) -> str:
    """Normalize a scraped text field to length-capped plain Unicode (not escaped)."""
    if not isinstance(text, str):
        return ""
    if decode_html:
        text = html.unescape(text)
    max_len = LIMITS.get(field, 1000)
    return clean_text(text)[:max_len].strip()


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


CONTENT_FIELDS = ("title", "company", "city", "description", "salary_info")


def normalize_content(value, field: str) -> str:
    """Strip HTML and normalize a human-readable job field."""
    return sanitize_text(strip_html(value), field, decode_html=False)


def sanitize_job(raw: dict) -> dict:
    """Sanitize an entire job record from a scraper."""
    job = {f: normalize_content(raw.get(f, ""), f) for f in CONTENT_FIELDS}
    job.update({
        "external_id": sanitize_text(raw.get("external_id", ""), "title"),
        "country": sanitize_text(raw.get("country", ""), "country"),
        "source_url": sanitize_url(raw.get("source_url", "")),
        "posted_at": sanitize_text(raw.get("posted_at", ""), "title"),
    })
    return job
