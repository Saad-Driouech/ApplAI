"""Full context for one application, shaped for composing with the Gmail and Calendar connectors."""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Optional

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from applai_mcp.applications_fs import ApplicationsIndex, similarity
from applai_mcp.concurrency import degrade, gather
from applai_mcp.models import ApplicationDetail, Company, FolderDetail, FsMatch, JobDetail, JobSummary, PrepNote
from applai_mcp.state import state
from applai_mcp.tools.scoring import READ_ONLY, SAME_ROLE_SIMILARITY

MAX_JD_CHARS = 8_000
MAX_NOTE_CHARS = 6_000
MAX_NOTES_PER_FOLDER = 4
MAX_PIPELINE_JOBS = 5
MAX_PIPELINE_DESCRIPTIONS = 3
READABLE_SUFFIXES = {".md", ".txt"}
GMAIL_TERMS = ("(interview OR application OR applied OR recruiter OR offer OR assessment "
               "OR rejection OR unfortunately)")


def gmail_query(company: str) -> str:
    name = re.sub(r'["\\]', "", company).strip()
    return f'"{name}" {GMAIL_TERMS} newer_than:180d'


def _read(fs: ApplicationsIndex, rel_path: str, limit: int) -> tuple[str, bool]:
    text = fs.read_text(rel_path, max_chars=limit + 1)
    return text[:limit], len(text) > limit


def folder_detail(fs: ApplicationsIndex, match: FsMatch) -> FolderDetail:
    app = match.application
    jd_text, jd_truncated = (_read(fs, f"{app.path}/JD.txt", MAX_JD_CHARS) if app.has_jd else (None, False))
    readable = [p for p in app.prep_files if PurePosixPath(p).suffix.lower() in READABLE_SUFFIXES]
    notes = []
    for path in readable[:MAX_NOTES_PER_FOLDER]:
        content, truncated = _read(fs, path, MAX_NOTE_CHARS)
        notes.append(PrepNote(path=path, content=content, truncated=truncated))
    return FolderDetail(
        application=app, jd_text=jd_text, jd_truncated=jd_truncated, prep_notes=notes,
        other_prep_files=[p for p in app.prep_files if p not in readable[:MAX_NOTES_PER_FOLDER]],
    )


def _role_filter(matches: list[FsMatch], role: Optional[str], warnings: list[str]) -> list[FsMatch]:
    if not role:
        return matches
    kept = [m for m in matches if m.title_similarity is None or m.title_similarity >= SAME_ROLE_SIMILARITY]
    if matches and not kept:
        warnings.append(f"No folder matches the role '{role}'; showing all folders for the company")
        return matches
    return kept


def register(server: MCPServer) -> None:
    @server.tool(annotations=READ_ONLY)
    async def get_application(
        ctx: Context,
        company: Company,
        role: Annotated[Optional[str], Field(max_length=200, description="Narrow to one role")] = None,
    ) -> ApplicationDetail:
        """
        Everything ApplAI knows about one application: matching folders with the JD text and your
        interview-prep notes, pipeline jobs with score reasoning and description, plus ready-made
        search queries for the Gmail and Google Calendar connectors. Use it for interview prep and
        status checks.
        """
        s = state(ctx)
        warnings: list[str] = []
        matches, jobs = await gather(
            lambda: anyio.to_thread.run_sync(s.fs.find, company, role),
            degrade(lambda: s.api.list_jobs(company=company, limit=50), warnings, []),
        )
        matches = _role_filter(matches, role, warnings)
        try:
            folders = await anyio.to_thread.run_sync(lambda: [folder_detail(s.fs, m) for m in matches])
        except ToolError as exc:
            warnings.append(f"Could not read a folder file: {exc}")
            folders = []

        company_jobs: list[JobSummary] = [j for j in jobs if similarity(company, j.company) >= 0.85]
        if role:
            company_jobs.sort(key=lambda j: similarity(role, j.title), reverse=True)
        pipeline_jobs = [JobDetail(**j.model_dump()) for j in company_jobs[:MAX_PIPELINE_JOBS]]
        details = await gather(*(
            degrade(lambda j=j: s.api.get_job(j.id), warnings) for j in pipeline_jobs[:MAX_PIPELINE_DESCRIPTIONS]
        ))
        for i, detail in enumerate(details):
            if detail is not None:
                pipeline_jobs[i] = detail.model_copy(update={"description": (detail.description or "")[:MAX_JD_CHARS]})
        return ApplicationDetail(
            company=company, role=role, found=bool(folders or pipeline_jobs), folders=folders,
            pipeline_jobs=pipeline_jobs, gmail_query=gmail_query(company), calendar_query=company,
            warnings=list(dict.fromkeys(warnings)),
        )
