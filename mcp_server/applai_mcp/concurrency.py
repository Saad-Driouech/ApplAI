"""Concurrent awaits that keep ToolError messages visible to the model."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from mcp.server.mcpserver.exceptions import ToolError

from applai_mcp.errors import ApplAIError


async def gather(*calls: Callable[[], Awaitable[Any]]) -> list[Any]:
    """
    Run calls concurrently and return their results in order. A ToolError from any call is
    re-raised unwrapped; the SDK would treat the task group's ExceptionGroup as a crash.
    """
    results: list[Any] = [None] * len(calls)

    async def run(i: int, call: Callable[[], Awaitable[Any]]) -> None:
        results[i] = await call()

    try:
        async with anyio.create_task_group() as tg:
            for i, call in enumerate(calls):
                tg.start_soon(run, i, call)
    except* ToolError as group:
        raise group.exceptions[0] from None
    return results


def degrade(call: Callable[[], Awaitable[Any]], warnings: list[str], fallback: Any = None):
    """Wrap an ApplAI call so its failure becomes a warning, letting filesystem results still return."""
    async def wrapped() -> Any:
        try:
            return await call()
        except ApplAIError as exc:
            warnings.append(f"Pipeline data unavailable: {exc}")
            return fallback
    return wrapped
