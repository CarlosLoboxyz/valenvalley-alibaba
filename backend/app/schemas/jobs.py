from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .rfq import GroupRequest

JobState = Literal["queued", "running", "completed", "cancelled", "failed"]


class ScrapeJobRequest(GroupRequest):
    max_products_per_page: Optional[int] = Field(default=None, ge=1, le=60)
    force_refresh: bool = False
    family_queries: Optional[list[str]] = Field(default=None, max_length=500)
    confirm_credit_spend: bool = False  # must be true, otherwise HTTP 400


class ProjectedBudget(BaseModel):
    total_families: int
    cached_families: int
    families_to_scrape: int
    pages: int
    stealth: bool
    rate_per_page: int
    estimated_credits: int


class JobCreated(BaseModel):
    job_id: str
    status: JobState
    projected_budget: ProjectedBudget


class FamilyLog(BaseModel):
    family_id: str
    search_query: str
    status: str
    listing_count: int = 0
    credits: int = 0
    warnings: list[str] = []
    error: Optional[str] = None


class JobProgress(BaseModel):
    completed_families: int
    total_families: int
    cache_hits: int
    live_fetches: int


class JobStatus(BaseModel):
    job_id: str
    rfq_file_id: Optional[str] = None  # list this job scrapes for (lets the UI re-score it)
    granularity: Optional[str] = None
    status: JobState
    progress: JobProgress
    credits_spent: int
    projected_budget: ProjectedBudget
    family_logs: list[FamilyLog]
    warnings: list[str]
    error: Optional[str] = None
    created_at: str
    finished_at: Optional[str] = None
