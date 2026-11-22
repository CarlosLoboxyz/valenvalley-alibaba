"""Application settings, loaded from the repository-root ``.env`` via pydantic-settings.

``SCRAPINGBEE_API_KEY`` is held as ``SecretStr`` and must never be serialized into a
response. Public payloads are built explicitly in ``schemas/config.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- ScrapingBee / Alibaba ---
    scrapingbee_api_key: Optional[SecretStr] = None
    scrapingbee_url: str = "https://app.scrapingbee.com/api/v1/"
    alibaba_search_url: str = "https://www.alibaba.com/trade/search"
    scrapingbee_wait_ms: int = 3000
    scrapingbee_timeout: int = 90
    scrapingbee_stealth_proxy: bool = False
    max_products_per_page: int = 15
    default_pages: int = 1
    request_delay_seconds: float = 1.0
    debug_dir: str = "debug_raw"

    # --- RFQ / grouping ---
    rfq_excel_file: str = "templates/products.xlsx"
    family_granularity: str = "balanced"
    recommend_top_n: int = 5

    # --- Client filter defaults ---
    client_required_certs: str = ""
    client_min_years: int = 0
    client_require_trade_assurance: bool = False
    client_strict_moq: bool = False
    client_strict_certs: bool = False
    strict_min_price: bool = False

    # --- Score weights ---
    weight_price: float = 30.0
    weight_moq: float = 20.0
    weight_trade_assurance: float = 20.0
    weight_years: float = 15.0
    weight_certs: float = 15.0
    weight_vendor_score: float = 75.0
    weight_rfq_fit: float = 25.0

    # --- HTTP / storage ---
    cors_origins: str = "http://localhost:5173"
    output_dir: str = "output"
    max_upload_mb: int = Field(default=20, ge=1)

    # --- Auth (PocketBase) ---
    pocketbase_url: str = "http://localhost:8090"
    pocketbase_users_collection: str = "users"
    require_verified_email: bool = True
    auth_cache_ttl_seconds: int = 60
    pocketbase_mirror: bool = True
    admin_emails: str = ""  # comma-separated; may delete cache entries and read debug files

    # --- Credit guardrails (unset = unlimited) ---
    max_credits_per_job: Optional[int] = None
    max_credits_per_day: Optional[int] = None

    @field_validator("max_credits_per_job", "max_credits_per_day", mode="before")
    @classmethod
    def _blank_to_none(cls, v):
        return None if v in ("", None) else v

    @field_validator("family_granularity")
    @classmethod
    def _valid_granularity(cls, v: str) -> str:
        if v not in {"fine", "balanced", "broad"}:
            raise ValueError("FAMILY_GRANULARITY must be fine, balanced or broad")
        return v

    # ---- derived helpers ----
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def admin_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def required_certs_list(self) -> list[str]:
        return [c.strip() for c in self.client_required_certs.split(",") if c.strip()]

    @property
    def scrapingbee_configured(self) -> bool:
        return bool(self.scrapingbee_api_key and self.scrapingbee_api_key.get_secret_value().strip())

    def api_key_value(self) -> Optional[str]:
        return self.scrapingbee_api_key.get_secret_value().strip() if self.scrapingbee_configured else None
