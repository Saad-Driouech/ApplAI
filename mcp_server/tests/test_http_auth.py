"""HTTP transport: WorkOS JWT verification, discovery metadata, transport checks and the CLI entrypoint."""
import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from applai_mcp import __main__ as entrypoint
from applai_mcp.auth import JwtTokenVerifier
from applai_mcp.config import HttpSettings, Settings
from applai_mcp.server import create_server, http_app
from conftest import COMMIT, COMMITTED, DRAFT_ID, FakeApi, call, draft

pytestmark = pytest.mark.anyio

ISSUER = "https://auth.example.test"
PUBLIC = "https://mcp.example.test"
RESOURCE = f"{PUBLIC}/mcp"
USER = "user_01OWNER"
KID = "key-1"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}


def public_jwk(key: rsa.RSAPrivateKey, kid: str) -> dict:
    return {**json.loads(RSAAlgorithm.to_jwk(key.public_key())), "kid": kid, "alg": "RS256", "use": "sig"}


def claims(**overrides: Any) -> dict:
    """Valid claims; an override of None drops the claim."""
    now = int(time.time())
    base = {"iss": ISSUER, "aud": RESOURCE, "sub": USER, "iat": now, "exp": now + 300, "client_id": "client_01"}
    return {k: v for k, v in {**base, **overrides}.items() if v is not None}


def token(key: rsa.RSAPrivateKey = KEY, kid: str = KID, **overrides: Any) -> str:
    return jwt.encode(claims(**overrides), key, algorithm="RS256", headers={"kid": kid})


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def handmade(header: dict, signer=lambda signing_input: b"") -> str:
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims()).encode())}"
    return f"{signing_input}.{_b64(signer(signing_input.encode()))}"


def hs256_with_public_key(signing_input: bytes) -> bytes:
    pem = KEY.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hmac.new(pem, signing_input, hashlib.sha256).digest()


def tampered(tok: str) -> str:
    header, payload, sig = tok.split(".")
    forged = _b64(json.dumps(claims(sub="user_01ATTACKER")).encode())
    return f"{header}.{forged}.{sig}"


class FakeJwks:
    def __init__(self) -> None:
        self.response: Any = {"keys": [public_jwk(KEY, KID)]}
        self.requests = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == f"{ISSUER}/oauth2/jwks"
        self.requests += 1
        if isinstance(self.response, Exception):
            raise self.response
        if isinstance(self.response, int):
            return httpx2.Response(self.response, json={})
        return httpx2.Response(200, json=self.response)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def jwks() -> FakeJwks:
    return FakeJwks()


@pytest.fixture
def http_settings() -> HttpSettings:
    return HttpSettings(mcp_public_url=PUBLIC, authkit_issuer=ISSUER, allowed_user_id=USER, _env_file=None)


@pytest.fixture
async def app(settings: Settings, fake_api: FakeApi, http_settings: HttpSettings,
              jwks: FakeJwks) -> AsyncIterator[Starlette]:
    server = create_server(settings.model_copy(update={"confirmation_mode": "token"}),
                           transport=httpx2.MockTransport(fake_api.handler), http=http_settings,
                           jwks_transport=httpx2.MockTransport(jwks.handler))
    app = http_app(server, http_settings)
    async with app.router.lifespan_context(app):
        yield app


def http(app: Starlette, bearer: str | None = None, base_url: str = PUBLIC) -> httpx2.AsyncClient:
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url=base_url, headers=headers)


async def initialize(app: Starlette, bearer: str | None = None, **kwargs: Any) -> httpx2.Response:
    async with http(app, bearer, **kwargs) as client:
        return await client.post("/mcp", json=INIT, headers={"Accept": "application/json, text/event-stream"})


def mcp_client(http_client: httpx2.AsyncClient) -> Client:
    return Client(streamable_http_client(RESOURCE, http_client=http_client))


class TestDiscovery:
    async def test_no_token_gets_401_pointing_at_the_metadata(self, app):
        resp = await initialize(app)
        assert resp.status_code == 401
        challenge = resp.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"' in challenge

    async def test_protected_resource_metadata(self, app):
        async with http(app) as client:
            resp = await client.get("/.well-known/oauth-protected-resource/mcp")
        assert resp.status_code == 200
        meta = resp.json()
        assert meta["resource"] == RESOURCE and meta["authorization_servers"] == [ISSUER]

    async def test_healthz_is_open(self, app, jwks):
        async with http(app) as client:
            resp = await client.get("/healthz")
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}
        assert jwks.requests == 0


class TestAcceptedToken:
    async def test_tools_prompts_and_a_read_tool(self, app):
        async with http(app, token()) as http_client, mcp_client(http_client) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            prompts = {p.name for p in (await client.list_prompts()).prompts}
            stats = await call(client, "get_stats")
        assert {"get_scoring_context", "generate_application", "run_pipeline_phase"} <= tools
        assert {"apply_to_job", "daily_run"} <= prompts
        assert stats["pipeline_status_counts"]["new"] == 4

    async def test_keys_are_cached(self, app, jwks):
        for _ in range(3):
            assert (await initialize(app, token())).status_code == 200
        assert jwks.requests == 1

    async def test_confirmation_token_survives_a_new_session(self, app, fake_api):
        fake_api.routes[f"GET /drafts/{DRAFT_ID}"] = draft()
        fake_api.routes[COMMIT] = COMMITTED
        async with http(app, token()) as http_client, mcp_client(http_client) as client:
            first = await call(client, "generate_application", {"draft_id": DRAFT_ID})
        assert first["status"] == "confirmation_required" and fake_api.calls(COMMIT) == []
        async with http(app, token()) as http_client, mcp_client(http_client) as client:
            done = await call(client, "generate_application",
                              {"draft_id": DRAFT_ID, "confirmation_token": first["confirmation_token"]})
        assert done["status"] == "done" and len(fake_api.calls(COMMIT)) == 1


class TestRejectedToken:
    @pytest.mark.parametrize("bearer", [
        pytest.param(token(sub="user_01SOMEONE_ELSE"), id="other-user"),
        pytest.param(token(aud="https://other.example.test/mcp"), id="wrong-audience"),
        pytest.param(token(aud="client_01"), id="client-id-audience"),
        pytest.param(token(iss="https://evil.example.test"), id="wrong-issuer"),
        pytest.param(token(exp=int(time.time()) - 120), id="expired"),
        pytest.param(token(nbf=int(time.time()) + 600), id="not-yet-valid"),
        pytest.param(token(sub=None), id="missing-sub"),
        pytest.param(token(exp=None), id="missing-exp"),
        pytest.param(token(iat=None), id="missing-iat"),
        pytest.param(token(key=OTHER_KEY), id="signed-by-another-key"),
        pytest.param(tampered(token()), id="tampered-payload"),
        pytest.param(handmade({"alg": "none", "typ": "JWT", "kid": KID}), id="alg-none"),
        pytest.param(handmade({"alg": "HS256", "typ": "JWT", "kid": KID}, hs256_with_public_key),
                     id="hs256-with-public-key"),
        pytest.param(token(kid="unknown"), id="unknown-kid"),
        pytest.param(handmade({"alg": "RS256", "typ": "JWT"}), id="no-kid"),
        pytest.param("not-a-jwt", id="garbage"),
    ])
    async def test_rejected(self, app, fake_api, bearer):
        resp = await initialize(app, bearer)
        assert resp.status_code == 401
        assert 'error="invalid_token"' in resp.headers["www-authenticate"]
        assert fake_api.requests == []

    @pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer  ", "bearer-token"])
    async def test_malformed_authorization_header(self, app, header):
        async with http(app) as client:
            resp = await client.post("/mcp", json=INIT, headers={"Authorization": header,
                                                                 "Accept": "application/json, text/event-stream"})
        assert resp.status_code == 401

    @pytest.mark.parametrize("failure", [500, 404, httpx2.ConnectError("down"), {"keys": []}, ["not", "a", "set"]],
                             ids=["500", "404", "unreachable", "empty-set", "not-a-set"])
    async def test_jwks_unavailable_fails_closed(self, app, jwks, failure):
        jwks.response = failure
        assert (await initialize(app, token())).status_code == 401

    async def test_unknown_kid_refetches_at_most_once_a_minute(self, app, jwks):
        for _ in range(3):
            assert (await initialize(app, token(kid="rotated"))).status_code == 401
        assert jwks.requests == 1

    async def test_wrong_host_is_refused(self, app):
        resp = await initialize(app, token(), base_url="https://attacker.example.test")
        assert resp.status_code == 421

    async def test_foreign_origin_is_refused(self, app):
        async with http(app, token()) as client:
            resp = await client.post("/mcp", json=INIT, headers={"Origin": "https://attacker.example.test",
                                                                 "Accept": "application/json, text/event-stream"})
        assert resp.status_code == 403


class TestKeyCache:
    @pytest.fixture
    def clock(self) -> Clock:
        return Clock()

    @pytest.fixture
    def verifier(self, http_settings, jwks, clock) -> JwtTokenVerifier:
        return JwtTokenVerifier(http_settings, transport=httpx2.MockTransport(jwks.handler), clock=clock)

    async def test_rotated_key_is_picked_up_after_the_refetch_window(self, verifier, jwks, clock):
        assert await verifier.verify_token(token())
        jwks.response = {"keys": [public_jwk(OTHER_KEY, "key-2")]}
        assert await verifier.verify_token(token(OTHER_KEY, "key-2")) is None
        clock.now += 61
        access = await verifier.verify_token(token(OTHER_KEY, "key-2"))
        assert access and access.subject == USER and jwks.requests == 2

    async def test_expired_cache_is_not_used_when_jwks_is_down(self, verifier, jwks, http_settings, clock):
        assert await verifier.verify_token(token())
        jwks.response = httpx2.ConnectError("down")
        clock.now += http_settings.jwks_cache_seconds
        assert await verifier.verify_token(token()) is None

    async def test_access_token_fields(self, verifier):
        tok = token(scope="openid profile")
        access = await verifier.verify_token(tok)
        assert access.token == tok and access.client_id == "client_01" and access.scopes == ["openid", "profile"]
        assert access.resource == RESOURCE and access.subject == USER and access.claims == {"iss": ISSUER}
        assert access.expires_at == jwt.decode(tok, options={"verify_signature": False})["exp"]


class TestEntrypoint:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch, apps_dir):
        for cls in (Settings, HttpSettings):
            monkeypatch.setitem(cls.model_config, "env_file", None)  # never read the developer's .env
        for name in ("MCP_TRANSPORT", "MCP_PUBLIC_URL", "AUTHKIT_ISSUER", "ALLOWED_USER_ID", "HTTP_HOST", "HTTP_PORT"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("APPLAI_API_KEY", "test-key")  # pragma: allowlist secret
        monkeypatch.setenv("APPLAI_APPLICATIONS_DIR", str(apps_dir))

    def test_stdio_needs_no_http_settings(self, monkeypatch):
        ran = []
        monkeypatch.setattr(entrypoint, "create_server", lambda settings, **kw: type(
            "S", (), {"run": lambda self: ran.append(kw)})())
        entrypoint.main([])
        assert ran == [{}]

    def test_http_without_settings_exits_with_the_missing_names(self):
        with pytest.raises(SystemExit) as exc:
            entrypoint.main(["--transport", "http"])
        assert "MCP_PUBLIC_URL" in str(exc.value) and "ALLOWED_USER_ID" in str(exc.value)

    def test_http_runs_uvicorn_without_trusting_proxy_headers(self, monkeypatch):
        monkeypatch.setenv("MCP_TRANSPORT", "http")
        monkeypatch.setenv("MCP_PUBLIC_URL", PUBLIC)
        monkeypatch.setenv("AUTHKIT_ISSUER", ISSUER)
        monkeypatch.setenv("ALLOWED_USER_ID", USER)
        monkeypatch.setenv("HTTP_HOST", "0.0.0.0")  # nosec B104 - container bind address
        runs = []
        monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kw: runs.append((app, kw)))
        entrypoint.main([])
        app, kwargs = runs[0]
        assert isinstance(app, Starlette)
        assert kwargs == {"host": "0.0.0.0", "port": 8080, "proxy_headers": False,  # nosec B104
                          "server_header": False}

    def test_invalid_transport_env(self, monkeypatch):
        monkeypatch.setenv("MCP_TRANSPORT", "sse")
        with pytest.raises(SystemExit) as exc:
            entrypoint.main([])
        assert exc.value.code == 2
