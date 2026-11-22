from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from .rfq import TargetSlice


class ScoreRequest(TargetSlice):
    top_n: Optional[int] = Field(default=None, ge=1, le=50)

    # Hard filters (None -> server defaults)
    require_trade_assurance: Optional[bool] = None
    min_years: Optional[int] = Field(default=None, ge=0, le=100)
    max_price: Optional[float] = Field(default=None, gt=0)
    strict_moq: Optional[bool] = None
    strict_certs: Optional[bool] = None
    strict_min_price: Optional[bool] = None
    required_certs: Optional[list[str]] = Field(default=None, max_length=50)

    # Weights (non-negative; each group is normalized and the effective weights returned)
    weight_price: Optional[float] = Field(default=None)
    weight_moq: Optional[float] = Field(default=None)
    weight_trade_assurance: Optional[float] = Field(default=None)
    weight_years: Optional[float] = Field(default=None)
    weight_certs: Optional[float] = Field(default=None)
    weight_vendor_score: Optional[float] = Field(default=None)
    weight_rfq_fit: Optional[float] = Field(default=None)

    persist_artifacts: bool = True


class EffectiveWeights(BaseModel):
    """Percentages; each group sums to 100."""

    vendor: dict[str, float]
    blend: dict[str, float]


class Dashboard(BaseModel):
    tier_distribution: dict[str, int]
    score_component_averages: dict[str, Optional[float]]
    positions_total: int
    positions_without_vendors: int
    cross_item_vendors_total: int
    cross_item_vendors_multi: int
    risk_flag_counts: dict[str, int]
    missing_cache_families: list[str]


class DeliverablePayload(BaseModel):
    """JSON mirror of the 4-tab workbook."""

    meta: dict[str, Any]
    recommendations: list[dict[str, Any]]  # tab 1 (top_n vendors per position)
    family_search_plan: list[dict[str, Any]]  # tab 2
    cross_item_vendors: list[dict[str, Any]]  # tab 3
    disqualified: list[dict[str, Any]]  # tab 4
    dashboard: Dashboard
    warnings: list[str] = []


class ScoreResponse(DeliverablePayload):
    effective_weights: EffectiveWeights
    credits_consumed: int = 0
    persisted: bool


class LatestResponse(BaseModel):
    exists: bool
    result: Optional[DeliverablePayload] = None
