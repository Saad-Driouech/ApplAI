"""Tests for confirmation tokens and the preview cache."""
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from applai_mcp.confirm import ConfirmationTokens, PreviewCache
from applai_mcp.models import Preview

ARGS = {"phase": "score"}


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tokens(clock: Clock) -> ConfirmationTokens:
    return ConfirmationTokens(ttl_seconds=600, clock=clock)


def test_valid_token_redeems_once(tokens):
    token, _ = tokens.issue("run_pipeline_phase", ARGS)
    tokens.redeem("run_pipeline_phase", ARGS, token)
    with pytest.raises(ToolError, match="already used"):
        tokens.redeem("run_pipeline_phase", ARGS, token)


@pytest.mark.parametrize("tool,args", [
    ("run_pipeline_phase", {"phase": "deliver"}),
    ("generate_application", ARGS),
])
def test_token_is_bound_to_tool_and_arguments(tokens, tool, args):
    token, _ = tokens.issue("run_pipeline_phase", ARGS)
    with pytest.raises(ToolError, match="does not match"):
        tokens.redeem(tool, args, token)


def test_token_expires(tokens, clock):
    token, expires_at = tokens.issue("run_pipeline_phase", ARGS)
    assert expires_at.timestamp() == clock.now + 600
    clock.now += 601
    with pytest.raises(ToolError, match="expired"):
        tokens.redeem("run_pipeline_phase", ARGS, token)


def test_extending_the_expiry_breaks_the_mac(tokens):
    token, _ = tokens.issue("run_pipeline_phase", ARGS)
    expiry, nonce, mac = token.split(".")
    with pytest.raises(ToolError, match="does not match"):
        tokens.redeem("run_pipeline_phase", ARGS, f"{int(expiry) + 3600}.{nonce}.{mac}")


@pytest.mark.parametrize("token", ["", "abc", "1.2", "x.y.z"])
def test_malformed_tokens(tokens, token):
    with pytest.raises(ToolError, match="Malformed"):
        tokens.redeem("run_pipeline_phase", ARGS, token)


def test_tokens_from_another_process_are_rejected(clock):
    token, _ = ConfirmationTokens(600, clock).issue("run_pipeline_phase", ARGS)
    with pytest.raises(ToolError, match="does not match"):
        ConfirmationTokens(600, clock).redeem("run_pipeline_phase", ARGS, token)


@pytest.mark.anyio
async def test_preview_cache_builds_once_per_arguments_until_expiry(clock):
    cache = PreviewCache(ttl_seconds=60, clock=clock)
    builds = []

    async def build() -> Preview:
        builds.append(1)
        return Preview(action="a", side_effects=[], details={})

    await cache.get("t", ARGS, build)
    await cache.get("t", ARGS, build)
    assert len(builds) == 1
    await cache.get("t", {"phase": "deliver"}, build)
    assert len(builds) == 2
    clock.now += 61
    await cache.get("t", ARGS, build)
    assert len(builds) == 3
