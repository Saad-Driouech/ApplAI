"""Document tools (Claude writes, ApplAI renders) and pipeline phases; writes go through the confirmation gate."""
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
    CvTemplate,
    DraftId,
    DraftResult,
    Gated,
    GenerateResult,
    GenerationGuide,
    JdText,
    JobId,
    JobUrl,
    PipelineRun,
    PipelineRunList,
    Preview,
    ScoreInput,
    Title,
)
from applai_mcp.state import AppState, state
from applai_mcp.tools.scoring import READ_ONLY, job_payload

Phase = Literal["scrape", "score", "generate", "deliver"]
CvTex = Annotated[str, Field(min_length=100, max_length=100_000,
                             description="Complete tailored CV LaTeX source, from \\documentclass to \\end{document}")]
CoverLetterBody = Annotated[str, Field(
    min_length=200, max_length=10_000,
    description="Cover letter body as plain text: paragraphs separated by blank lines, no salutation or sign-off")]

GENERATE_TOOL = "generate_application"
PHASE_TOOL = "run_pipeline_phase"
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
# Writes files into .pending/ only; nothing is registered or sent
DRAFT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

PHASE_EFFECTS = {
    "scrape": ["Query the job boards (Arbeitnow, EnglishJobs.de, Remotive, RemoteOK, Adzuna if configured)",
               "Insert newly found jobs into the ApplAI database"],
    "score": ["Mark jobs older than JOB_MAX_AGE_DAYS as expired",
              "Score a batch of 'new' jobs with ApplAI's own LLM (fails unless an LLM key is configured; "
              "normally you score them with list_jobs_to_score and record_scores)"],
    "generate": ["Generate a CV and cover letter for each queued job with ApplAI's own LLM (fails unless an LLM "
                 "key is configured; normally you write them with draft_application)",
                 "Write the PDFs into .pending/ for review"],
    "deliver": ["Post up to 10 ready bundles to Discord for approval",
                "Log each delivered job to the Notion tracker"],
}
PHASE_BACKLOG = {"score": "new", "generate": "queued", "deliver": "ready"}


def _allow_elicitation(s: AppState) -> bool:
    return s.settings.confirmation_mode == "auto"


async def _generate_preview(s: AppState, draft_id: str) -> Preview:
    draft = await s.api.get_draft(draft_id)
    if draft.committed:
        raise ToolError(f"Draft {draft_id} was already used; write a new draft to generate again")

    effects = ["Create the pipeline job record for this posting (source 'mcp')"] if draft.job_id is None else []
    effects += [
        f"Register the draft's PDFs in .pending/{draft_id}/ as an application",
        "Post it to Discord for your approve/reject decision and log it in Notion",
    ]
    details = {
        "Job": f"{draft.title} at {draft.company} ({draft.country})",
        "Score": f"{draft.score}/10" if draft.score is not None else "unknown",
        "Pages": f"CV {draft.cv_pages} (limit {draft.cv_max_pages}), cover letter {draft.cover_letter_pages}",
        "CV template": draft.cv_template,
    }
    if draft.warnings:
        details["Warnings"] = "; ".join(draft.warnings)
    folders = await anyio.to_thread.run_sync(s.fs.find, draft.company, draft.title)
    if folders:
        details["Existing folders"] = ", ".join(m.application.path for m in folders[:3])
    return Preview(action=f"Generate an application for {draft.title} at {draft.company}",
                   side_effects=effects, details=details)


async def _approve_generate(ctx: Context, draft_id: str, confirmation_token: Optional[str] = None):
    s = state(ctx)
    return await request_approval(ctx, s.tokens, s.previews, GENERATE_TOOL, {"draft_id": draft_id},
                                  confirmation_token, lambda: _generate_preview(s, draft_id), _allow_elicitation(s))


async def _phase_preview(s: AppState, phase: str) -> Preview:
    details = {}
    if phase in PHASE_BACKLOG:
        counts = await s.api.stats()
        details[f"Jobs currently '{PHASE_BACKLOG[phase]}'"] = counts.get(PHASE_BACKLOG[phase], 0)
    return Preview(action=f"Run the ApplAI '{phase}' phase", side_effects=PHASE_EFFECTS[phase], details=details)


async def _approve_phase(ctx: Context, phase: str, confirmation_token: Optional[str] = None):
    s = state(ctx)
    return await request_approval(ctx, s.tokens, s.previews, PHASE_TOOL, {"phase": phase}, confirmation_token,
                                  lambda: _phase_preview(s, phase), _allow_elicitation(s))


def _draft_next_step(warnings: list[str]) -> str:
    if warnings:
        return ("Fix these problems and call draft_application again with the corrected content: "
                + "; ".join(warnings))
    return ("Show the user the job, score and page counts. Only after they approve, call generate_application "
            "with this draft_id.")


def register(server: MCPServer) -> None:
    @server.tool(annotations=READ_ONLY)
    async def get_generation_context(
        ctx: Context,
        job_id: Optional[JobId] = None,
        cv_template: Optional[CvTemplate] = None,
    ) -> GenerationGuide:
        """
        What you need to write a tailored CV and cover letter: the LaTeX CV template, the CV and cover
        letter rules, and the candidate profile. Pass job_id for a pipeline job (its description is
        included); for a pasted posting, use the description from get_scoring_context.
        """
        context = await state(ctx).api.generation_context(job_id, cv_template)
        return GenerationGuide(**context.model_dump())

    @server.tool(annotations=DRAFT)
    async def draft_application(
        ctx: Context,
        cv_tex: CvTex,
        cover_letter_body: CoverLetterBody,
        job_id: Optional[JobId] = None,
        jd_text: Optional[JdText] = None,
        url: Optional[JobUrl] = None,
        title: Optional[Title] = None,
        company: Optional[Company] = None,
        country: Optional[Country] = None,
        city: Optional[str] = None,
        score: Optional[ScoreInput] = None,
        cv_template: Optional[CvTemplate] = None,
    ) -> DraftResult:
        """
        Compile your tailored CV LaTeX and cover letter body into PDFs as a draft, and report page counts
        and problems to fix. Give an existing job_id, or a new posting as jd_text or url (title, company
        and country are required, either passed or found on the page). Pass score when the job has none
        yet (always for a new posting). Nothing is registered or sent until generate_application.
        """
        if sum(bool(v) for v in (jd_text, url, job_id)) != 1:
            raise ToolError("Provide exactly one of jd_text, url or job_id")
        payload: dict = {"cv_tex": cv_tex, "cover_letter_body": cover_letter_body, "cv_template": cv_template,
                         "score": score.model_dump() if score else None}
        if job_id:
            payload["job_id"] = job_id
        else:
            payload["job"] = job_payload(jd_text, url, title, company, country, city)
        draft = await state(ctx).api.create_draft(payload)
        return DraftResult(draft=draft, next_step=_draft_next_step(draft.warnings))

    @server.tool(name=GENERATE_TOOL, annotations=WRITE)
    async def generate_application(
        ctx: Context,
        draft_id: DraftId,
        approval: Annotated[ElicitationResult[Approval], Resolve(_approve_generate)],
        confirmation_token: Optional[ConfirmationToken] = None,
    ) -> Gated[GenerateResult]:
        """
        Turn a checked draft from draft_application into an application and send it to Discord for the
        user's approve/reject decision, logging it in Notion (requires user confirmation). Only this
        application is sent. The first call returns a preview; see its message.
        """
        if (blocked := unapproved(approval)) is not None:
            return blocked
        result = await state(ctx).api.commit_draft(draft_id)
        if result.delivery_error:
            message = (f"Application created for job {result.job_id} but not sent yet: {result.delivery_error}. "
                       "run_pipeline_phase('deliver') sends it.")
        else:
            message = f"Application for job {result.job_id} sent to Discord for your approval."
        if result.approved_dir:
            message += f" Approving it moves the files into job_applications/{result.approved_dir}/."
        return Gated(status="done", message=message, result=result)

    @server.tool(name=PHASE_TOOL, annotations=WRITE)
    async def run_pipeline_phase(
        ctx: Context,
        phase: Annotated[Phase, Field(description="Pipeline phase to run")],
        approval: Annotated[ElicitationResult[Approval], Resolve(_approve_phase)],
        confirmation_token: Optional[ConfirmationToken] = None,
    ) -> Gated[PipelineRun]:
        """
        Start an ApplAI pipeline phase in the background (requires user confirmation): scrape job
        boards or deliver ready bundles to Discord/Notion. 'score' and 'generate' use ApplAI's own LLM
        and only work when it has an API key. Returns a run_id immediately; poll get_pipeline_run.
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
