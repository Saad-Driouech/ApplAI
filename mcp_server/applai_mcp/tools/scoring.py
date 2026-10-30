"""Read-only tools: score a job against the profile, check for duplicates."""
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
    JobSummary,
    JobUrl,
    ScoreJobResult,
    Title,
)
from applai_mcp.concurrency import degrade
from applai_mcp.state import state

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Scoring calls an LLM and may fetch a public URL
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
    async def score_job(
        ctx: Context,
        jd_text: Optional[JdText] = None,
        url: Optional[JobUrl] = None,
        title: Optional[Title] = None,
        company: Optional[Company] = None,
        country: Optional[Country] = None,
        city: Optional[str] = None,
    ) -> ScoreJobResult:
        """
        Score a job against the candidate profile (0-10) using ApplAI's scoring model, without storing it.
        Give either the pasted job description (jd_text) or a public https URL. Also reports pipeline
        duplicates and existing application folders for the same company.
        """
        s = state(ctx)
        result = await s.api.score_job(job_payload(jd_text, url, title, company, country, city))
        existing = (
            await anyio.to_thread.run_sync(s.fs.find, result.job.company, result.job.title or None)
            if result.job.company else []
        )
        return ScoreJobResult(result=result, existing_applications=existing)

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
