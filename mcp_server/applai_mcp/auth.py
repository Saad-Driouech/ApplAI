"""
Bearer-token check for the HTTP transport. WorkOS AuthKit is the authorization
server; this server only verifies its RS256 access tokens (issuer, audience =
our /mcp URL, expiry) and admits a single user. Any failure, including an
unreachable JWKS endpoint, rejects the request.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from typing import Any

import anyio
import httpx2
import jwt
from mcp.server.auth.provider import AccessToken

from applai_mcp.config import HttpSettings

log = logging.getLogger(__name__)

LEEWAY_SECONDS = 30
REFETCH_SECONDS = 60  # at most one JWKS request per minute, even for unknown key ids
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub"]


class JwtTokenVerifier:
    def __init__(self, http: HttpSettings, transport: httpx2.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._issuer = http.issuer
        self._audience = http.resource_url
        self._allowed_sub = http.allowed_user_id
        self._jwks_url = f"{http.issuer}/oauth2/jwks"
        self._ttl = http.jwks_cache_seconds
        self._transport = transport
        self._clock = clock
        self._lock = anyio.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_expire = -math.inf
        self._last_fetch = -math.inf

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await self._decode(token)
        except Exception as exc:  # fail closed on anything unexpected
            log.warning("Rejected bearer token: %s: %s", type(exc).__name__, exc)
            return None
        if claims["sub"] != self._allowed_sub:
            log.warning("Rejected bearer token for user %r: not the allowed user", claims["sub"])
            return None
        scope = claims.get("scope")
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("azp") or claims["sub"]),
            scopes=scope.split() if isinstance(scope, str) else [],
            expires_at=int(claims["exp"]),
            resource=self._audience,
            subject=claims["sub"],
            claims={"iss": claims["iss"]},
        )

    async def _decode(self, token: str) -> dict[str, Any]:
        kid = jwt.get_unverified_header(token).get("kid")
        if not isinstance(kid, str):
            raise jwt.InvalidTokenError("token header has no key id")
        key = await self._signing_key(kid)
        return jwt.decode(token, key, algorithms=["RS256"], issuer=self._issuer, audience=self._audience,
                          leeway=LEEWAY_SECONDS, options={"require": REQUIRED_CLAIMS})

    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            now = self._clock()
            stale = now >= self._keys_expire or kid not in self._keys
            if stale and now - self._last_fetch >= REFETCH_SECONDS:
                self._last_fetch = now
                await self._fetch(now)
            if now >= self._keys_expire:
                raise jwt.InvalidTokenError("signing keys unavailable")
            key = self._keys.get(kid)
        if key is None:
            raise jwt.InvalidTokenError(f"unknown signing key {kid!r}")
        return key

    async def _fetch(self, now: float) -> None:
        async with httpx2.AsyncClient(timeout=10, transport=self._transport) as client:
            resp = await client.get(self._jwks_url)
        resp.raise_for_status()
        keys = jwt.PyJWKSet.from_dict(resp.json()).keys
        self._keys = {k.key_id: k for k in keys if k.key_id}
        self._keys_expire = now + self._ttl
