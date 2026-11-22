from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Granularity = Literal["fine", "balanced", "broad"]
POSITIONS_PATTERN = r"^[0-9,\-\s]{1,200}$"


class RFQPreviewRow(BaseModel):
    position: int
    name: str
    qty: Optional[int] = None
    unit: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    brand: Optional[str] = None


class UploadResponse(BaseModel):
    upload_id: str
    filename: str
    row_count: int
    sheets: list[str]
    sheet: str
    warnings: list[str]
    preview: list[RFQPreviewRow]


class RFQFileInfo(BaseModel):
    rfq_file_id: str
    filename: str
    row_count: Optional[int] = None
    is_default: bool = False
    created_at: Optional[str] = None


class TargetSlice(BaseModel):
    """Which RFQ rows to process. Shared by grouping, scraping and scoring."""

    rfq_file_id: str = Field(default="default", max_length=64)
    granularity: Optional[Granularity] = None  # None -> FAMILY_GRANULARITY
    limit: Optional[int] = Field(default=None, ge=1, le=10000)
    positions: Optional[str] = Field(default=None, pattern=POSITIONS_PATTERN)


class GroupRequest(TargetSlice):
    pages: int = Field(default=1, ge=1, le=10)
    stealth: bool = False


class FamilyOut(BaseModel):
    family_id: str
    family_name: str
    search_query: str
    cohesion_score: float
    item_count: int
    positions: list[int]
    is_cached: bool
    cached_listing_count: int


class GroupResponse(BaseModel):
    total_items: int
    family_count: int
    api_reduction_pct: float
    cached_families: int
    uncached_families: int
    pages: int
    stealth: bool
    estimated_credits_premium: int
    estimated_credits_stealth: int
    estimated_credits_selected: int
    granularity: Granularity
    families: list[FamilyOut]
    warnings: list[str] = []
