from __future__ import annotations

import anyio
from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from ..core.auth import AuthUser, get_current_user
from ..deps import Services, get_services
from ..schemas.scoring import LatestResponse, ScoreRequest, ScoreResponse

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/score", response_model=ScoreResponse)
async def score(
    body: ScoreRequest,
    user: AuthUser = Depends(get_current_user),
    svc: Services = Depends(get_services),
) -> dict:
    """Offline re-scoring from the shared cache. Always 0 credits."""
    return await anyio.to_thread.run_sync(svc.scoring.score, user, body)


@router.get("/latest", response_model=LatestResponse)
async def latest(
    user: AuthUser = Depends(get_current_user), svc: Services = Depends(get_services)
) -> dict:
    return await anyio.to_thread.run_sync(svc.scoring.latest, user)


@router.get("/export/xlsx")
async def export_xlsx(
    user: AuthUser = Depends(get_current_user), svc: Services = Depends(get_services)
) -> FileResponse:
    path = await anyio.to_thread.run_sync(svc.scoring.export_path, user)
    return FileResponse(path, media_type=XLSX_MEDIA_TYPE, filename="final_recommendations.xlsx")
