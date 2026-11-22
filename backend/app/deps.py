"""FastAPI dependencies exposing the service container."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .core.config import Settings
from .core.paths import AppPaths
from .services.cache_service import CacheService
from .services.rfq_service import RFQService
from .services.scoring_service import ScoringService
from .services.scrape_job_service import ScrapeJobService


@dataclass
class Services:
    settings: Settings
    paths: AppPaths
    cache: CacheService
    rfq: RFQService
    scoring: ScoringService
    jobs: ScrapeJobService


def get_services(request: Request) -> Services:
    return request.app.state.services
