"""Background scrape job manager: the only code path that can spend ScrapingBee credits.

* Jobs run on daemon worker threads (``scrape_families`` is blocking ``requests`` code).
* Cancellation is cooperative: a flag checked between pages and families. A request
  already in flight cannot be interrupted, and every finished family is already saved.
* Events are appended to a per-job list; SSE readers poll it by index, so late
  subscribers replay history and no event loop coupling exists.
* Job snapshots are persisted to ``output/jobs/`` so status survives restarts; jobs left
  ``queued``/``running`` by a crash are marked ``failed`` on startup.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..core.auth import AuthUser
from ..core.config import Settings
from ..core.errors import ApiError
from ..core.locks import JobGuard
from ..core.paths import AppPaths
from ..schemas.jobs import ScrapeJobRequest
from .pipeline_bridge import pipeline
from .pocketbase_mirror import PocketBaseMirror
from .rfq_service import RFQService

log = logging.getLogger("app.jobs")
TERMINAL = {"completed", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    job_id: str
    user: AuthUser
    projected: dict[str, Any]
    rfq_file_id: str = ""
    granularity: str = ""
    created_at: str = field(default_factory=_now)
    status: str = "queued"
    finished_at: Optional[str] = None
    total_families: int = 0
    completed_families: int = 0
    cache_hits: int = 0
    live_fetches: int = 0
    credits_spent: int = 0
    family_logs: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    events: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "job_id": self.job_id,
                "owner_id": self.user.id,
                "rfq_file_id": self.rfq_file_id,
                "granularity": self.granularity,
                "status": self.status,
                "progress": {
                    "completed_families": self.completed_families,
                    "total_families": self.total_families,
                    "cache_hits": self.cache_hits,
                    "live_fetches": self.live_fetches,
                },
                "credits_spent": self.credits_spent,
                "projected_budget": self.projected,
                "family_logs": list(self.family_logs),
                "warnings": list(self.warnings),
                "error": self.error,
                "created_at": self.created_at,
                "finished_at": self.finished_at,
            }

    def push(self, event_type: str, **data: Any) -> None:
        with self.lock:
            self.events.append({"event": event_type, "data": data})


class ScrapeJobService:
    def __init__(
        self,
        settings: Settings,
        paths: AppPaths,
        rfq: RFQService,
        mirror: PocketBaseMirror,
        guard: Optional[JobGuard] = None,
        sleep_fn=None,
        scrape_fn=None,
    ):
        self.settings = settings
        self.paths = paths
        self.rfq = rfq
        self.mirror = mirror
        self.guard = guard or JobGuard()
        self._jobs: dict[str, Job] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._registry_lock = threading.Lock()
        # Injection points used by tests (never set in production)
        self._sleep_fn = sleep_fn
        self._scrape_fn = scrape_fn

    # ------------------------------------------------------------------
    # Startup / shutdown
    # ------------------------------------------------------------------
    def recover_orphans(self) -> int:
        n = 0
        if not self.paths.jobs_dir.is_dir():
            return 0
        for p in self.paths.jobs_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("status") in ("queued", "running"):
                data["status"] = "failed"
                data["error"] = "Server restarted while the job was active"
                data["finished_at"] = _now()
                pipeline.write_json_atomic(p, data)
                n += 1
        return n

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._registry_lock:
            jobs = list(self._jobs.values())
            threads = list(self._threads.values())
        for j in jobs:
            j.cancel.set()
        for t in threads:
            t.join(timeout=timeout)

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    def _credits_today(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total = 0
        for p in self.paths.jobs_dir.glob("*.json") if self.paths.jobs_dir.is_dir() else []:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if str(d.get("created_at", "")).startswith(today):
                total += int(d.get("credits_spent") or 0)
        return total

    def create(self, user: AuthUser, req: ScrapeJobRequest) -> Job:
        """Validate, enforce guardrails, and start a job. Blocking (builds the plan)."""
        api_key = self.settings.api_key_value()
        if not api_key:
            raise ApiError(503, "scrapingbee_not_configured", "SCRAPINGBEE_API_KEY is not configured")

        built = self.rfq.build_plan(user, req)
        plan = built["plan"]
        keys_in_plan = {pipeline.normalize_query_key(f["search_query"]) for f in plan}

        only: Optional[set[str]] = None
        if req.family_queries is not None:
            only = {pipeline.normalize_query_key(q) for q in req.family_queries}
            unknown = sorted(only - keys_in_plan)
            if unknown:
                raise ApiError(400, "unknown_family_query", f"Queries not in the current plan: {unknown[:3]}")

        classified = pipeline.classify_families(plan, self.rfq.cache.store)
        to_scrape = [
            f for f in classified
            if (only is None or pipeline.normalize_query_key(f["search_query"]) in only)
            and (req.force_refresh or not f["is_cached"])
        ]
        if not to_scrape:
            raise ApiError(400, "nothing_to_scrape", "All selected families are already cached; nothing to fetch")

        rate = pipeline.credits_per_page(req.stealth)
        estimated = len(to_scrape) * req.pages * rate
        projected = {
            "total_families": len(plan),
            "cached_families": sum(1 for f in classified if f["is_cached"]),
            "families_to_scrape": len(to_scrape),
            "pages": req.pages,
            "stealth": req.stealth,
            "rate_per_page": rate,
            "estimated_credits": estimated,
        }

        cap_job = self.settings.max_credits_per_job
        if cap_job is not None and estimated > cap_job:
            raise ApiError(400, "credit_cap_exceeded", f"Estimated {estimated} credits exceeds MAX_CREDITS_PER_JOB={cap_job}")
        cap_day = self.settings.max_credits_per_day
        remaining_day: Optional[int] = None
        if cap_day is not None:
            remaining_day = cap_day - self._credits_today()
            if estimated > remaining_day:
                raise ApiError(400, "credit_cap_exceeded", f"Estimated {estimated} credits exceeds the remaining daily budget ({max(remaining_day, 0)})")

        job_id = uuid.uuid4().hex
        scrape_keys = {pipeline.normalize_query_key(f["search_query"]) for f in to_scrape}
        conflict = self.guard.try_acquire(user.id, job_id, scrape_keys)
        if conflict:
            raise ApiError(409, "job_conflict", conflict)

        job = Job(
            job_id=job_id, user=user, projected=projected, total_families=len(plan),
            rfq_file_id=req.rfq_file_id, granularity=built["granularity"],
        )
        caps = [c for c in (cap_job, remaining_day) if c is not None]
        with self._registry_lock:
            self._jobs[job_id] = job
        self._persist(job)
        thread = threading.Thread(
            target=self._run,
            args=(job, plan, req, api_key, scrape_keys, min(caps) if caps else None),
            name=f"scrape-job-{job_id[:8]}",
            daemon=True,
        )
        with self._registry_lock:
            self._threads[job_id] = thread
        thread.start()
        return job

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _persist(self, job: Job) -> None:
        try:
            pipeline.write_json_atomic(self.paths.job_path(job.job_id), job.snapshot())
        except Exception:  # persistence must never kill the worker
            log.exception("Could not persist job %s", job.job_id)
        snap = job.snapshot()
        self.mirror.upsert(
            job.user, "scrape_jobs", "job_id", job.job_id,
            {
                "status": snap["status"],
                "total_families": job.total_families,
                "completed_families": job.completed_families,
                "credits_spent": job.credits_spent,
                "error": job.error or "",
            },
        )

    def _on_event(self, job: Job, event: dict[str, Any]) -> None:
        kind = event["type"]
        if kind == "family_completed":
            with job.lock:
                job.completed_families += 1
                if event["status"] == "cached":
                    job.cache_hits += 1
                elif event["status"] != "missing":
                    job.live_fetches += 1
                job.family_logs.append(
                    {
                        "family_id": event["family_id"],
                        "search_query": event["search_query"],
                        "status": event["status"],
                        "listing_count": event["listing_count"],
                        "credits": event.get("credits", 0),
                        "warnings": event.get("warnings", []),
                        "error": event.get("error"),
                    }
                )
            job.push("family_completed", **{k: v for k, v in event.items() if k != "type"})
            job.push("progress", **self.snapshot_progress(job))
            self._persist(job)
        elif kind == "credit_update":
            with job.lock:
                job.credits_spent = event["credits_spent"]
            job.push("credit_update", credits_spent=event["credits_spent"], page_cost=event["page_cost"],
                     family_id=event["family_id"])
        elif kind == "warning":
            with job.lock:
                job.warnings.append(f"[{event.get('family_id', '-')}] {event['message']}")
            job.push("warning", family_id=event.get("family_id"), message=event["message"])

    @staticmethod
    def snapshot_progress(job: Job) -> dict[str, Any]:
        with job.lock:
            return {
                "completed_families": job.completed_families,
                "total_families": job.total_families,
                "cache_hits": job.cache_hits,
                "live_fetches": job.live_fetches,
                "credits_spent": job.credits_spent,
            }

    def _run(self, job: Job, plan, req: ScrapeJobRequest, api_key: str, scrape_keys: set[str],
             credit_cap: Optional[int]) -> None:
        kwargs: dict[str, Any] = {}
        if self._sleep_fn is not None:
            kwargs["sleep_fn"] = self._sleep_fn
        if self._scrape_fn is not None:
            kwargs["scrape_fn"] = self._scrape_fn
        try:
            with job.lock:
                job.status = "running"
            job.push("progress", **self.snapshot_progress(job))
            self._persist(job)
            run = pipeline.scrape_families(
                plan,
                self.rfq.cache.store,
                pages=req.pages,
                stealth=req.stealth,
                max_products=req.max_products_per_page or self.settings.max_products_per_page,
                api_key=api_key,
                force_refresh=req.force_refresh,
                only_queries=scrape_keys,
                debug_dir=str(self.paths.debug_dir),
                delay_seconds=self.settings.request_delay_seconds,
                credit_cap=credit_cap,
                on_event=lambda e: self._on_event(job, e),
                should_cancel=job.cancel.is_set,
                quiet=True,
                **kwargs,
            )
            failed = sum(1 for o in run.outcomes if o.status in ("failed", "empty"))
            if run.cancelled:
                final, err = "cancelled", None
            elif run.live_fetches and failed == run.live_fetches:
                final, err = "failed", "Every live fetch failed; see family logs"
            else:
                final, err = "completed", (f"{failed} family(ies) failed or returned no listings" if failed else None)
            self._finish(job, final, err)
        except Exception as exc:
            msg = pipeline.redact(f"{type(exc).__name__}: {exc}", api_key)
            log.error("Job %s crashed: %s", job.job_id, msg)
            self._finish(job, "failed", msg)

    def _finish(self, job: Job, status: str, error: Optional[str]) -> None:
        # Append the terminal event and flip the status atomically so SSE readers that
        # observe a terminal status are guaranteed to find the terminal event.
        with job.lock:
            job.status = status
            job.error = error
            job.finished_at = _now()
            job.events.append(
                {
                    "event": status,
                    "data": {
                        "completed_families": job.completed_families,
                        "total_families": job.total_families,
                        "cache_hits": job.cache_hits,
                        "live_fetches": job.live_fetches,
                        "credits_spent": job.credits_spent,
                        "error": error,
                    },
                }
            )
        self.guard.release(job.user.id, job.job_id)
        self._persist(job)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def _load(self, user: AuthUser, job_id: str) -> tuple[Optional[Job], dict[str, Any]]:
        path = self.paths.job_path(job_id)  # validates the id format
        with self._registry_lock:
            job = self._jobs.get(job_id)
        if job is not None:
            if job.user.id != user.id:
                raise ApiError(404, "job_not_found", "Job not found")
            return job, job.snapshot()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ApiError(404, "job_not_found", "Job not found") from exc
        if data.get("owner_id") != user.id:
            raise ApiError(404, "job_not_found", "Job not found")
        return None, data

    def get(self, user: AuthUser, job_id: str) -> dict[str, Any]:
        return self._load(user, job_id)[1]

    def active_for(self, user: AuthUser) -> Optional[dict[str, Any]]:
        with self._registry_lock:
            mine = [j for j in self._jobs.values() if j.user.id == user.id]
        for job in mine:
            snap = job.snapshot()
            if snap["status"] not in TERMINAL:
                return snap
        return None

    def cancel(self, user: AuthUser, job_id: str) -> dict[str, Any]:
        job, snap = self._load(user, job_id)
        if job is None or snap["status"] in TERMINAL:
            raise ApiError(409, "job_not_active", f"Job is already {snap['status']}")
        job.cancel.set()
        return job.snapshot()

    def events_source(self, user: AuthUser, job_id: str) -> tuple[Optional[Job], dict[str, Any]]:
        return self._load(user, job_id)
