"""
Human-in-the-loop gate for write/outbound tools.

Clients that support elicitation get a confirmation prompt addressed to the
user (the model cannot answer it). Other clients get a two-step flow: the
first call returns a preview and a short-lived, single-use HMAC token bound
to the tool name and exact arguments; only a repeat call carrying it runs.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import AcceptedElicitation, Context, Elicit, ElicitationResult
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from applai_mcp.models import Gated, Preview


class Approval(BaseModel):
    approve: bool = Field(description="Approve this action")


class PendingConfirmation(BaseModel):
    preview: Preview
    token: str
    expires_at: datetime


def _canonical(tool: str, args: dict[str, Any]) -> bytes:
    return json.dumps([tool, args], sort_keys=True, separators=(",", ":"), default=str).encode()


class ConfirmationTokens:
    """Issues and redeems tokens of the form '<expiry>.<nonce>.<mac>'."""

    def __init__(self, ttl_seconds: int, clock: Callable[[], float] = time.time):
        self._key = secrets.token_bytes(32)  # per process: tokens die with the server
        self._ttl = ttl_seconds
        self._clock = clock
        self._redeemed: dict[str, float] = {}

    def _mac(self, tool: str, args: dict[str, Any], expiry: int, nonce: str) -> str:
        msg = _canonical(tool, args) + f"|{expiry}|{nonce}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    def issue(self, tool: str, args: dict[str, Any]) -> tuple[str, datetime]:
        expiry = int(self._clock()) + self._ttl
        nonce = secrets.token_hex(8)
        token = f"{expiry}.{nonce}.{self._mac(tool, args, expiry, nonce)}"
        return token, datetime.fromtimestamp(expiry, timezone.utc)

    def redeem(self, tool: str, args: dict[str, Any], token: str) -> None:
        """Raise ToolError unless the token is valid for exactly these arguments; consume it."""
        now = self._clock()
        self._redeemed = {n: exp for n, exp in self._redeemed.items() if exp > now}
        try:
            expiry_text, nonce, mac = token.split(".")
            expiry = int(expiry_text)
        except ValueError:
            raise ToolError("Malformed confirmation_token; call again without it to get a new preview") from None
        if not hmac.compare_digest(mac, self._mac(tool, args, expiry, nonce)):
            raise ToolError("confirmation_token does not match these arguments; call again without it "
                            "to preview the changed request")
        if expiry <= now:
            raise ToolError("confirmation_token expired; call again without it to get a new preview")
        if nonce in self._redeemed:
            raise ToolError("confirmation_token was already used; call again without it to get a new preview")
        self._redeemed[nonce] = expiry


class PreviewCache:
    """Previews keyed by tool + arguments, so resolver re-runs and token redemption don't recompute them."""

    def __init__(self, ttl_seconds: int, clock: Callable[[], float] = time.time):
        self._ttl = ttl_seconds
        self._clock = clock
        self._items: dict[bytes, tuple[float, Preview]] = {}

    async def get(self, tool: str, args: dict[str, Any], build: Callable[[], Awaitable[Preview]]) -> Preview:
        now = self._clock()
        self._items = {k: v for k, v in self._items.items() if v[0] > now}
        key = _canonical(tool, args)
        if key not in self._items:
            self._items[key] = (now + self._ttl, await build())
        return self._items[key][1]


def client_can_elicit(ctx: Context) -> bool:
    caps = ctx.client_capabilities
    return caps is not None and caps.elicitation is not None


async def request_approval(
    ctx: Context,
    tokens: ConfirmationTokens,
    previews: PreviewCache,
    tool: str,
    args: dict[str, Any],
    token: str | None,
    build_preview: Callable[[], Awaitable[Preview]],
) -> Elicit[Approval] | Approval | PendingConfirmation:
    """Resolver body shared by gated tools; see the module docstring."""
    if client_can_elicit(ctx):
        # Tokens are never issued to elicitation-capable clients, so one passed here is ignored
        preview = await previews.get(tool, args, build_preview)
        return Elicit(preview.as_message() + "\n\nApprove?", Approval)
    if token:
        tokens.redeem(tool, args, token)
        return Approval(approve=True)
    preview = await previews.get(tool, args, build_preview)
    token, expires_at = tokens.issue(tool, args)
    return PendingConfirmation(preview=preview, token=token, expires_at=expires_at)


def unapproved(outcome: ElicitationResult[Any]) -> Gated[Any] | None:
    """The response to return when the action must not run yet, or None when it is approved."""
    if not isinstance(outcome, AcceptedElicitation):
        status = "cancelled" if outcome.action == "cancel" else "declined"
        return Gated(status=status, message=f"The user {status} the action; nothing was changed.")
    data = outcome.data
    if isinstance(data, PendingConfirmation):
        return Gated(
            status="confirmation_required",
            message="Nothing has run yet. Show the preview to the user and, only if they approve, call this "
                    "tool again with identical arguments plus confirmation_token.",
            preview=data.preview,
            confirmation_token=data.token,
            expires_at=data.expires_at,
        )
    if isinstance(data, Approval) and data.approve:
        return None
    return Gated(status="declined", message="The user declined the action; nothing was changed.")
