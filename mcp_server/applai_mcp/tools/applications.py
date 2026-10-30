"""Read-only tools over pipeline jobs and application folders combined."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

import anyio
from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from applai_mcp.applications_fs import normalize, similarity
from applai_mcp.concurrency import degrade, gather
from applai_mcp.models import (
    ApplicationList,
    ApplicationRecord,
    CountryStats,
    Country,
    FsApplication,
    JobSummary,
    Stage,
    Stats,
)
from applai_mcp.state import state
from applai_mcp.tools.scoring import READ_ONLY

PipelineStatus = Literal["new", "queued", "generating", "ready", "delivered", "approved", "rejected",
                         "skipped", "failed"]
# Without a status filter, "applications" means jobs that got a document bundle
BUNDLE_STATUSES = ("ready", "delivered", "approved", "rejected")


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _from_job(job: JobSummary) -> ApplicationRecord:
    return ApplicationRecord(
        source="pipeline", company=job.company, title=job.title, country=job.country, status=job.status,
        stage=None, score=job.score, job_id=job.id, application_id=job.application_id, path=None,
        prep_files=[], notes=[], source_url=job.source_url,
        last_activity=_parse_time(job.decision_at) or _parse_time(job.scraped_at),
    )


def _from_folder(app: FsApplication) -> ApplicationRecord:
    return ApplicationRecord(
        source="filesystem", company=app.company, title=app.role, country=app.country, status=None,
        stage=app.stage, score=None, job_id=None, application_id=None, path=app.path,
        prep_files=app.prep_files, notes=app.notes, source_url=None, last_activity=app.last_modified,
    )


def merge(jobs: list[JobSummary], folders: list[FsApplication]) -> list[ApplicationRecord]:
    """
    Join approved pipeline jobs to their promoted folder (same country, matching company, flat layout)
    and manual folders to jobs with a matching company + title. Unmatched records are kept as-is.
    """
    folder_records = [_from_folder(f) for f in folders]
    records = list(folder_records)
    for job in jobs:
        match = None
        for rec, folder in zip(folder_records, folders, strict=True):
            if rec.source != "filesystem" or folder.country != job.country:
                continue
            if similarity(job.company, folder.company) < 0.85:
                continue
            if folder.role is None and job.status == "approved":
                match = rec
            elif folder.role and normalize(folder.role) == normalize(job.title):
                match = rec
            if match:
                break
        if match is None:
            records.append(_from_job(job))
            continue
        match.source = "both"
        match.title = match.title or job.title
        match.status, match.score, match.job_id = job.status, job.score, job.id
        match.application_id, match.source_url = job.application_id, job.source_url
    return records


def register(server: MCPServer) -> None:
    @server.tool(annotations=READ_ONLY)
    async def list_applications(
        ctx: Context,
        source: Annotated[Literal["all", "pipeline", "filesystem"],
                          Field(description="pipeline = ApplAI DB, filesystem = job_applications/ folders")] = "all",
        status: Annotated[Optional[PipelineStatus],
                          Field(description="Pipeline status; default lists jobs with a document bundle")] = None,
        stage: Annotated[Optional[Stage], Field(description="Folder stage (filesystem records)")] = None,
        country: Optional[Country] = None,
        company: Annotated[Optional[str], Field(max_length=150, description="Case-insensitive substring")] = None,
        since: Annotated[Optional[date], Field(description="Only records active on or after this date")] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> ApplicationList:
        """
        List job applications across the ApplAI pipeline database and the job_applications/ folders
        (where manually prepared applications, CVs and interview prep live), newest activity first.
        Records present in both are merged (source='both').
        """
        s = state(ctx)
        country = country.upper() if country else None
        since_text = since.isoformat() if since else None
        jobs: list[JobSummary] = []
        folders: list[FsApplication] = []
        warnings: list[str] = []

        if source in ("all", "pipeline") and stage is None:
            statuses = (status,) if status else BUNDLE_STATUSES
            batches = await gather(*(
                degrade(lambda st=st: s.api.list_jobs(status=st, country=country, company=company,
                                                      since=since_text, limit=limit), warnings, [])
                for st in statuses
            ))
            warnings = warnings[:1]  # one per outage, not per status
            jobs = [j for batch in batches for j in batch]

        if source in ("all", "filesystem") and status is None:
            cutoff = datetime.combine(since, datetime.min.time(), timezone.utc) if since else None
            folders = [
                f for f in await anyio.to_thread.run_sync(s.fs.scan)
                if (not country or f.country == country)
                and (not company or company.lower() in f.company.lower())
                and (not stage or f.stage == stage)
                and (not cutoff or f.last_modified >= cutoff)
            ]

        records = merge(jobs, folders)
        floor = datetime.min.replace(tzinfo=timezone.utc)
        records.sort(key=lambda r: r.last_activity or floor, reverse=True)
        return ApplicationList(count=min(len(records), limit), truncated=len(records) > limit,
                               applications=records[:limit], warnings=warnings)

    @server.tool(annotations=READ_ONLY)
    async def get_stats(ctx: Context) -> Stats:
        """
        Overview of the job search: pipeline counts by status, approval rate and threshold advice from
        past decisions, application folders by stage and country, and recent activity.
        """
        s = state(ctx)
        warnings: list[str] = []
        counts, feedback, folders = await gather(
            degrade(s.api.stats, warnings), degrade(s.api.feedback, warnings, {}),
            lambda: anyio.to_thread.run_sync(s.fs.scan),
        )
        warnings = warnings[:1]
        now = datetime.now(timezone.utc)
        by_country: dict[str, Counter] = {}
        for f in folders:
            c = by_country.setdefault(f.country, Counter())
            c["applications"] += 1
            c["interviewing"] += f.stage == "interviewing"
        decisions = feedback.get("total_decisions")
        return Stats(
            pipeline_status_counts=counts,
            approval_rate=feedback.get("overall_approval_rate") if decisions else None,
            decisions=decisions,
            current_threshold=feedback.get("current_threshold"),
            recommended_threshold=feedback.get("recommended_threshold"),
            threshold_recommendation=feedback.get("recommendation"),
            filesystem_by_stage=dict(Counter(f.stage for f in folders)),
            filesystem_by_country=sorted(
                (CountryStats(country=k, applications=v["applications"], interviewing=v["interviewing"])
                 for k, v in by_country.items()),
                key=lambda c: c.applications, reverse=True,
            ),
            active_last_7_days=sum(f.last_modified >= now - timedelta(days=7) for f in folders),
            active_last_30_days=sum(f.last_modified >= now - timedelta(days=30) for f in folders),
            warnings=warnings,
        )
