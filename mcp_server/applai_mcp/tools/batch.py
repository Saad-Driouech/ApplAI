"""Batch scoring: Claude adds jobs it found, scores scraped jobs that pass the pre-filters and stores the results."""
from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from applai_mcp.concurrency import gather
from applai_mcp.models import JobsAdded, JobScoreInput, JobsToScore, NewJob, ScoresRecorded
from applai_mcp.state import state
from applai_mcp.tools.scoring import job_payload

MAX_BATCH = 30
MAX_NEW_JOBS = 25
# Changes job statuses in ApplAI only; a stored score is not overwritten by a repeat call
STORE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# Adds jobs to ApplAI (a repeat call reports duplicates) and fetches the given URLs
INGEST = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


def register(server: MCPServer) -> None:
    @server.tool(annotations=INGEST)
    async def add_jobs(
        ctx: Context,
        jobs: Annotated[list[NewJob], Field(min_length=1, max_length=MAX_NEW_JOBS)],
    ) -> JobsAdded:
        """
        Add jobs you found (pasted by the user, from web search, or from job alert emails) to the pipeline
        as 'new', to be scored like scraped jobs. Each job is fetched (url) or taken as text (jd_text),
        checked for duplicates and run through the location, title and AI-keyword pre-filters. A URL that
        cannot be read (LinkedIn rate limits, login walls) comes back as needs_text.
        """
        payload = [{**job_payload(j.jd_text, j.url, j.title, j.company, j.country, j.city), "keep": j.keep}
                   for j in jobs]
        results = await state(ctx).api.ingest(payload)
        count = lambda status: sum(r.status == status for r in results)  # noqa: E731
        return JobsAdded(inserted=count("inserted"), duplicates=count("duplicate"), filtered=count("filtered"),
                         needs_text=count("needs_text"), rejected=count("rejected"), results=results)

    @server.tool(annotations=STORE)
    async def list_jobs_to_score(
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=MAX_BATCH, description="Maximum jobs to return")] = MAX_BATCH,
    ) -> JobsToScore:
        """
        Scraped jobs waiting for a score, with the scoring rubric, profile and threshold. First runs the
        pipeline's free pre-filters: expires old jobs and skips jobs outside the target countries, with
        working-student or above-senior titles, or without AI/ML keywords. Score each returned job,
        then store the scores with record_scores.
        """
        s = state(ctx)
        prefilter = await s.api.prefilter()
        jobs, context = await gather(lambda: s.api.jobs_to_score(limit), s.api.scoring_context)
        return JobsToScore(prefilter=prefilter, context=context, jobs=jobs)

    @server.tool(annotations=STORE)
    async def record_scores(
        ctx: Context,
        scores: Annotated[list[JobScoreInput], Field(min_length=1, max_length=MAX_BATCH)],
    ) -> ScoresRecorded:
        """
        Store your scores for jobs from list_jobs_to_score. Jobs at or above the threshold become
        'queued' for documents; the rest are skipped (low_score or not_a_job). Only jobs still 'new'
        are updated; others are reported as errors.
        """
        results = await state(ctx).api.record_scores([s.model_dump() for s in scores])
        return ScoresRecorded(
            queued=sum(r.status == "queued" for r in results),
            skipped=sum(r.status == "skipped" for r in results),
            errors=sum(r.error is not None for r in results),
            results=results,
        )
