from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends

from ..core.auth import AuthUser, get_current_user
from ..deps import Services, get_services
from ..schemas.config import HealthResponse, PublicConfig

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health(svc: Services = Depends(get_services)) -> HealthResponse:
    entries = await anyio.to_thread.run_sync(svc.cache.count)
    return HealthResponse(
        status="ok",
        scrapingbee_configured=svc.settings.scrapingbee_configured,
        cache_entries=entries,
        default_rfq_exists=bool(svc.paths.default_rfq and svc.paths.default_rfq.is_file()),
    )


@router.get("/config", response_model=PublicConfig)
async def public_config(svc: Services = Depends(get_services)) -> PublicConfig:
    """Safe defaults only. The ScrapingBee key is never part of this payload."""
    return PublicConfig.from_settings(svc.settings)


@router.get("/me")
async def me(user: AuthUser = Depends(get_current_user)) -> dict:
    return {"id": user.id, "email": user.email, "verified": user.verified}
