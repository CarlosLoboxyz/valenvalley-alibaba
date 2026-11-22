from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends, File, UploadFile

from ..core.auth import AuthUser, get_current_user
from ..deps import Services, get_services
from ..schemas.rfq import GroupRequest, GroupResponse, RFQFileInfo, RFQItemsRequest, UploadResponse

router = APIRouter(prefix="/rfq", tags=["rfq"])


@router.post("/upload", response_model=UploadResponse)
async def upload_rfq(
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_current_user),
    svc: Services = Depends(get_services),
) -> dict:
    return await anyio.to_thread.run_sync(svc.rfq.save_upload, user, file.filename or "", file.file)


@router.post("/items", response_model=UploadResponse)
async def create_rfq_from_items(
    body: RFQItemsRequest,
    user: AuthUser = Depends(get_current_user),
    svc: Services = Depends(get_services),
) -> dict:
    """Create an RFQ from a JSON list (typed or parsed client-side). Same result as an upload."""
    return await anyio.to_thread.run_sync(svc.rfq.save_items, user, body)


@router.get("/files", response_model=list[RFQFileInfo])
async def list_rfq_files(
    user: AuthUser = Depends(get_current_user), svc: Services = Depends(get_services)
) -> list[dict]:
    return await anyio.to_thread.run_sync(svc.rfq.list_files, user)


@router.post("/group", response_model=GroupResponse)
async def group_rfq(
    body: GroupRequest,
    user: AuthUser = Depends(get_current_user),
    svc: Services = Depends(get_services),
) -> dict:
    """Dry run: local clustering plus cache lookup. Always 0 credits."""
    return await anyio.to_thread.run_sync(svc.rfq.group, user, body)
