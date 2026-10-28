"""Tests for scraper _map_item methods and remote location inference."""
import pytest

from src.scrapers.location import infer_onsite_country, infer_remote_country
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


    def test_city_keeps_location_requirement(self):
        item = {"id": "3", "position": "ML Eng", "company": "Co", "url": "https://x", "location": "USA only"}
        result = self._scraper()._map_item(item)
        assert result["city"] == "Remote · USA only"
        assert result["country"] == "XX"


# ── Location inference (RemoteOK + Remotive) ──────────────────────────────

class TestInferRemoteCountry:
    @pytest.mark.parametrize("location,expected", [
        ("Germany", "DE"),
        ("Berlin, Germany", "DE"),
        ("Dubai, UAE", "AE"),
        ("UAE", "AE"),
        ("Riyadh, Saudi Arabia", "SA"),
        ("Amsterdam, Netherlands", "NL"),
        ("Zurich, Switzerland", "CH"),
        ("Doha", "QA"),
    ])
    def test_target_countries(self, location, expected):
        assert infer_remote_country(location) == expected

    @pytest.mark.parametrize("location", [
        "Worldwide", "Anywhere", "Europe", "EU", "EMEA",
        "Europe, USA, Canada, APAC",
        "Europe, USA, UK, Canada, Switzerland",
        "", None, "Remote", "Fully remote",
    ])
    def test_workable_from_home_base(self, location):
        assert infer_remote_country(location) == "DE"

    @pytest.mark.parametrize("location", [
        "USA only", "United States", "Chicago, IL", "Redwood City",
        "Your Remote US Home Office", "Canada", "Tech hub, LATAM", "Sweden",
    ])
    def test_outside_targets(self, location):
        assert infer_remote_country(location) == "XX"


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


# ── Location inference (Arbeitnow) ────────────────────────────────────────

class TestInferOnsiteCountry:
    @pytest.mark.parametrize("location,expected", [
        ("Berlin", "DE"), ("Hösbach", "DE"), ("", "DE"), (None, "DE"), ("Remote", "DE"),
        ("Munich, Bavaria, Germany", "DE"), ("Germany, Manchester", "DE"),
        ("Zürich", "CH"), ("Amsterdam", "NL"),
        ("London", "XX"), ("GB - Dundee", "XX"), ("Remote (UK hours)", "XX"),
        ("Bicester United Kingdom", "XX"), ("Sydney, Australia", "XX"), ("Vilnius", "XX"), ("Wien", "XX"),
    ])
    def test_locations(self, location, expected):
        assert infer_onsite_country(location) == expected


def test_arbeitnow_posted_at_is_iso():
    from src.scrapers.arbeitnow import ArbeitnowScraper, _iso_from_epoch

    item = {"slug": "ml-eng", "title": "ML Engineer", "company_name": "Acme", "location": "Berlin", "created_at": 1774341045}
    assert ArbeitnowScraper.__new__(ArbeitnowScraper)._map_item(item)["posted_at"] == "2026-03-24T08:30:45+00:00"
    assert _iso_from_epoch(None) == ""
