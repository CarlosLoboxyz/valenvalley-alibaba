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

    rfq_file_id: str = Field(min_length=1, max_length=64)  # id from /rfq/upload or /rfq/items
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


class RFQItemIn(BaseModel):
    """One line of a materials list submitted as JSON (canonical, already validated by the UI)."""

    name: str = Field(min_length=1, max_length=300)
    qty: Optional[int] = Field(default=None, ge=1, le=100_000_000)
    unit: Optional[str] = Field(default=None, max_length=40)
    position: Optional[int] = Field(default=None, ge=1, le=1_000_000)
    description: Optional[str] = Field(default=None, max_length=2000)
    min_price: Optional[float] = Field(default=None, ge=0, le=1e9)
    max_price: Optional[float] = Field(default=None, ge=0, le=1e9)
    brand: Optional[str] = Field(default=None, max_length=120)
    technical_specs: Optional[str] = Field(default=None, max_length=4000)


MAX_RFQ_ITEMS = 5000


class RFQItemsRequest(BaseModel):
    filename: Optional[str] = Field(default=None, max_length=120)
    items: list[RFQItemIn] = Field(min_length=1, max_length=MAX_RFQ_ITEMS)
