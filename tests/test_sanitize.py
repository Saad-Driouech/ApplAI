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

    def test_stores_plain_text_unescaped(self):
        assert sanitize_text("AT&T R&D <Team> \"O'Reilly\"", "title") == "AT&T R&D <Team> \"O'Reilly\""

    def test_decodes_html_entities(self):
        assert sanitize_text("AT&amp;T &quot;Lab&quot; O&#x27;Reilly", "company") == 'AT&T "Lab" O\'Reilly'

    def test_repairs_mojibake(self):
        assert sanitize_text("Youâ\u0080\u0099ll work 8â\u0080\u009320 hrs", "title") == "You’ll work 8–20 hrs"

    @pytest.mark.parametrize("text", [
        "You’ll own “evals” — end‑to‑end • naïve café",
        "Müller & Söhne GmbH",
        "日本語 ＡＢＣ",
    ])
    def test_correct_text_unchanged(self, text):
        assert sanitize_text(text, "title") == text

    def test_strips_control_characters(self):
        assert sanitize_text("ML​Engineer\x07", "title") == "MLEngineer"

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

    def test_double_encoded_entities_decoded(self):
        result = sanitize_job({"title": "Fleet &amp;amp; Maintenance", "description": "<p>People &amp;amp; Culture</p>"})
        assert result["title"] == "Fleet & Maintenance"
        assert result["description"] == "People & Culture"

    def test_invalid_url_cleared(self):
        raw = {
            "external_id": "x", "title": "T", "company": "C",
            "city": "", "country": "DE", "description": "",
            "salary_info": "", "source_url": "ftp://not-allowed.com", "posted_at": "",
        }
        result = sanitize_job(raw)
        assert result["source_url"] == ""
