"""Tests for the company watchlist API routes (/watchlist*)."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import src.api as api
import src.database as db
from src.scrapers import ats

KEY = {"X-API-Key": "test-key"}
BOARDS = {
    ("greenhouse", "helsing"): [{"title": "Accountant"}, {"title": "AI Research Engineer"}],
    ("ashby", "empty"): [],
}


@pytest.fixture
def cfg(tmp_path):
    db.init_db(tmp_path / "t.db")
    return SimpleNamespace(api_key="test-key", paths=SimpleNamespace(db_path=tmp_path / "t.db"))  # pragma: allowlist secret


@pytest.fixture
def client(cfg, monkeypatch):
    @contextmanager
    def board_client(timeout=10.0):
        yield "get"

    def fetch_board(get, ats_name, slug, details=True):
        if (ats_name, slug) not in BOARDS:
            raise RuntimeError("404 Not Found")
        return BOARDS[(ats_name, slug)]

    monkeypatch.setattr(api, "_cfg", cfg)
    monkeypatch.setattr(ats, "board_client", board_client)
    monkeypatch.setattr(ats, "fetch_board", fetch_board)
    return TestClient(api.app)


def test_routes_require_the_api_key(client):
    assert client.get("/watchlist").status_code == 401


def test_detect_returns_board_candidates(client, monkeypatch):
    seen = {}

    def detect(get, name, careers_url=None, download=None):
        seen.update(name=name, careers_url=careers_url, download=download)
        return [ats.Candidate("greenhouse", "helsing", "page", 2, ["AI Research Engineer", "Accountant"])]

    monkeypatch.setattr(ats, "detect_ats", detect)
    resp = client.post("/watchlist/detect", json={"name": "Helsing", "careers_url": "https://helsing.ai/careers"}, headers=KEY)

    assert resp.status_code == 200
    [c] = resp.json()["candidates"]
    assert (c["ats"], c["slug"], c["found_by"], c["job_count"]) == ("greenhouse", "helsing", "page", 2)
    # The careers page is fetched with the SSRF-checked downloader
    assert seen["download"] is api._download


@pytest.mark.parametrize("body", [
    {"name": "X", "careers_url": "http://insecure.example"},
    {"name": ""},
])
def test_detect_validates_input(client, body):
    assert client.post("/watchlist/detect", json=body, headers=KEY).status_code == 422


def test_watch_list_and_unwatch(client):
    resp = client.post("/watchlist", json={"name": "Helsing", "ats": "greenhouse", "slug": "helsing"}, headers=KEY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["added"] is True and body["job_count"] == 2
    assert body["sample_titles"][0] == "AI Research Engineer"
    company_id = body["company"]["id"]

    again = client.post("/watchlist", json={"name": "Helsing", "ats": "greenhouse", "slug": "helsing"}, headers=KEY)
    assert again.json()["added"] is False

    listed = client.get("/watchlist", headers=KEY).json()
    assert listed["count"] == 1 and listed["companies"][0]["active"] is True

    removed = client.delete(f"/watchlist/{company_id}", headers=KEY)
    assert removed.status_code == 200 and removed.json()["active"] is False
    assert client.get("/watchlist", headers=KEY).json()["count"] == 0
    assert client.get("/watchlist?include_inactive=true", headers=KEY).json()["count"] == 1
    assert client.delete("/watchlist/999", headers=KEY).status_code == 404


@pytest.mark.parametrize("body,detail", [
    ({"name": "Empty", "ats": "ashby", "slug": "empty"}, "no readable jobs"),
    ({"name": "Gone", "ats": "lever", "slug": "gone"}, "404 Not Found"),
])
def test_watch_rejects_unreadable_or_empty_boards(client, body, detail):
    resp = client.post("/watchlist", json=body, headers=KEY)
    assert resp.status_code == 422 and detail in resp.json()["detail"]


@pytest.mark.parametrize("body", [
    {"name": "X", "ats": "taleo", "slug": "x"},
    {"name": "X", "ats": "personio", "slug": "evil.com/#"},
])
def test_watch_validates_ats_and_slug(client, body):
    assert client.post("/watchlist", json=body, headers=KEY).status_code == 422
