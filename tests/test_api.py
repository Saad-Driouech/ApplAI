"""Tests for API authentication and the Discord interactions webhook."""
import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

import src.api as api
import src.database as db
from src.utils.locks import PhaseBusy

_SIGNING_KEY = Ed25519PrivateKey.generate()
_PUBLIC_HEX = _SIGNING_KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db.init_db(tmp_path / "t.db")
    monkeypatch.setattr(api, "_cfg", SimpleNamespace(
        api_key="test-key",
        paths=SimpleNamespace(db_path=tmp_path / "t.db", working_dir=tmp_path),
        discord=SimpleNamespace(public_key=_PUBLIC_HEX, allowed_user_ids=frozenset(), bot_token="x", channel_id="1"),
    ))
    return TestClient(api.app)


def _signed(payload: dict, timestamp: int | None = None) -> dict:
    body = json.dumps(payload).encode()
    ts = str(int(time.time()) if timestamp is None else timestamp)
    signature = _SIGNING_KEY.sign(ts.encode() + body).hex()
    return {"content": body, "headers": {"X-Signature-Ed25519": signature, "X-Signature-Timestamp": ts}}


class TestApiKey:
    def test_health_is_public(self, client):
        assert client.get("/health").status_code == 200

    @pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
    def test_missing_or_wrong_key_rejected(self, client, headers):
        assert client.get("/stats", headers=headers).status_code == 401
        assert client.post("/generate", headers=headers).status_code == 401

    def test_valid_key_accepted(self, client):
        assert client.get("/stats", headers={"X-API-Key": "test-key"}).status_code == 200

    def test_unconfigured_key_fails_closed(self, client):
        api._cfg.api_key = ""
        assert client.get("/stats", headers={"X-API-Key": ""}).status_code == 503

    def test_busy_phase_returns_409(self, client):
        with mock.patch.object(api, "phase_score", side_effect=PhaseBusy("busy")):
            assert client.post("/score", headers={"X-API-Key": "test-key"}).status_code == 409


class TestDiscordInteractions:
    def test_valid_ping(self, client):
        assert client.post("/discord/interactions", **_signed({"type": 1})).json() == {"type": 1}

    def test_bad_signature_rejected(self, client):
        request = _signed({"type": 1})
        request["headers"]["X-Signature-Ed25519"] = "00" * 64
        assert client.post("/discord/interactions", **request).status_code == 401

    def test_replayed_old_request_rejected(self, client):
        stale = _signed({"type": 1}, timestamp=int(time.time()) - 600)
        assert client.post("/discord/interactions", **stale).status_code == 401

    def test_tampered_body_rejected(self, client):
        request = _signed({"type": 1})
        request["content"] = json.dumps({"type": 3}).encode()
        assert client.post("/discord/interactions", **request).status_code == 401

    def test_button_click_scheduled(self, client):
        click = {"type": 3, "data": {"custom_id": "approve_a1"}, "message": {"id": "m1"},
                 "member": {"user": {"id": "42"}}}
        with mock.patch.object(api, "_process_decision") as process:
            assert client.post("/discord/interactions", **_signed(click)).json() == {"type": 6}
        process.assert_called_once_with("a1", "approved", "m1")

    def test_disallowed_user_gets_ephemeral_refusal(self, client):
        api._cfg.discord.allowed_user_ids = frozenset({"42"})
        click = {"type": 3, "data": {"custom_id": "approve_a1"}, "message": {"id": "m1"},
                 "member": {"user": {"id": "99"}}}
        with mock.patch.object(api, "_process_decision") as process:
            reply = client.post("/discord/interactions", **_signed(click)).json()
        assert reply["type"] == 4 and reply["data"]["flags"] == 64
        process.assert_not_called()
