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


class JobListResponse(BaseModel):
    count: int
    jobs: list[JobSummary]


class DuplicatesResponse(BaseModel):
    dedup_key: str
    matches: list[JobSummary]
