"""Background pipeline-phase runs, so long phases don't hit client tool-call timeouts."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import anyio.abc
from mcp.server.mcpserver.exceptions import ToolError

from applai_mcp.models import PipelineRun

_MAX_KEPT = 50


class PipelineRuns:
    """Process-local registry; runs continue server-side even if this process exits."""

    def __init__(self, task_group: anyio.abc.TaskGroup):
        self._tg = task_group
        self._runs: dict[str, PipelineRun] = {}

    def running(self, phase: str) -> PipelineRun | None:
        return next((r for r in self._runs.values() if r.phase == phase and r.status == "running"), None)

    def start(self, phase: str, call: Callable[[], Awaitable[dict[str, Any]]]) -> PipelineRun:
        if active := self.running(phase):
            raise ToolError(f"Phase '{phase}' is already running as run {active.run_id}")
        run = PipelineRun(run_id=uuid.uuid4().hex[:12], phase=phase, status="running",
                          started_at=datetime.now(timezone.utc))
        self._runs[run.run_id] = run
        self._prune()
        self._tg.start_soon(self._execute, run, call)
        return run

    async def _execute(self, run: PipelineRun, call: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        try:
            run.result = await call()
            run.status = "succeeded"
        except ToolError as exc:
            run.status, run.error = "failed", str(exc)
        except Exception as exc:  # surfaced through get_pipeline_run, never raised into the task group
            run.status, run.error = "failed", f"Unexpected error: {type(exc).__name__}"
        finally:
            run.finished_at = datetime.now(timezone.utc)

    def _prune(self) -> None:
        finished = [r for r in self._runs.values() if r.status != "running"]
        for run in sorted(finished, key=lambda r: r.started_at)[: max(0, len(self._runs) - _MAX_KEPT)]:
            del self._runs[run.run_id]

    def get(self, run_id: str) -> PipelineRun:
        if run_id not in self._runs:
            raise ToolError(f"Unknown run_id {run_id}; runs are kept only while this MCP server is running")
        return self._runs[run_id]

    def all(self) -> list[PipelineRun]:
        return sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)
