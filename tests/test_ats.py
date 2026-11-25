"""Tests for the ATS board adapters, board detection and the watchlist scraper."""
import json

import httpx
import pytest

import src.database as db
from src.scrapers import ats
from src.scrapers.ats import AtsError, Candidate, boards_in, detect_ats, fetch_board, guess_slugs
from src.scrapers.location import infer_country

ML_HTML = "<p>Train PyTorch models for our LLM platform.</p>"


def fake_get(routes: dict):
    """A Get that serves routes {url: json-able | str | Exception} and 404s for anything else."""
    calls = []

    def get(url, params=None, **kwargs):
        calls.append((url, params))
        body = routes.get(url)
        request = httpx.Request("GET", url)
        if body is None:
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        if isinstance(body, Exception):
            raise body
        content = body if isinstance(body, str) else json.dumps(body)
        return httpx.Response(200, content=content.encode(), request=request)

    get.calls = calls
    return get


class TestInferCountry:
    @pytest.mark.parametrize("location,remote,expected", [
        ("Munich - London", False, "DE"), ("London", False, "XX"), ("Lisbon", False, "XX"),
        ("Karlsruhe", False, "DE"), ("Remote - Germany", False, "DE"), ("Remote", False, "REMOTE"),
        ("Berlin", True, "DE"), ("Remote (US)", False, "XX"), ("", False, "XX"),
    ])
    def test_unknown_places_are_not_assumed_german(self, location, remote, expected):
        assert infer_country(location, remote) == expected


class TestAdapters:
    def test_greenhouse(self):
        get = fake_get({"https://boards-api.greenhouse.io/v1/boards/helsing/jobs": {"jobs": [
            {"id": 1, "title": "ML Engineer", "absolute_url": "https://helsing.ai/jobs/1", "location": {"name": "Munich - London"},
             "content": "&lt;p&gt;Train PyTorch models&lt;/p&gt;", "first_published": "2026-11-01T10:00:00-04:00"},
            {"id": 2, "title": "Accountant", "absolute_url": "https://helsing.ai/jobs/2", "location": {"name": "London"}},
        ]}})
        jobs = fetch_board(get, "greenhouse", "helsing")
        assert jobs[0]["external_id"] == "1" and jobs[0]["country"] == "DE" and jobs[0]["city"] == "Munich - London"
        assert jobs[0]["source_url"] == "https://helsing.ai/jobs/1" and "PyTorch" in jobs[0]["description"]
        assert jobs[1]["country"] == "XX"
        assert get.calls[0][1] == {"content": "true"}

    def test_lever_falls_back_to_the_eu_host(self):
        get = fake_get({"https://api.eu.lever.co/v0/postings/acme": [
            {"id": "a", "text": "AI Engineer", "hostedUrl": "https://jobs.eu.lever.co/acme/a", "createdAt": 1763985600000,
             "country": "NL", "workplaceType": "onsite", "categories": {"location": "Amsterdam", "allLocations": ["Amsterdam"]},
             "description": "<p>Intro</p>", "lists": [{"text": "What you do", "content": "<li>Build RAG</li>"}],
             "additional": "<p>Benefits</p>"},
        ]})
        [job] = fetch_board(get, "lever", "acme")
        assert job["country"] == "NL" and job["city"] == "Amsterdam"
        assert "What you do" in job["description"] and "Build RAG" in job["description"]
        assert job["posted_at"].startswith("2025-11-24")
        assert [c[0] for c in get.calls] == ["https://api.lever.co/v0/postings/acme", "https://api.eu.lever.co/v0/postings/acme"]

    def test_ashby_prefers_a_target_country_and_skips_unlisted(self):
        get = fake_get({"https://api.ashbyhq.com/posting-api/job-board/n8n": {"jobs": [
            {"id": "x", "title": "AI Engineer", "jobUrl": "https://jobs.ashbyhq.com/n8n/x", "isRemote": True, "isListed": True,
             "location": "Spain", "address": {"postalAddress": {"addressCountry": "Spain"}},
             "secondaryLocations": [{"location": "Germany", "address": {"postalAddress": {"addressCountry": "Germany"}}}],
             "descriptionHtml": ML_HTML, "publishedAt": "2026-11-20T09:00:00Z"},
            {"id": "hidden", "title": "Hidden", "isListed": False},
        ]}})
        [job] = fetch_board(get, "ashby", "n8n")
        assert job["country"] == "DE" and job["city"] == "Remote · Germany"

    def test_smartrecruiters_fetches_details_only_for_relevant_titles(self):
        base = "https://api.smartrecruiters.com/v1/companies/deliveryhero/postings"
        get = fake_get({
            base: {"totalFound": 2, "content": [
                {"id": "1", "name": "Machine Learning Engineer", "releasedDate": "2026-11-24T18:31:20Z",
                 "location": {"city": "Berlin", "country": "de", "remote": False, "fullLocation": "Berlin, Germany"}},
                {"id": "2", "name": "Sales Executive", "location": {"city": "San Salvador", "country": "sv"}},
            ]},
            f"{base}/1": {"postingUrl": "https://jobs.smartrecruiters.com/DeliveryHero/1-ml",
                          "jobAd": {"sections": {"jobDescription": {"title": "Job", "text": ML_HTML}}}},
        })
        ml, sales = fetch_board(get, "smartrecruiters", "deliveryhero")
        assert ml["country"] == "DE" and "PyTorch" in ml["description"]
        assert ml["source_url"] == "https://jobs.smartrecruiters.com/DeliveryHero/1-ml"
        assert sales["country"] == "SV" and sales["description"] == ""
        assert f"{base}/2" not in [c[0] for c in get.calls]

    def test_personio_xml(self):
        xml = """<?xml version="1.0" encoding="UTF-8"?><workzag-jobs><position><id>42</id><office>Berlin</office>
            <additionalOffices><office>Frankfurt</office></additionalOffices><name>AI Engineer (all genders)</name>
            <createdAt>2026-11-01T08:00:00+00:00</createdAt><jobDescriptions><jobDescription><name>Your mission</name>
            <value><![CDATA[<p>Build LLM systems</p>]]></value></jobDescription></jobDescriptions></position></workzag-jobs>"""
        get = fake_get({"https://dida.jobs.personio.de/xml": xml})
        [job] = fetch_board(get, "personio", "dida")
        assert job["external_id"] == "42" and job["city"] == "Berlin" and job["country"] == "DE"
        assert job["source_url"] == "https://dida.jobs.personio.de/job/42" and "Build LLM systems" in job["description"]

    def test_personio_rejects_entities(self):
        xml = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "boom">]><workzag-jobs><position><name>&e;</name></position></workzag-jobs>'
        with pytest.raises(Exception):
            fetch_board(fake_get({"https://dida.jobs.personio.de/xml": xml}), "personio", "dida")

    def test_recruitee_and_workable(self):
        get = fake_get({
            "https://wetteronline.recruitee.com/api/offers/": {"offers": [
                {"id": 7, "title": "Data Scientist", "location": "Bonn, Deutschland", "country_code": "DE", "remote": False,
                 "careers_url": "https://wetteronline.recruitee.com/o/ds", "description": "<p>a</p>", "requirements": "<p>b</p>",
                 "published_at": "2026-09-29 08:29:44 UTC"}]},
            "https://apply.workable.com/api/v1/widget/accounts/acme": {"jobs": [
                {"shortcode": "ABC", "title": "ML Engineer", "url": "https://apply.workable.com/j/ABC", "telecommuting": True,
                 "locations": [{"city": "", "country": "Spain", "countryCode": "ES"}], "description": ML_HTML,
                 "published_on": "2026-10-13"}]},
        })
        [rec] = fetch_board(get, "recruitee", "wetteronline")
        assert rec["country"] == "DE" and rec["description"] == "<p>a</p><p>b</p>"
        [wk] = fetch_board(get, "workable", "acme")
        assert wk["country"] == "ES" and wk["city"].startswith("Remote")

    @pytest.mark.parametrize("ats_name,slug", [("greenhouse", "evil.com/#"), ("personio", "a.b"), ("nope", "acme"), ("lever", "")])
    def test_rejects_unknown_ats_and_unsafe_slugs(self, ats_name, slug):
        with pytest.raises(AtsError):
            fetch_board(fake_get({}), ats_name, slug)


class TestDetection:
    @pytest.mark.parametrize("text,expected", [
        ("https://job-boards.greenhouse.io/helsing/jobs/123", [("greenhouse", "helsing")]),
        ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=databricks">', [("greenhouse", "databricks")]),
        ("https://jobs.eu.lever.co/acme/abc", [("lever", "acme")]),
        ("https://jobs.ashbyhq.com/deepl/embed?version=2", [("ashby", "deepl")]),
        ('<a href="https://dida.jobs.personio.de/job/1">', [("personio", "dida")]),
        ("https://wetteronline.recruitee.com/o/x", [("recruitee", "wetteronline")]),
        ("https://apply.workable.com/acme/j/ABC", [("workable", "acme")]),
        ("https://apply.workable.com/j/ABC", []),
        ("https://careers.smartrecruiters.com/DeliveryHero", [("smartrecruiters", "DeliveryHero")]),
    ])
    def test_boards_in(self, text, expected):
        assert boards_in(text) == expected

    def test_guess_slugs(self):
        assert guess_slugs("iba AG") == ["ibaag", "iba-ag", "iba"]
        assert guess_slugs("Munich Electrification") == ["munichelectrification", "munich-electrification"]
        assert guess_slugs("Mueller's Solutions") == ["muellerssolutions", "muellers-solutions"]
        assert guess_slugs("DeepL") == ["deepl"]

    def test_missing_board_is_not_an_error_but_a_failed_read_is(self):
        get = fake_get({"https://api.ashbyhq.com/posting-api/job-board/down": httpx.ConnectTimeout("timed out")})
        missing = ats.probe(get, Candidate("ashby", "gone", "guess"))
        failed = ats.probe(get, Candidate("ashby", "down", "guess"))
        assert (missing.job_count, missing.error) == (0, None)
        assert failed.error.startswith("ConnectTimeout")

    def test_board_client_retries_busy_servers(self, monkeypatch):
        replies = iter([httpx.Response(503), httpx.Response(200, json={"jobs": []})])
        transport, real_client = httpx.MockTransport(lambda request: next(replies)), httpx.Client
        monkeypatch.setattr(ats.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
        monkeypatch.setattr(ats.time, "sleep", lambda s: None)
        with ats.board_client() as get:
            assert get("https://api.ashbyhq.com/posting-api/job-board/x").json() == {"jobs": []}

    def test_careers_url_link_wins_without_guessing(self):
        get = fake_get({"https://api.ashbyhq.com/posting-api/job-board/deepl": {"jobs": [
            {"id": "1", "title": "Sales Manager"}, {"id": "2", "title": "Research Scientist, LLMs"}]}})
        [c] = detect_ats(get, "DeepL", "https://jobs.ashbyhq.com/deepl")
        assert (c.ats, c.slug, c.found_by, c.job_count) == ("ashby", "deepl", "url", 2)
        assert c.sample_titles[0] == "Research Scientist, LLMs"
        assert len(get.calls) == 1

    def test_careers_page_is_scanned_for_embedded_boards(self):
        get = fake_get({"https://boards-api.greenhouse.io/v1/boards/helsing/jobs": {"jobs": [{"id": 1, "title": "ML Engineer"}]}})
        page = '<div id="grnhse_app"></div><script src="https://boards.greenhouse.io/embed/job_board/js?for=helsing"></script>'
        [c] = detect_ats(get, "Helsing", "https://helsing.ai/careers", download=lambda url: (url, page))
        assert (c.ats, c.slug, c.found_by) == ("greenhouse", "helsing", "page")

    def test_name_guesses_keep_only_boards_with_jobs(self):
        get = fake_get({
            "https://api.ashbyhq.com/posting-api/job-board/telli": {"jobs": [{"id": "1", "title": "AI Engineer"}]},
            "https://api.smartrecruiters.com/v1/companies/telli/postings": {"totalFound": 0, "content": []},
        })
        candidates = detect_ats(get, "telli")
        assert [(c.ats, c.slug, c.found_by) for c in candidates] == [("ashby", "telli", "guess")]


class TestSeedWatchlist:
    def test_reads_company_folders_and_suggestions(self, tmp_path):
        from src.scrapers.seed_watchlist import folder_companies, suggested_companies, unique_companies

        (tmp_path / "germany" / "Delivery_Hero").mkdir(parents=True)
        (tmp_path / "germany" / "Helsing").mkdir()
        (tmp_path / "germany" / "Cover_Letter.docx").write_text("x")
        (tmp_path / "resumes" / "tex").mkdir(parents=True)
        suggestions = tmp_path / "s.txt"
        suggestions.write_text("# comment\n\nHelsing | https://helsing.ai/careers\nDeepL\n")

        companies = folder_companies(tmp_path, ["germany", "uae"]) + suggested_companies(suggestions)
        unique = unique_companies(companies)

        assert [(c["name"], c["careers_url"], c["source"]) for c in unique] == [
            ("Delivery Hero", None, "folder:germany"),
            ("Helsing", "https://helsing.ai/careers", "folder:germany"),
            ("DeepL", None, "suggestion"),
        ]

    def test_watch_defaults_trust_links_over_guesses(self, monkeypatch):
        from src.scrapers import seed_watchlist

        results = {
            "Helsing": [Candidate("greenhouse", "helsing", "page", 176), Candidate("lever", "helsing", "guess", 2)],
            "telli": [Candidate("ashby", "telli", "guess", 14)],
            "Amazon": [Candidate("personio", "amazon", "guess", 3), Candidate("workable", "amazon", "guess", 1)],
            "SAP": [],
        }
        monkeypatch.setattr(seed_watchlist.ats, "detect_ats", lambda get, name, url, download=None: results[name])
        rows = {(r["name"], r["ats"]): r["watch"] for name in results
                for r in seed_watchlist._detect("get", {"name": name, "careers_url": None, "source": "x"})}
        assert rows == {
            ("Helsing", "greenhouse"): True, ("Helsing", "lever"): False,
            ("telli", "ashby"): True,
            ("Amazon", "personio"): False, ("Amazon", "workable"): False,
            ("SAP", None): False,
        }

    def test_apply_watches_only_marked_rows(self, tmp_path):
        from src.scrapers.seed_watchlist import apply_report

        path = tmp_path / "s.db"
        db.init_db(path)
        rows = [
            {"name": "Helsing", "ats": "greenhouse", "slug": "helsing", "watch": True, "careers_url": None},
            {"name": "Amazon", "ats": "personio", "slug": "amazon", "watch": False},
            {"name": "SAP", "ats": None, "slug": None, "watch": False},
        ]
        assert apply_report(path, rows) == (1, 0)
        assert apply_report(path, rows) == (0, 1)
        with pytest.raises(AtsError):
            apply_report(path, [{"name": "X", "ats": "personio", "slug": "evil.com/#", "watch": True}])


class TestWatchlistScraper:
    def _scraper(self, tmp_path):
        from src.scrapers.watchlist import WatchlistScraper

        path = tmp_path / "w.db"
        db.init_db(path)
        with db.get_conn(path) as conn:
            db.add_watched_company(conn, "Helsing", "greenhouse", "helsing")
            db.add_watched_company(conn, "Gone", "lever", "gone")
        return WatchlistScraper(path), path

    def test_keeps_relevant_jobs_and_records_each_board(self, tmp_path, monkeypatch):
        scraper, path = self._scraper(tmp_path)
        get = fake_get({"https://boards-api.greenhouse.io/v1/boards/helsing/jobs": {"jobs": [
            {"id": 1, "title": "ML Engineer", "absolute_url": "https://helsing.ai/jobs/1", "location": {"name": "Munich"}, "content": ML_HTML},
            # AI-company boilerplate in every description must not make an accountant relevant
            {"id": 2, "title": "Accountant", "absolute_url": "https://helsing.ai/jobs/2", "location": {"name": "Munich"}, "content": ML_HTML},
            {"id": 3, "title": "Staff ML Engineer", "absolute_url": "https://helsing.ai/jobs/3", "location": {"name": "Munich"}, "content": ML_HTML},
        ]}})
        monkeypatch.setattr(scraper, "_get", get)

        [job] = scraper.run("ignored", "Germany")

        assert job["external_id"] == "greenhouse:helsing:1" and job["company"] == "Helsing" and job["source"] == "ats"
        with db.get_conn(path) as conn:
            rows = {r["slug"]: dict(r) for r in db.list_watched_companies(conn)}
            run = conn.execute("SELECT error, jobs_new FROM scrape_runs").fetchone()
        assert (rows["helsing"]["last_job_count"], rows["helsing"]["last_relevant_count"]) == (3, 1)
        assert rows["gone"]["last_error"].startswith("HTTPStatusError") and rows["helsing"]["last_error"] is None
        assert (run["error"], run["jobs_new"]) == (None, 1)

    def test_run_fails_only_when_every_board_fails(self, tmp_path, monkeypatch):
        scraper, path = self._scraper(tmp_path)
        monkeypatch.setattr(scraper, "_get", fake_get({}))
        assert scraper.run("ignored", "Germany") == []
        with db.get_conn(path) as conn:
            assert "All 2 watched boards failed" in conn.execute("SELECT error FROM scrape_runs").fetchone()["error"]
