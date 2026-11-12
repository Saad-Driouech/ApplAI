"""Read-only tools: everything Claude needs to score a job against the profile, and duplicate checks."""
from __future__ import annotations

from typing import Optional

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from applai_mcp.models import (
    Company,
    Country,
    DuplicateCheck,
    JdText,
    JobId,
    JobSummary,
    JobUrl,
    ScoringContextResult,
    Title,
)
from applai_mcp.concurrency import degrade, gather
from applai_mcp.state import state

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# May fetch a public URL
READ_ONLY_OPEN = ToolAnnotations(read_only_hint=True, open_world_hint=True)
SAME_ROLE_SIMILARITY = 0.6


def job_payload(jd_text: Optional[str], url: Optional[str], title: Optional[str], company: Optional[str],
                country: Optional[str], city: Optional[str] = None) -> dict:
    if bool(jd_text) == bool(url):
        raise ToolError("Provide exactly one of jd_text or url")
    payload = {"description": jd_text or "", "url": url, "title": title or "", "company": company or "",
               "country": (country or "").upper(), "city": city or ""}
    return {k: v for k, v in payload.items() if v}


def register(server: MCPServer) -> None:
    @server.tool(annotations=READ_ONLY_OPEN)
    async def get_scoring_context(
        ctx: Context,
        jd_text: Optional[JdText] = None,
        url: Optional[JobUrl] = None,
        job_id: Optional[JobId] = None,
        title: Optional[Title] = None,
        company: Optional[Company] = None,
        country: Optional[Country] = None,
        city: Optional[str] = None,
    ) -> ScoringContextResult:
        """
        Everything needed to score a job against the candidate profile yourself: the sanitized posting,
        the scoring rubric, the profile, learned preferences and the threshold, plus pipeline duplicates
        and existing application folders. Give one of: the pasted job description (jd_text), a public
        https URL, or a pipeline job_id. Nothing is stored.
        """
        if sum(bool(v) for v in (jd_text, url, job_id)) != 1:
            raise ToolError("Provide exactly one of jd_text, url or job_id")
        s = state(ctx)
        posting = job = None
        if job_id:
            job, context = await gather(lambda: s.api.get_job(job_id, clean=True), s.api.scoring_context)
            company, title = job.company, job.title
        else:
            posting, context = await gather(
                lambda: s.api.resolve_job(job_payload(jd_text, url, title, company, country, city)),
                s.api.scoring_context,
            )
            company, title = posting.job.company, posting.job.title
        existing = await anyio.to_thread.run_sync(s.fs.find, company, title or None) if company else []
        return ScoringContextResult(posting=posting, job=job, context=context, existing_applications=existing)

    @server.tool(annotations=READ_ONLY)
    async def check_duplicate(ctx: Context, company: Company, title: Title) -> DuplicateCheck:
        """
        Check whether a job was already seen or applied to: exact match in the ApplAI pipeline
        (normalized company + title) and fuzzy match against application folders in job_applications/.
        """
        s = state(ctx)
        warnings: list[str] = []
        pipeline: list[JobSummary] = await degrade(lambda: s.api.duplicates(company, title), warnings, [])()
        folders = await anyio.to_thread.run_sync(s.fs.find, company, title)

        same_role = [m for m in folders if m.title_similarity is not None
                     and m.title_similarity >= SAME_ROLE_SIMILARITY]
        parts = []
        if pipeline:
            parts.append(f"pipeline has {len(pipeline)} matching job(s) "
                         f"(status: {', '.join(sorted({j.status for j in pipeline}))})")
        if same_role:
            parts.append(f"{len(same_role)} application folder(s) for a similar role "
                         f"({', '.join(m.application.path for m in same_role[:3])})")
        elif folders:
            parts.append(f"{len(folders)} folder(s) for this company but a different or unnamed role")
        return DuplicateCheck(
            is_duplicate=bool(pipeline or same_role),
            summary="; ".join(parts) or "No matches in the pipeline or application folders",
            pipeline_matches=pipeline,
            filesystem_matches=folders,
            warnings=warnings,
        )
