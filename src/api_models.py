"""Request/response models for the single-job API routes (/jobs/*)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

COUNTRY_PATTERN = r"^(?:[A-Z]{2}|REMOTE)$"


class JobInput(BaseModel):
    """A job given either as pasted description text or as a public https URL."""

    description: str = Field("", max_length=50_000, description="Job description text")
    url: Optional[str] = Field(None, max_length=2000, description="Public https URL of the posting")
    title: str = Field("", max_length=200)
    company: str = Field("", max_length=150)
    country: str = Field("", max_length=10, description="ISO 3166-1 alpha-2 code or REMOTE")
    city: str = Field("", max_length=100)

    @field_validator("country")
    @classmethod
    def _upper_country(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _description_xor_url(self) -> "JobInput":
        if bool(self.description.strip()) == bool(self.url):
            raise ValueError("Provide exactly one of 'description' or 'url'")
        return self


class IngestItem(JobInput):
    """A job Claude found (pasted, on the web, in a job alert) to add for scoring."""

    keep: bool = Field(False, description="Add it even if the location, title or AI-keyword pre-filter rejects it")


class IngestRequest(BaseModel):
    jobs: list[IngestItem] = Field(min_length=1, max_length=25)


class IngestOutcome(BaseModel):
    index: int = Field(description="Position in the request")
    status: str = Field(description="inserted, duplicate, filtered (a pre-filter rejects it; resend with keep), "
                                    "needs_text (the URL could not be read; send the text or the employer's posting) "
                                    "or rejected")
    job_id: Optional[str] = None
    existing_job_id: Optional[str] = Field(None, description="The job it duplicates")
    title: str = ""
    company: str = ""
    reason: Optional[str] = None


class IngestResponse(BaseModel):
    inserted: int
    results: list[IngestOutcome]


class JobSummary(BaseModel):
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


class JobDetail(JobSummary):
    description: Optional[str] = None


class ResolvedJob(BaseModel):
    title: str
    company: str
    country: str
    city: str
    source_url: str
    description_chars: int


class ScoreResponse(BaseModel):
    score: float = Field(description="Fit score 0-10")
    threshold: float = Field(description="Pipeline threshold for queueing a job")
    reasoning: str
    must_haves_met: list[str]
    gaps: list[str]
    is_job_posting: bool
    passes_keyword_filter: bool = Field(description="Whether the pipeline's AI-keyword pre-filter would keep it")
    passes_title_filter: bool = Field(
        description="Whether the pipeline's title pre-filter (working-student and above-senior roles) would keep it")
    job: ResolvedJob
    duplicates: list[JobSummary] = Field(description="Existing pipeline jobs with the same company+title")
    sanitizer_flags: list[str] = Field(description="Content the JD sanitizer removed before scoring")


class GenerateRequest(BaseModel):
    job_id: Optional[str] = Field(None, max_length=64, description="Existing job to generate for")
    job: Optional[JobInput] = Field(None, description="New job; title, company and country required")
    cv_template: Optional[str] = Field(
        None, max_length=50, pattern=r"^[A-Za-z0-9_-]+$",
        description="Template name from APPLAI_CV_TEMPLATES; default APPLAI_CV_TEMPLATE",
    )

    @model_validator(mode="after")
    def _job_id_xor_job(self) -> "GenerateRequest":
        if (self.job_id is None) == (self.job is None):
            raise ValueError("Provide exactly one of 'job_id' or 'job'")
        return self


class GenerateResponse(BaseModel):
    job_id: str
    application_id: str
    status: str
    score: Optional[float]
    cv_path: str
    cover_letter_path: str
    approved_dir: str = Field(description="Folder under the working directory that approving moves the files into")
    delivery_error: Optional[str] = Field(None, description="Why it is still 'ready' instead of delivered to Discord")


class JobListResponse(BaseModel):
    count: int
    jobs: list[JobSummary]


class DuplicatesResponse(BaseModel):
    dedup_key: str
    matches: list[JobSummary]


# ── Claude-authored flow: Claude scores and writes, ApplAI stores and renders ──

CV_TEMPLATE_NAME = r"^[A-Za-z0-9_-]+$"


class ResolveResponse(BaseModel):
    job: ResolvedJob
    description: str = Field(description="Job description after the prompt-injection filter")
    location: str = Field(description="Location as stated by the source")
    passes_keyword_filter: bool = Field(description="Whether the pipeline's AI-keyword pre-filter would keep it")
    passes_title_filter: bool = Field(
        description="Whether the pipeline's title pre-filter (working-student and above-senior roles) would keep it")
    duplicates: list[JobSummary] = Field(description="Existing pipeline jobs with the same company+title")
    sanitizer_flags: list[str] = Field(description="Content the JD sanitizer removed")


class ScoringContext(BaseModel):
    rubric: str = Field(description="Scoring rubric and guidelines, the same ones the pipeline's scorer uses")
    profile: str = Field(description="Candidate profile summary (profile_summary.md)")
    learned_preferences: str = Field(description="Preferences learned from past approve/reject decisions")
    threshold: float = Field(description="Jobs scoring at or above this are queued for documents")
    target_countries: list[str] = Field(description="Countries the location pre-filter keeps, besides REMOTE")


class PrefilterResponse(BaseModel):
    expired: int = Field(description="Jobs expired for being older than JOB_MAX_AGE_DAYS")
    skipped: int = Field(description="'new' jobs skipped by the location, title or keyword pre-filter")
    remaining: int = Field(description="'new' jobs left to score")


class JobToScore(BaseModel):
    id: str
    title: str
    company: str
    location: str
    source_url: Optional[str] = None
    posted_at: Optional[str] = None
    description: str = Field(description="Sanitized description, first 3000 characters")


class JobsToScoreResponse(BaseModel):
    count: int
    jobs: list[JobToScore]


class ScoreRecord(BaseModel):
    """A score Claude assigned using the rubric from GET /scoring/context."""

    score: float = Field(ge=0, le=10)
    reasoning: str = Field(min_length=1, max_length=2000)
    is_job_posting: bool = True
    scored_by: str = Field("claude", max_length=50, pattern=r"^[A-Za-z0-9 ._-]+$",
                           description="Model that scored it, for the audit log")


class JobScore(ScoreRecord):
    job_id: str = Field(max_length=64)


class ScoresRequest(BaseModel):
    scores: list[JobScore] = Field(min_length=1, max_length=30)


class ScoreOutcome(BaseModel):
    job_id: str
    status: Optional[str] = Field(None, description="New status: queued, or skipped (see skip_reason)")
    skip_reason: Optional[str] = None
    error: Optional[str] = Field(None, description="Why the score was not stored")


class ScoresResponse(BaseModel):
    results: list[ScoreOutcome]


class GenerationContext(BaseModel):
    job: Optional[JobDetail] = Field(None, description="The job, with its sanitized description, when job_id was given")
    cv_template: str = Field(description="CV template name, or 'default'")
    cv_max_pages: int = Field(description="Pages of the compiled template; the tailored CV must not exceed it")
    cv_template_tex: str = Field(description="LaTeX source to tailor")
    cv_rules: str
    cover_letter_rules: str
    profile: str


class DraftRequest(BaseModel):
    job_id: Optional[str] = Field(None, max_length=64, description="Existing job to write documents for")
    job: Optional[JobInput] = Field(None, description="New job; title, company and country required")
    score: Optional[ScoreRecord] = Field(None, description="Required when the job has no score yet")
    cv_tex: str = Field(min_length=100, max_length=100_000, description="Complete tailored CV LaTeX source")
    cover_letter_body: str = Field(min_length=200, max_length=10_000,
                                   description="Plain-text body paragraphs separated by blank lines")
    cv_template: Optional[str] = Field(
        None, max_length=50, pattern=CV_TEMPLATE_NAME,
        description="Template name from APPLAI_CV_TEMPLATES; default APPLAI_CV_TEMPLATE",
    )

    @model_validator(mode="after")
    def _job_id_xor_job(self) -> "DraftRequest":
        if (self.job_id is None) == (self.job is None):
            raise ValueError("Provide exactly one of 'job_id' or 'job'")
        return self


class DraftResponse(BaseModel):
    draft_id: str
    job_id: Optional[str] = Field(None, description="Existing job, or None for a new one created at commit")
    title: str
    company: str
    country: str
    city: str = ""
    score: Optional[float] = None
    score_reasoning: Optional[str] = None
    cv_template: str
    cv_pages: Optional[int] = Field(None, description="Pages in the compiled CV; None if unknown")
    cv_max_pages: int = Field(1, description="Page limit, from the compiled template")
    cover_letter_pages: Optional[int] = None
    warnings: list[str] = Field(default_factory=list)
    committed: bool = False


# ── Company watchlist: public ATS job boards polled by the scrape phase ──

ATS_PATTERN = r"^(?:greenhouse|lever|ashby|smartrecruiters|personio|recruitee|workable)$"
SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"
HTTPS_URL = r"^https://"


class DetectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150, description="Company name")
    careers_url: Optional[str] = Field(None, max_length=2000, pattern=HTTPS_URL,
                                       description="Careers page or job board URL; the most reliable way to find the board")


class BoardCandidate(BaseModel):
    ats: str
    slug: str
    found_by: str = Field(description="url: in the careers URL; page: linked from the careers page; "
                                      "guess: guessed from the name and possibly another company's board")
    job_count: int = Field(description="Jobs on the board right now")
    sample_titles: list[str] = Field(description="Up to 3 titles, AI/ML roles first")
    error: Optional[str] = Field(None, description="Why the board could not be read")


class DetectResponse(BaseModel):
    candidates: list[BoardCandidate]


class WatchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150, description="Company name, used as the jobs' company")
    ats: str = Field(pattern=ATS_PATTERN)
    slug: str = Field(pattern=SLUG_PATTERN, description="Board slug from /watchlist/detect")
    careers_url: Optional[str] = Field(None, max_length=2000, pattern=HTTPS_URL)


class WatchedCompany(BaseModel):
    id: int
    name: str
    ats: str
    slug: str
    careers_url: Optional[str] = None
    active: bool
    added_at: str
    last_checked_at: Optional[str] = Field(None, description="Last scrape that read the board")
    last_job_count: Optional[int] = Field(None, description="Jobs on the board at the last successful read")
    last_relevant_count: Optional[int] = Field(None, description="Of those, jobs whose title passed the pre-filters")
    last_error: Optional[str] = Field(None, description="Error from the last read, if it failed")


class WatchResponse(BaseModel):
    company: WatchedCompany
    added: bool = Field(description="False when the board was already on the watchlist (it is reactivated)")
    job_count: int
    sample_titles: list[str]


class WatchlistResponse(BaseModel):
    count: int
    companies: list[WatchedCompany]
