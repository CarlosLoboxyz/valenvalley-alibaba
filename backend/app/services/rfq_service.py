"""RFQ upload validation and zero-credit dry-run grouping. All methods are blocking."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Optional

from ..core.auth import AuthUser
from ..core.config import Settings
from ..core.errors import ApiError
from ..core.paths import DEFAULT_RFQ_ID, AppPaths
from ..schemas.rfq import GroupRequest, TargetSlice
from .cache_service import CacheService
from .pipeline_bridge import InputError, dfg, inspect_workbook, pipeline, preview_items
from .pocketbase_mirror import PocketBaseMirror

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")
_CHUNK = 1024 * 1024


def sanitize_filename(name: str) -> str:
    base = Path((name or "").replace("\\", "/")).name
    base = _SAFE_NAME.sub("_", base).strip(" .") or "rfq.xlsx"
    return base[:120]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RFQService:
    def __init__(
        self,
        settings: Settings,
        paths: AppPaths,
        cache: CacheService,
        mirror: PocketBaseMirror,
    ):
        self.settings = settings
        self.paths = paths
        self.cache = cache
        self.mirror = mirror

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------
    def save_upload(self, user: AuthUser, filename: str, stream: BinaryIO) -> dict[str, Any]:
        if not (filename or "").lower().endswith(".xlsx"):
            raise ApiError(400, "invalid_file_type", "Only .xlsx files are accepted")
        clean_name = sanitize_filename(filename)

        self.paths.ensure()
        upload_id = uuid.uuid4().hex
        final = self.paths.upload_path(upload_id)
        tmp = final.with_name(final.stem + ".part.xlsx")  # openpyxl requires an .xlsx suffix
        limit = self.settings.max_upload_mb * 1024 * 1024

        try:
            size = 0
            with open(tmp, "wb") as out:
                while chunk := stream.read(_CHUNK):
                    size += len(chunk)
                    if size > limit:
                        raise ApiError(413, "file_too_large", f"File exceeds {self.settings.max_upload_mb} MB")
                    out.write(chunk)
            if size == 0:
                raise ApiError(400, "empty_file", "Uploaded file is empty")
            with open(tmp, "rb") as f:
                if f.read(4) != b"PK\x03\x04":
                    raise ApiError(400, "invalid_rfq", "File is not a valid .xlsx workbook")
            try:
                info = inspect_workbook(tmp)
            except InputError as exc:
                raise ApiError(400, "invalid_rfq", str(exc)) from exc
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)

        meta = {
            "upload_id": upload_id,
            "owner_id": user.id,
            "filename": clean_name,
            "row_count": len(info["items"]),
            "sheets": info["sheets"],
            "sheet": info["sheet"],
            "created_at": _now(),
        }
        self.paths.upload_meta_path(upload_id).write_text(json.dumps(meta), encoding="utf-8")
        self.mirror.upsert(
            user, "rfq_files", "upload_id", upload_id,
            {"filename": clean_name, "row_count": meta["row_count"], "sheets": info["sheets"]},
        )
        return {
            "upload_id": upload_id,
            "filename": clean_name,
            "row_count": meta["row_count"],
            "sheets": info["sheets"],
            "sheet": info["sheet"],
            "warnings": info["warnings"],
            "preview": preview_items(info["items"]),
        }

    # ------------------------------------------------------------------
    # Listing / resolution
    # ------------------------------------------------------------------
    def list_files(self, user: AuthUser) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        if self.paths.default_rfq.is_file():
            files.append(
                {"rfq_file_id": DEFAULT_RFQ_ID, "filename": self.paths.default_rfq.name, "is_default": True}
            )
        if self.paths.uploads_dir.is_dir():
            for meta_path in sorted(self.paths.uploads_dir.glob("*.json")):
                meta = self._read_meta(meta_path)
                if meta and meta.get("owner_id") == user.id and self.paths.upload_path(meta["upload_id"]).is_file():
                    files.append(
                        {
                            "rfq_file_id": meta["upload_id"],
                            "filename": meta["filename"],
                            "row_count": meta.get("row_count"),
                            "is_default": False,
                            "created_at": meta.get("created_at"),
                        }
                    )
        return files

    @staticmethod
    def _read_meta(path: Path) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and "upload_id" in data else None
        except (OSError, ValueError):
            return None

    def resolve(self, user: AuthUser, rfq_file_id: str) -> tuple[Path, Optional[str]]:
        """Map an opaque id to an allowlisted path. Non-owners get 404 (no id probing)."""
        if rfq_file_id in ("", DEFAULT_RFQ_ID):
            if not self.paths.default_rfq.is_file():
                raise ApiError(404, "rfq_not_found", "Default RFQ file not found")
            return self.paths.default_rfq, None
        path = self.paths.upload_path(rfq_file_id)
        meta = self._read_meta(self.paths.upload_meta_path(rfq_file_id))
        if not path.is_file() or not meta or meta.get("owner_id") != user.id:
            raise ApiError(404, "rfq_not_found", "RFQ file not found")
        return path, meta.get("sheet")

    # ------------------------------------------------------------------
    # Shared plan builder (also used by scoring and scrape jobs)
    # ------------------------------------------------------------------
    def build_plan(self, user: AuthUser, target: TargetSlice) -> dict[str, Any]:
        path, sheet = self.resolve(user, target.rfq_file_id)
        granularity = target.granularity or self.settings.family_granularity
        warnings: list[str] = []
        try:
            if sheet is None:  # default file: first sheet for both loaders
                sheet = inspect_sheet_name(path)
            rfq_items, df = pipeline.load_and_filter(
                str(path), sheet_name=sheet, positions=target.positions,
                limit=target.limit, warnings=warnings,
            )
            tagged_df, plan = pipeline.build_family_plan(df, granularity=granularity)
        except InputError as exc:
            raise ApiError(400, "invalid_request", str(exc)) from exc
        return {
            "path": path,
            "rfq_items": rfq_items,
            "tagged_df": tagged_df,
            "plan": plan,
            "granularity": granularity,
            "warnings": warnings,
            "source_name": path.name,
        }

    # ------------------------------------------------------------------
    # Dry-run grouping (0 credits)
    # ------------------------------------------------------------------
    def group(self, user: AuthUser, req: GroupRequest) -> dict[str, Any]:
        built = self.build_plan(user, req)
        plan = built["plan"]

        user_dir = self.paths.user_dir(user.id)
        pipeline.write_json_atomic(user_dir / "family_search_plan.json", plan)
        dfg.export_excel(built["tagged_df"], plan, str(user_dir / "products_grouped.xlsx"))

        families = pipeline.classify_families(plan, self.cache.store)
        cached = sum(1 for f in families if f["is_cached"])
        uncached = len(families) - cached
        premium = uncached * req.pages * pipeline.CREDITS_PER_PAGE_PREMIUM
        stealth = uncached * req.pages * pipeline.CREDITS_PER_PAGE_STEALTH
        n_items = len(built["tagged_df"])
        return {
            "total_items": n_items,
            "family_count": len(plan),
            "api_reduction_pct": pipeline.reduction_pct(n_items, len(plan)),
            "cached_families": cached,
            "uncached_families": uncached,
            "pages": req.pages,
            "stealth": req.stealth,
            "estimated_credits_premium": premium,
            "estimated_credits_stealth": stealth,
            "estimated_credits_selected": stealth if req.stealth else premium,
            "granularity": built["granularity"],
            "families": families,
            "warnings": built["warnings"],
        }


def inspect_sheet_name(path: Path) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(filename=path, read_only=True)
    try:
        return wb.sheetnames[0]
    except IndexError as exc:
        raise InputError("Workbook has no sheets") from exc
    finally:
        wb.close()
