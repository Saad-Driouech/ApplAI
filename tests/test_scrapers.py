"""Tests for scraper _map_item and _infer_country methods."""
import pytest

from src.scrapers.remoteok import RemoteOKScraper
from src.scrapers.remotive import RemotiveScraper
from src.scrapers.arbeitnow import ArbeitnowScraper
from src.scrapers.adzuna import AdzunaScraper


# ── RemoteOK ──────────────────────────────────────────────────────────────

class TestRemoteOKMapItem:
    def _scraper(self):
        return RemoteOKScraper.__new__(RemoteOKScraper)

    def test_maps_required_fields(self):
        scraper = self._scraper()
        item = {
            "id": "12345",
            "position": "Senior ML Engineer",
            "company": "OpenAI",
            "url": "https://remoteok.com/jobs/12345",
            "description": "Build ML systems.",
            "date": "2026-04-01",
            "location": "",
            "salary_min": 0,
            "salary_max": 0,
        }
        result = scraper._map_item(item)
        assert result["external_id"] == "12345"
        assert result["title"] == "Senior ML Engineer"
        assert result["company"] == "OpenAI"
        assert result["source_url"] == "https://remoteok.com/jobs/12345"
        assert result["city"] == "Remote"

    def test_uses_position_over_title(self):
        scraper = self._scraper()
        item = {
            "id": "1",
            "position": "ML Engineer",
            "title": "Old Title",
            "company": "Acme",
            "url": "https://example.com",
            "location": "",
            "salary_min": 0,
            "salary_max": 0,
        }
        result = scraper._map_item(item)
        assert result["title"] == "ML Engineer"

    def test_salary_range_formatted(self):
        scraper = self._scraper()
        item = {
            "id": "2",
            "position": "Engineer",
            "company": "Co",
            "url": "https://example.com",
            "location": "",
            "salary_min": 80000,
            "salary_max": 120000,
        }
        result = scraper._map_item(item)
        assert "80000" in result["salary_info"]
        assert "120000" in result["salary_info"]

    def test_missing_id_raises(self):
        scraper = self._scraper()
        with pytest.raises(ValueError, match="Missing id"):
            scraper._map_item({"position": "Engineer", "company": "Co"})


class TestRemoteOKInferCountry:
    def test_germany(self):
        assert RemoteOKScraper._infer_country("Germany") == "DE"
        assert RemoteOKScraper._infer_country("Europe") == "DE"

    def test_uae(self):
        assert RemoteOKScraper._infer_country("Dubai, UAE") == "AE"

    def test_saudi(self):
        assert RemoteOKScraper._infer_country("Riyadh, Saudi Arabia") == "SA"

    def test_netherlands(self):
        assert RemoteOKScraper._infer_country("Amsterdam, Netherlands") == "NL"

    def test_switzerland(self):
        assert RemoteOKScraper._infer_country("Zurich, Switzerland") == "CH"

    def test_worldwide_defaults_to_de(self):
        assert RemoteOKScraper._infer_country("Worldwide") == "DE"
        assert RemoteOKScraper._infer_country("") == "DE"


# ── Remotive ──────────────────────────────────────────────────────────────

class TestRemotiveInferCountry:
    def test_germany(self):
        assert RemotiveScraper._infer_country("Germany") == "DE"
        assert RemotiveScraper._infer_country("EU") == "DE"

    def test_uae(self):
        assert RemotiveScraper._infer_country("UAE") == "AE"
        assert RemotiveScraper._infer_country("Dubai") == "AE"

    def test_saudi(self):
        assert RemotiveScraper._infer_country("Saudi Arabia") == "SA"

    def test_worldwide_defaults_to_de(self):
        assert RemotiveScraper._infer_country("Worldwide") == "DE"
        assert RemotiveScraper._infer_country("") == "DE"


# ── Arbeitnow ─────────────────────────────────────────────────────────────

class TestArbeitnowMapItem:
    def _scraper(self):
        return ArbeitnowScraper.__new__(ArbeitnowScraper)

    def test_maps_required_fields(self):
        scraper = self._scraper()
        item = {
            "slug": "ml-engineer-acme-123",
            "title": "ML Engineer",
            "company_name": "Acme GmbH",
            "location": "Berlin",
            "description": "Build ML pipelines.",
            "created_at": 1700000000,
        }
        result = scraper._map_item(item)
        assert result["external_id"] == "ml-engineer-acme-123"
        assert result["title"] == "ML Engineer"
        assert result["company"] == "Acme GmbH"
        assert result["country"] == "DE"
        assert "arbeitnow.com" in result["source_url"]

    def test_missing_slug_raises(self):
        scraper = self._scraper()
        with pytest.raises(ValueError):
            scraper._map_item({"title": "Engineer", "company_name": "Co"})


# ── Adzuna ────────────────────────────────────────────────────────────────

class TestAdzunaMapItem:
    def _scraper(self):
        return AdzunaScraper.__new__(AdzunaScraper)

    def test_maps_required_fields(self):
        scraper = self._scraper()
        item = {
            "id": "adzuna-001",
            "title": "Data Scientist",
            "company": {"display_name": "Google"},
            "location": {"display_name": "Berlin, Germany"},
            "redirect_url": "https://www.adzuna.de/jobs/ad/001",
            "description": "Work on ML models.",
            "created": "2026-04-01T00:00:00Z",
        }
        result = scraper._map_item(item, "DE")
        assert result["external_id"] == "adzuna-001"
        assert result["title"] == "Data Scientist"
        assert result["company"] == "Google"
        assert result["city"] == "Berlin"
        assert result["country"] == "DE"

    def test_salary_range_formatted(self):
        scraper = self._scraper()
        item = {
            "id": "2",
            "title": "Eng",
            "company": {"display_name": "Co"},
            "location": {"display_name": "Berlin"},
            "redirect_url": "https://example.com",
            "salary_min": 60000.0,
            "salary_max": 90000.0,
        }
        result = scraper._map_item(item, "DE")
        assert "60000" in result["salary_info"]
        assert "90000" in result["salary_info"]

    def test_missing_id_raises(self):
        scraper = self._scraper()
        with pytest.raises(ValueError, match="Missing id"):
            scraper._map_item({"title": "Engineer", "company": {"display_name": "Co"}}, "DE")
