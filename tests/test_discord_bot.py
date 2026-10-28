"""Tests for Discord payload construction (no network calls)."""
from src.delivery.discord_bot import DiscordDelivery, _field_value


def test_field_values_within_discord_limits():
    embed = DiscordDelivery._build_embed(
        {"title": "", "company": "Acme", "country": "DE", "source_url": "https://x.com/" + "a" * 2000},
        score=7.0, reasoning="",
    )
    values = [f["value"] for f in embed["fields"]]
    assert all(1 <= len(v) <= 1024 for v in values)
    assert values[0] == "—"


def test_field_value_truncates_with_ellipsis():
    assert _field_value("x" * 300, limit=256) == "x" * 255 + "…"
