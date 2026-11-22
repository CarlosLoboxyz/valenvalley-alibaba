"""FastAPI app factory.

Run from the repo root:  uvicorn backend.app.main:app --host 0.0.0.0 --port 8000

IMPORTANT: run with exactly ONE worker. The job manager, job guard and cache locks are
in-process; multiple workers would race on the shared scrape cache and job registry.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

import anyio
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .core.auth import PocketBaseAuth
from .core.config import Settings
from .core.errors import install_error_handlers
from .core.paths import AppPaths
from .deps import Services
from .routers import cache, health, jobs, recommendations, rfq
from .services.cache_service import CacheService
from .services.pocketbase_mirror import PocketBaseMirror
from .services.rfq_service import RFQService
from .services.scoring_service import ScoringService
from .services.scrape_job_service import ScrapeJobService

API_PREFIX = "/api/v1"


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings()
    paths = AppPaths.from_settings(settings)
    paths.ensure()

    mirror = PocketBaseMirror(settings)
    cache_svc = CacheService(paths)
    rfq_svc = RFQService(settings, paths, cache_svc, mirror)
    services = Services(
        settings=settings,
        paths=paths,
        cache=cache_svc,
        rfq=rfq_svc,
        scoring=ScoringService(settings, paths, rfq_svc, mirror),
        jobs=ScrapeJobService(settings, paths, rfq_svc, mirror),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await anyio.to_thread.run_sync(services.jobs.recover_orphans)
        yield
        await anyio.to_thread.run_sync(services.jobs.shutdown)
        await app.state.auth.aclose()

    app = FastAPI(title="Alibaba B2B Sourcing API", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.services = services
    app.state.auth = PocketBaseAuth(settings)

    install_error_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,  # Bearer tokens, no cookies
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )

    max_body = (settings.max_upload_mb + 1) * 1024 * 1024

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):
        # Early rejection by declared size; a reverse proxy should also cap chunked bodies.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_body:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request body exceeds {settings.max_upload_mb} MB", "code": "file_too_large"},
            )
        return await call_next(request)

    for r in (health.router, rfq.router, jobs.router, recommendations.router, cache.router):
        app.include_router(r, prefix=API_PREFIX)
    return app


app = create_app()
