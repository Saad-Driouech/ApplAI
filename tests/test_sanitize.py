"""Tests for src/utils/sanitize.py"""
import pytest

from src.utils.sanitize import sanitize_company_for_path, sanitize_job, sanitize_text, sanitize_url, strip_html


class TestStripHtml:
    def test_removes_tags(self):
        assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_unescapes_entities(self):
        assert strip_html("AT&amp;T pays &lt;100k") == "AT&T pays <100k"

    def test_collapses_whitespace(self):
        assert strip_html("<p>  lots   of   space  </p>") == "lots of space"

    def test_non_string_returns_empty(self):
        assert strip_html(None) == ""
        assert strip_html(123) == ""

    def test_empty_string(self):
        assert strip_html("") == ""


class TestSanitizeText:
    def test_truncates_to_limit(self):
        result = sanitize_text("x" * 300, "title")
        assert len(result) <= 200

    def test_strips_null_bytes(self):
        result = sanitize_text("hello\x00world", "title")
        assert "\x00" not in result
        assert "hello" in result

    def test_escapes_html(self):
        result = sanitize_text('<script>alert("xss")</script>', "title")
        assert "<script>" not in result

    def test_non_string_returns_empty(self):
        assert sanitize_text(None, "title") == ""
        assert sanitize_text(42, "title") == ""


class TestSanitizeUrl:
    def test_valid_https_url(self):
        url = "https://remoteok.com/jobs/123"
        assert sanitize_url(url) == url

    def test_valid_http_url(self):
        assert sanitize_url("http://example.com/job") == "http://example.com/job"

    def test_rejects_javascript_url(self):
        assert sanitize_url("javascript:alert(1)") == ""

    def test_rejects_non_string(self):
        assert sanitize_url(None) == ""

    def test_strips_newlines(self):
        result = sanitize_url("https://example.com/job\r\n?q=1")
        assert "\r" not in result
        assert "\n" not in result

    def test_truncates_long_url(self):
        url = "https://example.com/" + "a" * 3000
        assert len(sanitize_url(url)) <= 2000


class TestSanitizeCompanyForPath:
    def test_basic_company_name(self):
        assert sanitize_company_for_path("Acme GmbH") == "Acme_GmbH"

    def test_special_chars_removed(self):
        result = sanitize_company_for_path("A&B Corp!")
        assert "&" not in result
        assert "!" not in result

    def test_path_traversal_neutralized(self):
        # Slashes are stripped; '../../etc' becomes 'etc' — safe directory name
        result = sanitize_company_for_path("../../etc")
        assert ".." not in result
        assert "/" not in result
        assert result  # non-empty

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            sanitize_company_for_path("!!!")


class TestSanitizeJob:
    def test_full_sanitization(self):
        raw = {
            "external_id": "job-001",
            "title": "<b>ML Engineer</b>",
            "company": "Acme GmbH",
            "city": "Berlin",
            "country": "DE",
            "description": "<p>Build <em>ML</em> systems.</p>",
            "salary_info": "80k–100k",
            "source_url": "https://example.com/job/1",
            "posted_at": "2026-04-01",
        }
        result = sanitize_job(raw)
        assert "<b>" not in result["title"]
        assert "<p>" not in result["description"]
        assert result["source_url"] == "https://example.com/job/1"
        assert result["country"] == "DE"

    def test_invalid_url_cleared(self):
        raw = {
            "external_id": "x", "title": "T", "company": "C",
            "city": "", "country": "DE", "description": "",
            "salary_info": "", "source_url": "ftp://not-allowed.com", "posted_at": "",
        }
        result = sanitize_job(raw)
        assert result["source_url"] == ""
