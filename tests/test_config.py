"""Tests for src/config.py"""
import os
import pytest

import src.config as config_module


def _patch_env(monkeypatch, overrides: dict):
    required = {
        "N8N_USER": "admin",
        "N8N_PASSWORD": "secret",
        "GOOGLE_AI_API_KEY": "gemini-key",
        "ANTHROPIC_API_KEY": "anthropic-key",
        "DISCORD_BOT_TOKEN": "Bot.abc123",
        "DISCORD_CHANNEL_ID": "123456789",
        "NOTION_API_TOKEN": "notion-token",
        "NOTION_JOB_TRACKER_DB_ID": "db1",
        "NOTION_FEEDBACK_LOG_DB_ID": "db2",
    }
    required.update(overrides)
    for k, v in required.items():
        monkeypatch.setenv(k, v)
    config_module.reset()


class TestConfigLoad:
    def test_happy_path(self, monkeypatch):
        _patch_env(monkeypatch, {})
        cfg = config_module.load()
        assert cfg.n8n.user == "admin"
        assert cfg.gemini.api_key == "gemini-key"
        assert cfg.discord.channel_id == "123456789"
        assert cfg.tier1_provider == "gemini"

    def test_missing_required_raises(self, monkeypatch):
        _patch_env(monkeypatch, {})
        monkeypatch.delenv("GOOGLE_AI_API_KEY")
        config_module.reset()
        with pytest.raises(EnvironmentError, match="GOOGLE_AI_API_KEY"):
            config_module.load()

    def test_invalid_provider_raises(self, monkeypatch):
        _patch_env(monkeypatch, {"LLM_TIER1_PROVIDER": "openai"})
        with pytest.raises(EnvironmentError, match="LLM_TIER1_PROVIDER"):
            config_module.load()

    def test_score_threshold_default(self, monkeypatch):
        _patch_env(monkeypatch, {})
        cfg = config_module.load()
        assert cfg.score_threshold == pytest.approx(6.0)

    def test_score_threshold_override(self, monkeypatch):
        _patch_env(monkeypatch, {"SCORE_THRESHOLD": "7.5"})
        cfg = config_module.load()
        assert cfg.score_threshold == pytest.approx(7.5)
