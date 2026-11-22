from __future__ import annotations

import json
from typing import Any, Optional

import anyio
from fastapi import APIRouter, Depends, Header
from sse_starlette.sse import EventSourceResponse

from ..core.auth import AuthUser, get_current_user, require_verified_user
from ..core.errors import ApiError
from ..deps import Services, get_services
from ..schemas.jobs import JobCreated, JobStatus, ScrapeJobRequest
from ..services.scrape_job_service import TERMINAL

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/scrape", response_model=JobCreated, status_code=202)
async def create_scrape_job(
    body: ScrapeJobRequest,
    user: AuthUser = Depends(require_verified_user),
    svc: Services = Depends(get_services),
) -> dict:
    """Spends ScrapingBee credits. Requires ``confirm_credit_spend: true``."""
    if body.confirm_credit_spend is not True:
        raise ApiError(400, "credit_confirmation_required", "confirm_credit_spend must be true to spend credits")
    job = await anyio.to_thread.run_sync(svc.jobs.create, user, body)
    # Contract: creation always reports "queued" (the worker may already have flipped to running)
    return {"job_id": job.job_id, "status": "queued", "projected_budget": job.projected}


@router.get("/active", response_model=Optional[JobStatus])
async def active_job(
    user: AuthUser = Depends(get_current_user), svc: Services = Depends(get_services)
) -> Optional[dict]:
    """The caller's queued or running scrape job, or null. Lets the UI resume after a refresh."""
    return await anyio.to_thread.run_sync(svc.jobs.active_for, user)


@router.get("/{job_id}", response_model=JobStatus)
async def get_job(
    job_id: str, user: AuthUser = Depends(require_verified_user), svc: Services = Depends(get_services)
) -> dict:
    return await anyio.to_thread.run_sync(svc.jobs.get, user, job_id)


@router.post("/{job_id}/cancel", response_model=JobStatus)
async def cancel_job(
    job_id: str, user: AuthUser = Depends(require_verified_user), svc: Services = Depends(get_services)
) -> dict:
    return await anyio.to_thread.run_sync(svc.jobs.cancel, user, job_id)


@router.get("/{job_id}/events")
async def job_events(
    job_id: str,
    user: AuthUser = Depends(require_verified_user),
    svc: Services = Depends(get_services),
    last_event_id: Optional[str] = Header(default=None),
) -> EventSourceResponse:
    """SSE stream. Auth is a normal Authorization header, so use a fetch-based SSE client
    (the browser ``EventSource`` cannot set headers). Supports ``Last-Event-ID`` resume."""
    job, snapshot = await anyio.to_thread.run_sync(svc.jobs.events_source, user, job_id)

    async def stream():
        if job is None:  # finished before a restart: replay only the terminal state
            data = snapshot["progress"] | {
                "credits_spent": snapshot["credits_spent"],
                "error": snapshot["error"],
            }
            yield {"id": "0", "event": snapshot["status"], "data": json.dumps(data)}
            return
        idx = int(last_event_id) + 1 if last_event_id and last_event_id.isdigit() else 0
        while True:
            with job.lock:
                terminal = job.status in TERMINAL
                batch = job.events[idx:]
            for ev in batch:
                data: dict[str, Any] = ev["data"]
                yield {"id": str(idx), "event": ev["event"], "data": json.dumps(data)}
                idx += 1
            if terminal and not batch:
                return
            await anyio.sleep(0.25)

    return EventSourceResponse(stream(), ping=15)
