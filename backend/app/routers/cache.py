from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends, Response

from ..core.auth import AuthUser, get_current_user, require_admin
from ..deps import Services, get_services
from ..schemas.cache import CacheEntryDetail, CacheListResponse, DebugFilesResponse

router = APIRouter(tags=["cache"])


@router.get("/cache", response_model=CacheListResponse)
async def list_cache(
    _: AuthUser = Depends(get_current_user), svc: Services = Depends(get_services)
) -> dict:
    entries = await anyio.to_thread.run_sync(svc.cache.list_summaries)
    return {"total": len(entries), "entries": entries}


@router.get("/cache/{query_key:path}", response_model=CacheEntryDetail)
async def get_cache_entry(
    query_key: str,
    _: AuthUser = Depends(get_current_user),
    svc: Services = Depends(get_services),
) -> dict:
    entry = await anyio.to_thread.run_sync(svc.cache.get_entry, query_key)
    return {**entry, "query_key": query_key.strip().lower()}


@router.delete("/cache/{query_key:path}", status_code=204)
async def delete_cache_entry(
    query_key: str,
    _: AuthUser = Depends(require_admin),
    svc: Services = Depends(get_services),
) -> Response:
    """Admin-only: the cache is shared by all users, and evicting costs credits to refill."""
    await anyio.to_thread.run_sync(svc.cache.delete, query_key)
    return Response(status_code=204)


@router.get("/debug/files", response_model=DebugFilesResponse)
async def list_debug_files(
    _: AuthUser = Depends(require_admin), svc: Services = Depends(get_services)
) -> dict:
    def _list() -> list[str]:
        d = svc.paths.debug_dir
        return sorted(p.name for p in d.iterdir() if p.is_file()) if d.is_dir() else []

    return {"files": await anyio.to_thread.run_sync(_list)}
