from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel


class CacheEntrySummary(BaseModel):
    query_key: str
    family_id: Optional[str] = None
    family_name: Optional[str] = None
    search_query: Optional[str] = None
    listing_count: int
    sample_suppliers: list[str]
    scraped_at: Optional[str] = None
    pages: Optional[int] = None
    stealth: Optional[bool] = None
    credits: Optional[int] = None


class CacheListResponse(BaseModel):
    total: int
    entries: list[CacheEntrySummary]


class CacheEntryDetail(BaseModel):
    query_key: str
    search_query: Optional[str] = None
    family_id: Optional[str] = None
    family_name: Optional[str] = None
    scraped_at: Optional[str] = None
    products: list[dict[str, Any]]


class DebugFilesResponse(BaseModel):
    files: list[str]
