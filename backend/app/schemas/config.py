from __future__ import annotations

from pydantic import BaseModel

from ..core.config import Settings


class HealthResponse(BaseModel):
    status: str
    scrapingbee_configured: bool
    cache_entries: int
    default_rfq_exists: bool


class FilterDefaults(BaseModel):
    required_certs: list[str]
    min_years: int
    require_trade_assurance: bool
    strict_moq: bool
    strict_certs: bool
    strict_min_price: bool


class WeightDefaults(BaseModel):
    weight_price: float
    weight_moq: float
    weight_trade_assurance: float
    weight_years: float
    weight_certs: float
    weight_vendor_score: float
    weight_rfq_fit: float


class CreditRates(BaseModel):
    premium_per_page: int = 30
    stealth_per_page: int = 80


class PublicConfig(BaseModel):
    """Explicit allowlist of safe settings. Never add secrets here."""

    filters: FilterDefaults
    weights: WeightDefaults
    proxy_mode: str
    stealth_default: bool
    granularity: str
    granularities: list[str]
    default_pages: int
    max_products_per_page: int
    recommend_top_n: int
    credit_rates: CreditRates
    max_upload_mb: int
    require_verified_email: bool
    max_credits_per_job: int | None
    max_credits_per_day: int | None

    @classmethod
    def from_settings(cls, s: Settings) -> "PublicConfig":
        return cls(
            filters=FilterDefaults(
                required_certs=s.required_certs_list,
                min_years=s.client_min_years,
                require_trade_assurance=s.client_require_trade_assurance,
                strict_moq=s.client_strict_moq,
                strict_certs=s.client_strict_certs,
                strict_min_price=s.strict_min_price,
            ),
            weights=WeightDefaults(
                weight_price=s.weight_price,
                weight_moq=s.weight_moq,
                weight_trade_assurance=s.weight_trade_assurance,
                weight_years=s.weight_years,
                weight_certs=s.weight_certs,
                weight_vendor_score=s.weight_vendor_score,
                weight_rfq_fit=s.weight_rfq_fit,
            ),
            proxy_mode="stealth" if s.scrapingbee_stealth_proxy else "premium",
            stealth_default=s.scrapingbee_stealth_proxy,
            granularity=s.family_granularity,
            granularities=["fine", "balanced", "broad"],
            default_pages=s.default_pages,
            max_products_per_page=s.max_products_per_page,
            recommend_top_n=s.recommend_top_n,
            credit_rates=CreditRates(),
            max_upload_mb=s.max_upload_mb,
            require_verified_email=s.require_verified_email,
            max_credits_per_job=s.max_credits_per_job,
            max_credits_per_day=s.max_credits_per_day,
        )
