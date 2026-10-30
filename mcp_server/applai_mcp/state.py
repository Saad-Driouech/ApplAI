"""Per-connection state shared by tools, built in the server lifespan."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
import httpx2
from mcp.server.mcpserver import Context, MCPServer

from applai_mcp.applications_fs import ApplicationsIndex
from applai_mcp.client import ApplAIClient
from applai_mcp.config import Settings
from applai_mcp.confirm import ConfirmationTokens, PreviewCache
from applai_mcp.runs import PipelineRuns


@dataclass
class AppState:
    settings: Settings
    api: ApplAIClient
    fs: ApplicationsIndex
    tokens: ConfirmationTokens
    previews: PreviewCache
    runs: PipelineRuns


def state(ctx: Context) -> AppState:
    return ctx.request_context.lifespan_context


def make_lifespan(settings: Settings, transport: httpx2.AsyncBaseTransport | None = None):
    @asynccontextmanager
    async def lifespan(server: MCPServer) -> AsyncIterator[AppState]:
        api = ApplAIClient(settings, transport=transport)
        try:
            async with anyio.create_task_group() as tg:
                yield AppState(
                    settings=settings,
                    api=api,
                    fs=ApplicationsIndex(settings.applai_applications_dir),
                    tokens=ConfirmationTokens(settings.confirmation_ttl_seconds),
                    previews=PreviewCache(settings.confirmation_ttl_seconds),
                    runs=PipelineRuns(tg),
                )
                tg.cancel_scope.cancel()  # abandon in-flight phase requests on shutdown
        finally:
            await api.aclose()
    return lifespan
