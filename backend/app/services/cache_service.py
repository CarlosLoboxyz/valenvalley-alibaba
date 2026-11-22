"""Atomic, lock-guarded access to ``output/family_scrape_cache.json``."""

from __future__ import annotations

from typing import Any

from ..core.errors import ApiError
from ..core.locks import cache_lock_for
from ..core.paths import AppPaths
from .pipeline_bridge import pipeline


class CacheService:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        # Share the registry lock so jobs, scoring and API edits serialize on one lock per file
        self.store = pipeline.FileCacheStore(paths.cache_file, lock=cache_lock_for(paths.cache_file))

    # ---- reads ----
    def all_entries(self) -> dict[str, Any]:
        try:
            return self.store.load()
        except ValueError as exc:  # corrupt JSON
            raise ApiError(500, "cache_corrupt", "Cache file is unreadable") from exc

    def count(self) -> int:
        try:
            return len(self.all_entries())
        except ApiError:
            return 0

    def list_summaries(self) -> list[dict[str, Any]]:
        out = []
        for key, entry in sorted(self.all_entries().items()):
            if not isinstance(entry, dict):
                continue
            products = pipeline.normalize_listings(entry.get("products") or [])
            suppliers = []
            for p in products:
                s = p.get("supplier")
                if s and s not in suppliers:
                    suppliers.append(s)
                if len(suppliers) >= 3:
                    break
            out.append(
                {
                    "query_key": key,
                    "family_id": entry.get("family_id"),
                    "family_name": entry.get("family_name"),
                    "search_query": entry.get("search_query"),
                    "listing_count": len(products),
                    "sample_suppliers": suppliers,
                    "scraped_at": entry.get("scraped_at"),
                    "pages": entry.get("pages"),
                    "stealth": entry.get("stealth"),
                    "credits": entry.get("credits"),
                }
            )
        return out

    def get_entry(self, query_key: str) -> dict[str, Any]:
        entry = self.all_entries().get(pipeline.normalize_query_key(query_key))
        if not isinstance(entry, dict):
            raise ApiError(404, "cache_not_found", "Cache entry not found")
        return {**entry, "products": pipeline.normalize_listings(entry.get("products") or [])}

    # ---- writes ----
    def delete(self, query_key: str) -> None:
        if not self.store.delete(pipeline.normalize_query_key(query_key)):
            raise ApiError(404, "cache_not_found", "Cache entry not found")
