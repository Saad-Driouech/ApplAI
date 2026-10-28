"""Tests for Tier-1 client quota and retry handling (no network calls)."""
from types import SimpleNamespace
from unittest import mock

import pytest
from tenacity import wait_none

import src.matching.gemini_client as gc
from src.matching.gemini_client import BudgetExceeded, GeminiClient


class _ApiError(Exception):
    """Mimics google-genai's APIError, which carries the HTTP status in .code."""

    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(gc, "_SHARED_LIMITERS", {})
    monkeypatch.setattr(GeminiClient._generate.retry, "wait", wait_none())


def _client(side_effect, rpd=500, model="m"):
    client = GeminiClient(api_key="fake", model=model, rpm=100, rpd=rpd)
    client._client = mock.Mock()
    client._client.models.generate_content.side_effect = side_effect
    return client


def _calls(client):
    return client._client.models.generate_content.call_count


class TestGeminiQuota:
    def test_local_budget_exhausted_is_not_retried(self):
        client = _client([SimpleNamespace(text="ok")], rpd=0)
        with pytest.raises(BudgetExceeded):
            client._call("prompt")
        assert _calls(client) == 0

    def test_persistent_429_becomes_budget_exceeded_after_retries(self):
        client = _client(_ApiError(429))
        with pytest.raises(BudgetExceeded):
            client._call("prompt")
        assert _calls(client) == 3

    def test_transient_429_recovers(self):
        client = _client([_ApiError(429), SimpleNamespace(text="ok")])
        assert client._call("prompt") == "ok"

    def test_server_error_retried_then_raised_as_is(self):
        client = _client(_ApiError(503))
        with pytest.raises(_ApiError):
            client._call("prompt")
        assert _calls(client) == 3

    def test_client_error_not_retried(self):
        client = _client(_ApiError(400))
        with pytest.raises(_ApiError):
            client._call("prompt")
        assert _calls(client) == 1

    def test_daily_budget_shared_across_instances(self):
        # phase_score builds a new client every run — the budget must persist
        first = _client([SimpleNamespace(text="ok")], rpd=1)
        first._call("prompt")
        second = _client([SimpleNamespace(text="ok")], rpd=1)
        with pytest.raises(BudgetExceeded):
            second._call("prompt")

    def test_budgets_are_per_model(self):
        _client([SimpleNamespace(text="ok")], rpd=1, model="a")._call("prompt")
        assert _client([SimpleNamespace(text="ok")], rpd=1, model="b")._call("prompt") == "ok"


class TestPersistentDailyBudget:
    @pytest.fixture
    def usage_db(self, tmp_path):
        import src.database as db
        path = tmp_path / "usage.db"
        db.init_db(path)
        return path

    def test_daily_count_survives_restart(self, usage_db):
        gc.shared_limiter("Gemini", "m", rpm=100, rpd=1, usage_db=usage_db).acquire()
        gc._SHARED_LIMITERS.clear()  # simulate a process restart
        with pytest.raises(BudgetExceeded):
            gc.shared_limiter("Gemini", "m", rpm=100, rpd=1, usage_db=usage_db).acquire()

    def test_budget_resets_on_new_quota_day(self, usage_db, monkeypatch):
        limiter = gc.shared_limiter("Gemini", "m", rpm=100, rpd=1, usage_db=usage_db)
        limiter.acquire()
        monkeypatch.setattr(limiter, "_today", lambda: "2099-01-01")
        limiter.acquire()  # new calendar day, fresh budget
        assert limiter.daily_remaining == 0

    def test_gemini_day_follows_pacific_time(self, usage_db):
        assert str(gc.shared_limiter("Gemini", "m", rpm=1, rpd=1, usage_db=usage_db)._tz) == "America/Los_Angeles"
        assert str(gc.shared_limiter("Groq", "m", rpm=1, rpd=1, usage_db=usage_db)._tz) == "UTC"
