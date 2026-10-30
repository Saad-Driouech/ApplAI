"""Write/outbound tools behind the confirmation gate, plus run tracking."""
from __future__ import annotations

from typing import Annotated, Literal, Optional

import anyio
from mcp.server.mcpserver import Context, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from applai_mcp.confirm import Approval, request_approval, unapproved
from applai_mcp.models import (
    Company,
    ConfirmationToken,
    Country,
    Gated,
    GenerateResult,
    JdText,
    JobId,
    JobUrl,
    PipelineRun,
    PipelineRunList,
    Preview,
    Title,
)
from applai_mcp.state import AppState, state
from applai_mcp.tools.scoring import READ_ONLY, job_payload

Phase = Literal["scrape", "score", "generate", "deliver"]
CvTemplate = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,50}$",
                                  description="Named CV template from APPLAI_CV_TEMPLATES, e.g. '1page'")]

GENERATE_TOOL = "generate_application"
PHASE_TOOL = "run_pipeline_phase"
DONE_STATUSES = {"generating", "ready", "delivered", "approved", "rejected"}
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

PHASE_EFFECTS = {
    "scrape": ["Query the job boards (Arbeitnow, Remotive, RemoteOK, Adzuna if configured)",
               "Insert newly found jobs into the ApplAI database"],
    "score": ["Mark jobs older than JOB_MAX_AGE_DAYS as expired",
              "Score a batch of 'new' jobs with the Tier-1 LLM (uses daily API quota)"],
    "generate": ["Generate a tailored CV and cover letter for each queued job with the Claude API "
                 "(about $0.02-0.05 per job)", "Write the PDFs into .pending/ for review"],
    "deliver": ["Post up to 10 ready bundles to Discord for approval",
                "Log each delivered job to the Notion tracker"],
}
PHASE_BACKLOG = {"score": "new", "generate": "queued", "deliver": "ready"}


async def _generate_preview(s: AppState, args: dict) -> Preview:
    details: dict = {}
    effects = []
    if args["job_id"]:
        job = await s.api.get_job(args["job_id"])
        if job.status in DONE_STATUSES:
            raise ToolError(f"Job {job.id} already has documents (status '{job.status}')")
        title, company, country = job.title, job.company, job.country
        details["Score"] = f"{job.score}/10" if job.score is not None else "not scored yet (will be scored first)"
    else:
        payload = job_payload(args["jd_text"], args["url"], args["title"], args["company"], args["country"],
                              args["city"])
        result = await s.api.score_job(payload)
        title, company, country = result.job.title, result.job.company, result.job.country
        missing = [name for name, value in (("title", title), ("company", company), ("country", country))
                   if not value]
        if missing:
            raise ToolError(f"Could not determine {', '.join(missing)} from the posting; pass them explicitly")
        if result.duplicates:
            dup = result.duplicates[0]
            raise ToolError(f"This job already exists in the pipeline as job_id {dup.id} (status '{dup.status}'); "
                            "call generate_application with that job_id instead")
        effects.append("Create a pipeline job record for this posting (source 'mcp')")
        details["Score"] = f"{result.score}/10 (pipeline threshold {result.threshold})"
        if result.gaps:
            details["Gaps"] = "; ".join(result.gaps[:5])

    effects += [
        "Call the Claude API to tailor a CV and cover letter (about $0.02-0.05)",
        "Compile both PDFs into .pending/<application_id>/ (nothing is sent yet)",
        "Leave it 'ready' for the next deliver run, which posts it to Discord for your approval",
    ]
    folders = await anyio.to_thread.run_sync(s.fs.find, company, title)
    details = {"Job": f"{title} at {company} ({country})", **details,
               "CV template": args["cv_template"] or "default"}
    if folders:
        details["Existing folders"] = ", ".join(m.application.path for m in folders[:3])
    return Preview(action=f"Generate an application for {title} at {company}", side_effects=effects, details=details)


async def _approve_generate(
    ctx: Context,
    jd_text: Optional[str] = None,
    url: Optional[str] = None,
    job_id: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
    country: Optional[str] = None,
    city: Optional[str] = None,
    cv_template: Optional[str] = None,
    confirmation_token: Optional[str] = None,
):
    if sum(bool(v) for v in (jd_text, url, job_id)) != 1:
        raise ToolError("Provide exactly one of jd_text, url or job_id")
    s = state(ctx)
    args = {"jd_text": jd_text, "url": url, "job_id": job_id, "title": title, "company": company,
            "country": country.upper() if country else None, "city": city, "cv_template": cv_template}
    return await request_approval(ctx, s.tokens, s.previews, GENERATE_TOOL, args, confirmation_token,
                                  lambda: _generate_preview(s, args))


async def _phase_preview(s: AppState, phase: str) -> Preview:
    details = {}
    if phase in PHASE_BACKLOG:
        counts = await s.api.stats()
        details[f"Jobs currently '{PHASE_BACKLOG[phase]}'"] = counts.get(PHASE_BACKLOG[phase], 0)
    return Preview(action=f"Run the ApplAI '{phase}' phase", side_effects=PHASE_EFFECTS[phase], details=details)


async def _approve_phase(ctx: Context, phase: str, confirmation_token: Optional[str] = None):
    s = state(ctx)
    return await request_approval(ctx, s.tokens, s.previews, PHASE_TOOL, {"phase": phase}, confirmation_token,
                                  lambda: _phase_preview(s, phase))


def register(server: MCPServer) -> None:
    @server.tool(name=GENERATE_TOOL, annotations=WRITE)
    async def generate_application(
        ctx: Context,
        approval: Annotated[ElicitationResult[Approval], Resolve(_approve_generate)],
        jd_text: Optional[JdText] = None,
        url: Optional[JobUrl] = None,
        job_id: Optional[JobId] = None,
        title: Optional[Title] = None,
        company: Optional[Company] = None,
        country: Optional[Country] = None,
        city: Optional[str] = None,
        cv_template: Optional[CvTemplate] = None,
        confirmation_token: Optional[ConfirmationToken] = None,
    ) -> Gated[GenerateResult]:
        """
        Generate a tailored CV and cover letter for one job (requires user confirmation).
        Give an existing job_id, or a new job as jd_text or url (title, company and country are
        needed, either passed or found on the page). Documents go to .pending/ and are delivered to
        Discord for approval by the next 'deliver' phase. The first call returns a preview; see its message.
        """
        if (blocked := unapproved(approval)) is not None:
            return blocked
        s = state(ctx)
        payload: dict = {"cv_template": cv_template}
        if job_id:
            payload["job_id"] = job_id
        else:
            payload["job"] = job_payload(jd_text, url, title, company, country, city)
        result = GenerateResult.model_validate(await s.api.generate_job(payload))
        return Gated(status="done", message=f"Documents generated for job {result.job_id}.", result=result)

    @server.tool(name=PHASE_TOOL, annotations=WRITE)
    async def run_pipeline_phase(
        ctx: Context,
        phase: Annotated[Phase, Field(description="Pipeline phase to run")],
        approval: Annotated[ElicitationResult[Approval], Resolve(_approve_phase)],
        confirmation_token: Optional[ConfirmationToken] = None,
    ) -> Gated[PipelineRun]:
        """
        Start an ApplAI pipeline phase in the background (requires user confirmation): scrape job
        boards, score new jobs, generate documents for queued jobs, or deliver ready bundles to
        Discord/Notion. Returns a run_id immediately; poll get_pipeline_run for the result.
        """
        if (blocked := unapproved(approval)) is not None:
            return blocked
        s = state(ctx)
        run = s.runs.start(phase, lambda: s.api.run_phase(phase))
        return Gated(status="done", message=f"Started '{phase}' as run {run.run_id}; check get_pipeline_run.",
                     result=run)

    @server.tool(annotations=READ_ONLY)
    async def get_pipeline_run(
        ctx: Context,
        run_id: Annotated[Optional[str], Field(max_length=32, description="Omit to list all runs")] = None,
    ) -> PipelineRunList:
        """Status and result of pipeline phases started with run_pipeline_phase in this session."""
        runs = state(ctx).runs
        return PipelineRunList(runs=[runs.get(run_id)] if run_id else runs.all())
