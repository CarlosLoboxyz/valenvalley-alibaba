"""Safe path resolution. Clients never supply filesystem paths, only opaque ids."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import ROOT_DIR, Settings
from .errors import ApiError

UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{32}$")
USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DEBUG_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
DEFAULT_RFQ_ID = "default"


def _is_within(path: Path, base: Path) -> bool:
    return path.resolve().is_relative_to(base.resolve())


def _abs(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT_DIR / p


@dataclass(frozen=True)
class AppPaths:
    root: Path
    output_dir: Path
    uploads_dir: Path
    jobs_dir: Path
    users_dir: Path
    cache_file: Path
    debug_dir: Path
    default_rfq: Optional[Path]

    @classmethod
    def from_settings(cls, settings: Settings) -> "AppPaths":
        out = _abs(settings.output_dir)
        return cls(
            root=ROOT_DIR,
            output_dir=out,
            uploads_dir=out / "uploads",
            jobs_dir=out / "jobs",
            users_dir=out / "users",
            cache_file=out / "family_scrape_cache.json",
            debug_dir=_abs(settings.debug_dir),
            default_rfq=_abs(settings.rfq_excel_file) if settings.rfq_excel_file.strip() else None,
        )

    def ensure(self) -> None:
        for d in (self.output_dir, self.uploads_dir, self.jobs_dir, self.users_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- per-user artifact directory ----
    def user_dir(self, user_id: str) -> Path:
        if not USER_ID_RE.match(user_id):
            raise ApiError(401, "invalid_user", "Invalid user id")
        d = self.users_dir / user_id
        if not _is_within(d, self.users_dir):
            raise ApiError(400, "invalid_path", "Invalid path")
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- RFQ files ----
    def upload_path(self, upload_id: str) -> Path:
        if not UPLOAD_ID_RE.match(upload_id):
            raise ApiError(404, "rfq_not_found", "RFQ file not found")
        p = self.uploads_dir / f"{upload_id}.xlsx"
        if not _is_within(p, self.uploads_dir):
            raise ApiError(404, "rfq_not_found", "RFQ file not found")
        return p

    def upload_meta_path(self, upload_id: str) -> Path:
        return self.upload_path(upload_id).with_suffix(".json")

    # ---- debug files ----
    def debug_file(self, name: str) -> Path:
        if not DEBUG_NAME_RE.match(name):
            raise ApiError(404, "debug_not_found", "Debug file not found")
        p = self.debug_dir / name
        if not _is_within(p, self.debug_dir) or not p.is_file():
            raise ApiError(404, "debug_not_found", "Debug file not found")
        return p

    def job_path(self, job_id: str) -> Path:
        if not re.match(r"^[a-f0-9]{32}$", job_id):
            raise ApiError(404, "job_not_found", "Job not found")
        return self.jobs_dir / f"{job_id}.json"


def display_name(path: Optional[Path]) -> str:
    return path.name if path else ""
