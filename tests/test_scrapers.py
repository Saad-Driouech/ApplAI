"""Tests for scraper _map_item methods and remote location inference."""
from datetime import date
from types import SimpleNamespace

import pytest

from src.scrapers.location import infer_onsite_country, infer_remote_country
from src.scrapers.remoteok import RemoteOKScraper
from src.scrapers.remotive import RemotiveScraper
from src.scrapers.arbeitnow import ArbeitnowScraper
from src.scrapers.adzuna import AdzunaScraper
from src.scrapers.englishjobs import EnglishJobsScraper, _parse_listing_date


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
    def test_remote_roles_tagged_remote(self, location):
        assert infer_remote_country(location) == "REMOTE"

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
        ("Berlin", "DE"), ("Hösbach", "DE"), ("", "DE"), (None, "DE"),
        ("Remote", "REMOTE"), ("Remote job", "REMOTE"),
        ("Munich, Bavaria, Germany", "DE"), ("Germany, Manchester", "DE"),
        ("Zürich", "CH"), ("Amsterdam", "NL"),
        ("London", "XX"), ("GB - Dundee", "XX"), ("Remote (UK hours)", "XX"),
        ("Bicester United Kingdom", "XX"), ("Sydney, Australia", "XX"), ("Vilnius", "XX"), ("Wien", "XX"),
        ("Darmstadt", "DE"), ("Baden-Württemberg", "DE"), ("Bern", "CH"), ("Zug", "CH"),
        ("Almelo, Overijssel, Nederland", "NL"), ("'s-Hertogenbosch, Noord-Brabant, Nederland", "NL"),
        ("Toulouse, Occitanie", "XX"), ("Lyon", "XX"), ("Cambridge", "XX"), ("La Rochelle", "XX"),
        ("Lyon 🇫🇷", "XX"), ("Hamburg 🇩🇪", "DE"), ("Utrecht 🇳🇱", "NL"),
    ])
    def test_locations(self, location, expected):
        assert infer_onsite_country(location) == expected


def test_remote_flag_location():
    assert infer_remote_country("Remote 🇳🇱") == "NL"
    assert infer_remote_country("Remote 🇺🇸") == "XX"


@pytest.mark.parametrize("location,code,expected", [
    ("Berlin", "DE", True), ("Darmstadt", "DE", False), ("Lyon 🇫🇷", "DE", False),
    ("Bern", "CH", True), ("Rotterdam, Zuid-Holland, Nederland", "NL", True), ("Gilching 🇩🇪", "DE", True),
])
def test_names_country(location, code, expected):
    from src.scrapers.location import names_country
    assert names_country(location, code) is expected


def test_arbeitnow_posted_at_is_iso():
    from src.scrapers.arbeitnow import ArbeitnowScraper, _iso_from_epoch

    item = {"slug": "ml-eng", "title": "ML Engineer", "company_name": "Acme", "location": "Berlin", "created_at": 1774341045}
    assert ArbeitnowScraper.__new__(ArbeitnowScraper)._map_item(item)["posted_at"] == "2026-03-24T08:30:45+00:00"
    assert _iso_from_epoch(None) == ""


def test_arbeitnow_paginates_and_dedupes(monkeypatch):
    scraper = ArbeitnowScraper.__new__(ArbeitnowScraper)
    scraper._max_pages = 3
    item = lambda slug: {"slug": slug, "title": "ML Engineer", "company_name": "Acme", "location": "Berlin"}
    pages = {
        1: {"data": [item("a"), item("b")], "links": {"next": "page=2"}},
        2: {"data": [item("b"), item("c")], "links": {"next": None}},
    }
    calls = []

    def fake_get(url, params=None, headers=None):
        calls.append(params)
        return SimpleNamespace(json=lambda: pages[params["page"]])

    monkeypatch.setattr(scraper, "_get", fake_get)
    jobs = scraper._fetch_jobs("ignored query", "Germany")

    assert [j["external_id"] for j in jobs] == ["a", "b", "c"]
    # Stops when there is no next page, and never sends the ignored search params
    assert calls == [{"page": 1}, {"page": 2}]


# ── EnglishJobs ───────────────────────────────────────────────────────────

def _englishjobs_card(job_id="ee61dff84d9c9495", company="AstraZeneca", facts=("München", "October 23")):
    company_li = f'<li class="flex text-sm break-words"><svg></svg> {company}</li>' if company else ""
    fact_lis = "".join(f'<li class="flex text-sm"><svg></svg> {f}</li>' for f in facts)
    return (
        f'<div id="{job_id}" class="job js-job"><div class="p-3">'
        f'<a href="/clickout/{job_id}?sig=abc&amp;p=1" class="js-joblink">'
        f'<h3 itemprop="title">Senior <em>Python</em> Engineer</h3></a>'
        f'<ul>{company_li}{fact_lis}</ul>'
        f'<div class="mr-4 flex-1 text-gray-400">Strong <em>Python</em>\n skills.</div>'
        f'</div></div>'
    )


class TestEnglishJobsParse:
    def _scraper(self):
        return EnglishJobsScraper.__new__(EnglishJobsScraper)

    def test_maps_required_fields(self):
        [job] = self._scraper()._parse(_englishjobs_card())
        assert job["external_id"] == "ee61dff84d9c9495"
        assert job["title"] == "Senior Python Engineer"
        assert job["company"] == "AstraZeneca"
        assert job["city"] == "München"
        assert job["country"] == "DE"
        assert job["source_url"] == "https://englishjobs.de/clickout/ee61dff84d9c9495"
        assert job["description"] == "Strong Python skills."
        assert job["posted_at"].endswith("-10-23")

    def test_tags_appended_to_description(self):
        [job] = self._scraper()._parse(_englishjobs_card(facts=("Berlin", "November 5", "remote work", "student")))
        assert job["city"] == "Berlin"
        assert job["description"].endswith("Tags: remote work, student")

    def test_missing_company_keeps_location(self):
        [job] = self._scraper()._parse(_englishjobs_card(company=""))
        assert job["company"] == ""
        assert job["city"] == "München"

    def test_card_without_link_skipped(self):
        html = '<div id="abc" class="job js-job"><h3>Engineer</h3></div>' + _englishjobs_card()
        assert len(self._scraper()._parse(html)) == 1

    @pytest.mark.parametrize("text,expected", [
        ("November 2", "2026-11-02"),
        ("December 30", "2025-12-30"),
        ("March 1, 2024", "2024-03-01"),
        ("remote work", ""),
        ("February 30", ""),
    ])
    def test_listing_date(self, text, expected):
        assert _parse_listing_date(text, today=date(2026, 11, 5)) == expected

    def test_or_query_split_and_deduped(self, monkeypatch):
        scraper = self._scraper()
        scraper._max_pages = 3
        pages = {
            ("AI_Engineer", None): _englishjobs_card("a1") + _englishjobs_card("shared"),
            ("Data_Scientist", None): _englishjobs_card("shared") + _englishjobs_card("d1"),
        }
        calls = []

        def fake_get(url, params=None):
            calls.append(params)
            return SimpleNamespace(text=pages.get((url.rsplit("/", 1)[1], params.get("page")), ""))

        monkeypatch.setattr(scraper, "_get", fake_get)
        jobs = scraper._fetch_jobs("AI Engineer OR Data Scientist", "Germany")

        assert sorted(j["external_id"] for j in jobs) == ["a1", "d1", "shared"]
        # Paging stops at the first empty page of each term
        assert len(calls) == 4
        assert all(p["sort"] == "date" for p in calls)
