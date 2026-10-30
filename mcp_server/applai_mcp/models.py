"""Typed tool inputs and outputs. API mirror models ignore unknown fields for forward compatibility."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

# ── Reusable input types ──────────────────────────────────────────────────

JobId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$", description="ApplAI job id")]
Country = Annotated[str, Field(pattern=r"^(?:[A-Za-z]{2}|REMOTE|remote)$",
                               description="ISO 3166-1 alpha-2 code (DE, AE, CH...) or REMOTE")]
JdText = Annotated[str, Field(min_length=50, max_length=50_000, description="Full job description text")]
JobUrl = Annotated[str, Field(pattern=r"^https://", max_length=2000,
                              description="Public https URL of the posting (login-walled pages like LinkedIn fail)")]
Company = Annotated[str, Field(min_length=1, max_length=150, description="Company name")]
Title = Annotated[str, Field(min_length=1, max_length=200, description="Job title")]
ConfirmationToken = Annotated[str, Field(
    max_length=200,
    description="Token from a previous confirmation_required response; pass it with identical arguments to proceed",
)]

# ── ApplAI API mirrors ────────────────────────────────────────────────────


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class JobSummary(_ApiModel):
    id: str
    source: str
    title: str
    company: str
    city: Optional[str] = None
    country: Optional[str] = None
    source_url: Optional[str] = None
    posted_at: Optional[str] = None
    scraped_at: Optional[str] = None
    score: Optional[float] = None
    score_reasoning: Optional[str] = None
    status: str
    skip_reason: Optional[str] = None
    last_error: Optional[str] = None
    application_id: Optional[str] = None
    user_decision: Optional[str] = None
    decision_at: Optional[str] = None


class ResolvedJob(_ApiModel):
    title: str
    company: str
    country: str
    city: str
    source_url: str
    description_chars: int


class ScoreResult(_ApiModel):
    score: float = Field(description="Fit score 0-10")
    threshold: float = Field(description="Score at which the pipeline queues a job")
    reasoning: str
    must_haves_met: list[str]
    gaps: list[str]
    is_job_posting: bool
    passes_keyword_filter: bool = Field(description="Whether the pipeline's AI-keyword pre-filter would keep it")
    job: ResolvedJob
    duplicates: list[JobSummary] = Field(description="Pipeline jobs with the same normalized company+title")
    sanitizer_flags: list[str] = Field(description="Content removed by the prompt-injection filter before scoring")


class GenerateResult(_ApiModel):
    job_id: str
    application_id: str
    status: str
    score: Optional[float]
    cv_path: str
    cover_letter_path: str
    next_step: str = Field("Run the 'deliver' phase (or wait for the scheduled run) to post it to Discord for "
                           "approval; approving moves the files into job_applications/<country>/<company>/")

# ── Filesystem applications ───────────────────────────────────────────────

Stage = Literal["researching", "prepared", "interviewing"]


class FsApplication(BaseModel):
    """An application folder under job_applications/."""

    country: str = Field(description="ISO code derived from the country folder")
    company: str
    role: Optional[str] = Field(description="Role from the folder name; None for the flat layout")
    layout: Literal["nested", "flat"] = Field(description="nested = <Company>/<Role>/, flat = <Company>/")
    path: str = Field(description="Path relative to the job_applications root")
    has_jd: bool
    has_cv: bool
    has_cover_letter: bool
    prep_files: list[str] = Field(description="Interview / call prep and Q&A files (relative paths)")
    notes: list[str] = Field(description="Dated 'NOTE (YYYY-MM-DD): ...' lines from JD.txt")
    last_modified: datetime
    stage: Stage = Field(description="researching = no CV yet, prepared = CV written, interviewing = prep files exist")


class FsMatch(BaseModel):
    application: FsApplication
    company_similarity: float = Field(ge=0, le=1)
    title_similarity: Optional[float] = Field(None, ge=0, le=1, description="None when the folder has no role")

# ── Tool outputs ──────────────────────────────────────────────────────────


class ScoreJobResult(BaseModel):
    result: ScoreResult
    existing_applications: list[FsMatch] = Field(description="Your application folders for the same company")


class DuplicateCheck(BaseModel):
    is_duplicate: bool = Field(description="True if the pipeline has this exact job or a folder matches the role")
    summary: str
    pipeline_matches: list[JobSummary]
    filesystem_matches: list[FsMatch]
    warnings: list[str] = Field(default_factory=list, description="Sources that could not be checked")


class ApplicationRecord(BaseModel):
    source: Literal["pipeline", "filesystem", "both"]
    company: str
    title: Optional[str]
    country: Optional[str]
    status: Optional[str] = Field(description="Pipeline status (pipeline records only)")
    stage: Optional[Stage] = Field(description="Folder-derived stage (filesystem records only)")
    score: Optional[float]
    job_id: Optional[str]
    application_id: Optional[str]
    path: Optional[str]
    prep_files: list[str]
    notes: list[str]
    source_url: Optional[str]
    last_activity: Optional[datetime]


class ApplicationList(BaseModel):
    count: int
    truncated: bool = Field(description="True if more records matched than the limit")
    applications: list[ApplicationRecord]
    warnings: list[str] = Field(default_factory=list, description="Sources that could not be read")


class CountryStats(BaseModel):
    country: str
    applications: int
    interviewing: int


class Stats(BaseModel):
    pipeline_status_counts: Optional[dict[str, int]] = Field(description="None if the API is unavailable")
    approval_rate: Optional[float] = Field(description="Approved / decided bundles; None before any decisions")
    decisions: Optional[int]
    current_threshold: Optional[float]
    recommended_threshold: Optional[float]
    threshold_recommendation: Optional[str]
    filesystem_by_stage: dict[str, int]
    filesystem_by_country: list[CountryStats]
    active_last_7_days: int = Field(description="Application folders modified in the last 7 days")
    active_last_30_days: int
    warnings: list[str] = Field(default_factory=list, description="Sources that could not be read")


class Preview(BaseModel):
    """What a gated action will do, shown before it runs."""

    action: str
    side_effects: list[str]
    details: dict[str, Any]

    def as_message(self) -> str:
        lines = [self.action, "", "This will:"] + [f"- {s}" for s in self.side_effects]
        if self.details:
            lines += ["", "Details:"] + [f"- {k}: {v}" for k, v in self.details.items()]
        return "\n".join(lines)


class Gated(BaseModel, Generic[T]):
    """Result of a write/outbound tool: a pending confirmation, a decline, or the action's result."""

    status: Literal["confirmation_required", "declined", "cancelled", "done"]
    message: str
    preview: Optional[Preview] = None
    confirmation_token: Optional[str] = None
    expires_at: Optional[datetime] = None
    result: Optional[T] = None


class PipelineRun(BaseModel):
    run_id: str
    phase: str
    status: Literal["running", "succeeded", "failed"]
    started_at: datetime
    finished_at: Optional[datetime] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class PipelineRunList(BaseModel):
    runs: list[PipelineRun]
