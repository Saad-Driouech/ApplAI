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
