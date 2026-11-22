"""Importable orchestration for the Alibaba sourcing pipeline.

Both the ``main.py`` CLI and the FastAPI backend call into this module. It has no
argparse, no ``sys.exit`` and no reliance on mutable module globals for per-run
settings: everything is passed explicitly.

Credit safety: nothing here spends ScrapingBee credits unless ``scrape_families``
is called with ``from_cache=False`` and a non-empty ``api_key``.
"""

from __future__ import annotations

import functools
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

import dynamic_family_grouper as dfg
import process_rfq_excel as pfe
import recommend_vendors as rv
import scrape_alibaba as sa
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from pipeline_errors import InputError

VALID_GRANULARITIES = ("fine", "balanced", "broad")
CREDITS_PER_PAGE_PREMIUM = 30
CREDITS_PER_PAGE_STEALTH = 80
DEFAULT_QUERY_SUFFIX = "wholesale OEM"


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def credits_per_page(stealth: bool) -> int:
    return CREDITS_PER_PAGE_STEALTH if stealth else CREDITS_PER_PAGE_PREMIUM


def normalize_query_key(query: str) -> str:
    """Cache key for a family search query (matches the historical CLI format)."""
    return query.strip().lower()


def parse_positions_arg(spec: str) -> set[int]:
    """Parse expressions like '1-10' or '1,2,5,8-12' into a set of position ints."""
    selected: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start_s, end_s = part.split("-", 1)
                start, end = int(start_s.strip()), int(end_s.strip())
                selected.update(range(min(start, end), max(start, end) + 1))
            else:
                selected.add(int(part))
        except ValueError as exc:
            raise InputError(f"Invalid positions expression: {spec!r}") from exc
    return selected


def validate_granularity(granularity: str) -> str:
    if granularity not in VALID_GRANULARITIES:
        raise InputError(
            f"Invalid granularity {granularity!r}; expected one of {list(VALID_GRANULARITIES)}"
        )
    return granularity


def redact(text: str, secret: Optional[str]) -> str:
    """Strip a secret from free text before it is logged or returned."""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


# ----------------------------------------------------------------------
# Cache store (query-keyed, atomic, lock-guarded)
# ----------------------------------------------------------------------
class CacheStore(Protocol):
    def get(self, key: str) -> Optional[dict[str, Any]]: ...

    def put(self, key: str, entry: dict[str, Any]) -> None: ...


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


class FileCacheStore:
    """JSON cache keyed by normalized query, with atomic writes.

    Every ``put`` is a locked read-modify-write followed by ``os.replace`` so that
    concurrent writers (in this process) and interrupted jobs never corrupt the file.
    The on-disk format is identical to the one the CLI has always used.
    """

    def __init__(self, path: str | Path, lock: Optional[threading.RLock] = None):
        self.path = Path(path)
        self.lock = lock or _lock_for(self.path)

    def load(self) -> dict[str, Any]:
        with self.lock:
            return pfe.load_cache(str(self.path))

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pfe.save_cache(str(self.path), data)  # temp file + os.replace

    def get(self, key: str) -> Optional[dict[str, Any]]:
        entry = self.load().get(key)
        return entry if isinstance(entry, dict) else None

    def put(self, key: str, entry: dict[str, Any]) -> None:
        with self.lock:
            data = pfe.load_cache(str(self.path))
            data[key] = entry
            self._write(data)

    def delete(self, key: str) -> bool:
        with self.lock:
            data = pfe.load_cache(str(self.path))
            if key not in data:
                return False
            del data[key]
            self._write(data)
            return True


def has_usable_entry(entry: Any) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("products"), list)


def normalize_listing(item: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a legacy raw-shaped listing (moq_raw/supplier_url/str years) to the normalized shape.

    Caches written before the duplicate ``clean_products`` bug was fixed contain raw
    AI keys. Already-normalized listings are returned untouched.
    """
    raw_markers = ("moq_raw", "supplier_url")
    needs = (
        any(k in item for k in raw_markers)
        or isinstance(item.get("years_active"), str)
        or isinstance(item.get("certifications"), str)
    )
    if not needs:
        return item
    moq_val, moq_unit = sa.parse_moq(item.get("moq_raw") or item.get("moq"))
    return {
        "title": item.get("title"),
        "price": item.get("price"),
        "moq": item.get("moq") if isinstance(item.get("moq"), (int, float)) else moq_val,
        "moq_unit": item.get("moq_unit") or moq_unit,
        "supplier": item.get("supplier"),
        "supplier_id": item.get("supplier_id") or sa.extract_supplier_id(item.get("supplier_url")),
        "years_active": sa.parse_years_active(item.get("years_active")),
        "certifications": sa.parse_certifications(item.get("certifications")),
        "has_trade_assurance": sa.parse_bool(item.get("has_trade_assurance")),
        "url": item.get("url"),
    }


def normalize_listings(items: list[Any]) -> list[dict[str, Any]]:
    return [normalize_listing(i) for i in items if isinstance(i, dict)]


# ----------------------------------------------------------------------
# Steps 1-2: load, filter, cluster (0 credits)
# ----------------------------------------------------------------------
def load_and_filter(
    excel_path: str,
    sheet_name: Optional[str] = None,
    positions: Optional[str] = None,
    limit: Optional[int] = None,
    warnings: Optional[list[str]] = None,
):
    """Load the RFQ workbook twice (RFQ items + catalog DataFrame) and apply filters."""
    rfq_items = pfe.load_rfq_excel(excel_path, sheet_name=sheet_name, warnings=warnings)
    df = dfg.load_catalog(excel_path, sheet_name=sheet_name)

    if positions:
        selected = parse_positions_arg(positions)
        rfq_items = [it for it in rfq_items if it["position"] in selected]
        df = df[df["position"].astype(int).isin(selected)].copy()

    if limit is not None:
        rfq_items = rfq_items[:limit]
        keep_pos = {it["position"] for it in rfq_items}
        df = df[df["position"].astype(int).isin(keep_pos)].head(limit).copy()

    df = df.reset_index(drop=True)
    if df.empty or not rfq_items:
        raise InputError("No product rows remain after applying filters.")
    return rfq_items, df


def build_family_plan(
    df,
    granularity: str = "balanced",
    stage2_sim: Optional[float] = None,
    query_suffix: str = DEFAULT_QUERY_SUFFIX,
):
    """Cluster locally (no LLM) and return ``(tagged_df, family_plan)``."""
    validate_granularity(granularity)
    vdf = dfg.extract_views(df)
    s_strict, s_broad = dfg.compute_similarity_matrices(df, vdf)
    labels = dfg.cluster_products(
        s_strict, s_broad, granularity=granularity, stage2_min_sim=stage2_sim
    )
    return dfg.build_plan(df, vdf, labels, s_strict, query_suffix=query_suffix)


def reduction_pct(n_products: int, n_families: int) -> float:
    return round((1 - n_families / n_products) * 100, 1) if n_products else 0.0


def read_family_plan(path: str | Path) -> list[dict[str, Any]]:
    """Read ``family_search_plan.json`` in either the list (main.py) or dict (dfg CLI) shape."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("families", [])
    return data if isinstance(data, list) else []


def write_json_atomic(path: str | Path, data: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def classify_families(
    family_plan: list[dict[str, Any]], store: Optional[CacheStore]
) -> list[dict[str, Any]]:
    """Annotate each family with cache status (read-only, 0 credits)."""
    out = []
    for fam in family_plan:
        entry = store.get(normalize_query_key(fam["search_query"])) if store else None
        cached = has_usable_entry(entry)
        out.append(
            {
                **fam,
                "is_cached": cached,
                "cached_listing_count": len(entry["products"]) if cached else 0,
            }
        )
    return out


# ----------------------------------------------------------------------
# Step 3: scrape loop (the only place credits are spent)
# ----------------------------------------------------------------------
@dataclass
class FamilyOutcome:
    family_id: str
    search_query: str
    # cached | scraped | failed | empty | missing | cancelled | skipped
    status: str
    listing_count: int = 0
    credits: int = 0
    pages_ok: int = 0
    pages_failed: int = 0
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class ScrapeRun:
    scraped_by_family: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    outcomes: list[FamilyOutcome] = field(default_factory=list)
    credits_spent: int = 0
    cache_hits: int = 0
    live_fetches: int = 0
    cancelled: bool = False

    @property
    def scraped_counts(self) -> dict[str, int]:
        return {fid: len(p) for fid, p in self.scraped_by_family.items()}


EventCallback = Callable[[dict[str, Any]], None]


def scrape_families(
    family_plan: list[dict[str, Any]],
    store: CacheStore,
    *,
    pages: int,
    stealth: bool,
    max_products: int,
    api_key: Optional[str],
    from_cache: bool = False,
    force_refresh: bool = False,
    only_queries: Optional[set[str]] = None,
    debug_dir: Optional[str] = None,
    delay_seconds: float = 0.0,
    credit_cap: Optional[int] = None,
    on_event: Optional[EventCallback] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    scrape_fn: Optional[Callable[..., Any]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    quiet: bool = True,
) -> ScrapeRun:
    """Resolve listings for every family: cache hit, or one live fetch per page.

    * A family is checkpointed to the cache immediately after it finishes, but only if
      every page succeeded and at least one listing was returned (failed/empty results
      are never cached, so they are retried on the next run).
    * ``only_queries`` (normalized keys) restricts which families may hit ScrapingBee;
      all others resolve from cache only.
    * ``should_cancel`` is checked between families and between pages. A request that
      is already in flight cannot be interrupted.
    * ``credit_cap`` stops live fetching before a page that could exceed the cap.
    """
    scrape = scrape_fn or functools.partial(sa.scrape_term_detailed, quiet=quiet)
    run = ScrapeRun()
    rate = credits_per_page(stealth)

    def emit(event: dict[str, Any]) -> None:
        if on_event:
            on_event(event)

    total = len(family_plan)
    for idx, fam in enumerate(family_plan, start=1):
        fid, query = fam["family_id"], fam["search_query"]
        key = normalize_query_key(query)

        if should_cancel and should_cancel():
            run.cancelled = True
            run.outcomes.append(FamilyOutcome(fid, query, "cancelled"))
            continue

        entry = store.get(key)
        cached_ok = has_usable_entry(entry)
        allowed_live = only_queries is None or key in only_queries
        use_cache = cached_ok and not (force_refresh and allowed_live)

        if use_cache or from_cache or not allowed_live:
            products = normalize_listings(entry["products"]) if cached_ok else []
            run.scraped_by_family[fid] = products
            status = "cached" if cached_ok else "missing"
            if cached_ok:
                run.cache_hits += 1
            run.outcomes.append(FamilyOutcome(fid, query, status, listing_count=len(products)))
            emit({"type": "family_completed", "family_id": fid, "search_query": query,
                  "status": status, "listing_count": len(products), "index": idx, "total": total,
                  "credits": 0, "warnings": [], "error": None})
            continue

        if not api_key:
            raise InputError("SCRAPINGBEE_API_KEY is missing (or use from_cache).")

        outcome = FamilyOutcome(fid, query, "scraped")
        run.live_fetches += 1
        products: list[dict[str, Any]] = []
        stopped = False
        for page in range(1, pages + 1):
            if should_cancel and should_cancel():
                run.cancelled = True
                stopped = True
                outcome.status = "cancelled"
                break
            if credit_cap is not None and run.credits_spent + rate > credit_cap:
                stopped = True
                outcome.status = "failed"
                outcome.error = f"Credit cap of {credit_cap} would be exceeded"
                outcome.warnings.append(outcome.error)
                emit({"type": "warning", "family_id": fid, "message": outcome.error})
                break
            try:
                result = scrape(
                    term=query, api_key=api_key, page=page, stealth=stealth,
                    max_products=max_products, debug_dir=debug_dir,
                )
            except Exception as exc:  # network, HTTP, or parse failure for this page
                outcome.pages_failed += 1
                resp = getattr(exc, "response", None)
                if resp is not None:
                    detail = f"HTTP {resp.status_code}: {str(resp.text)[:200]}"
                else:
                    detail = f"{type(exc).__name__}: {exc}"
                msg = f"Page {page} failed: {redact(detail, api_key)}"
                outcome.warnings.append(msg)
                emit({"type": "warning", "family_id": fid, "message": msg})
            else:
                outcome.pages_ok += 1
                page_products = getattr(result, "products", result)
                products.extend(page_products)
                cost = getattr(result, "spb_cost", None)
                if cost is None:
                    outcome.warnings.append(f"Page {page}: Spb-cost header missing; credits not counted")
                else:
                    outcome.credits += cost
                    run.credits_spent += cost
                    emit({"type": "credit_update", "family_id": fid,
                          "credits_spent": run.credits_spent, "page_cost": cost})
                if getattr(result, "repaired", False):
                    path = getattr(result, "debug_path", None)
                    msg = f"Page {page}: truncated JSON repaired" + (f" (raw saved: {Path(path).name})" if path else "")
                    outcome.warnings.append(msg)
                    emit({"type": "warning", "family_id": fid, "message": msg})
            if page < pages:
                sleep_fn(delay_seconds)

        if stopped and outcome.status == "cancelled":
            run.scraped_by_family[fid] = products
            outcome.listing_count = len(products)
            run.outcomes.append(outcome)
            continue

        outcome.listing_count = len(products)
        run.scraped_by_family[fid] = products
        if outcome.pages_failed or stopped:
            outcome.status = "failed"
            outcome.error = outcome.error or "; ".join(outcome.warnings[-1:])
        elif not products:
            outcome.status = "empty"
            outcome.warnings.append("0 listings returned; not cached so it will be retried")
        else:
            store.put(
                key,
                {
                    "family_id": fid,
                    "family_name": fam["family_name"],
                    "search_query": query,
                    "products": products,
                    "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "pages": pages,
                    "stealth": stealth,
                    "credits": outcome.credits,
                },
            )
        run.outcomes.append(outcome)
        emit({"type": "family_completed", "family_id": fid, "search_query": query,
              "status": outcome.status, "listing_count": len(products), "index": idx,
              "total": total, "credits": outcome.credits, "warnings": list(outcome.warnings),
              "error": outcome.error})
        if idx < total:
            sleep_fn(delay_seconds)

    return run


# ----------------------------------------------------------------------
# Step 4: scoring (0 credits)
# ----------------------------------------------------------------------
@dataclass
class ScoreOptions:
    top_n: int = 5
    required_certs: list[str] = field(default_factory=list)
    min_years: int = 0
    require_trade_assurance: bool = False
    strict_moq: bool = False
    strict_certs: bool = False
    strict_min_price: bool = False
    max_price: Optional[float] = None
    weights: Optional[dict[str, float]] = None  # price/moq/years_active/trade_assurance/certifications
    weight_vendor_score: Optional[float] = None
    weight_rfq_fit: Optional[float] = None


def score_positions(
    rfq_items: list[dict[str, Any]],
    family_plan: list[dict[str, Any]],
    scraped_by_family: dict[str, list[dict[str, Any]]],
    options: ScoreOptions,
) -> list[dict[str, Any]]:
    pos_to_family: dict[int, dict[str, Any]] = {}
    for fam in family_plan:
        for pos in fam["positions"]:
            pos_to_family[int(pos)] = fam

    report: list[dict[str, Any]] = []
    for item in rfq_items:
        fam = pos_to_family.get(int(item["position"]))
        fid = fam["family_id"] if fam else "UNASSIGNED"
        listings = normalize_listings(scraped_by_family.get(fid, []))

        evaluated = pfe.evaluate_rfq_position(
            item=item,
            listings=listings,
            global_required_certs=list(options.required_certs),
            min_years=options.min_years,
            require_ta=options.require_trade_assurance,
            strict_moq=options.strict_moq,
            strict_certs=options.strict_certs,
            strict_min_price=options.strict_min_price,
            weights=options.weights,
            weight_vendor_score=options.weight_vendor_score,
            weight_rfq_fit=options.weight_rfq_fit,
            global_max_price=options.max_price,
        )
        evaluated["family_id"] = fid
        evaluated["family_name"] = fam["family_name"] if fam else "Unassigned"
        evaluated["family_search_query"] = fam["search_query"] if fam else item["search_query"]
        report.append(evaluated)
    return report


def build_cross_item_vendor_summary(positions_report: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    """Identify qualified vendors that cover multiple products/families for shipment consolidation."""
    buckets: dict[str, dict[str, Any]] = {}

    for entry in positions_report:
        item = entry["rfq_item"]
        fam_id = entry["family_id"]
        for v in entry["recommended"][:top_n]:
            key = v["vendor_key"]
            if key not in buckets:
                buckets[key] = {
                    "supplier": v["supplier"],
                    "supplier_id": v["supplier_id"] or "",
                    "years_active": v["years_active"],
                    "has_trade_assurance": v["has_trade_assurance"],
                    "certifications": set(v["certifications"]),
                    "positions": [],
                    "item_names": [],
                    "families": set(),
                    "scores": [],
                }
            b = buckets[key]
            b["positions"].append(item["position"])
            b["item_names"].append(item["name"])
            b["families"].add(fam_id)
            b["scores"].append(v["total_score"])
            b["certifications"].update(v["certifications"])
            if v["years_active"] is not None:
                b["years_active"] = max(b["years_active"] or 0, v["years_active"])
            b["has_trade_assurance"] = b["has_trade_assurance"] or v["has_trade_assurance"]

    summary = []
    for b in buckets.values():
        summary.append(
            {
                "supplier": b["supplier"],
                "supplier_id": b["supplier_id"],
                "items_covered": len(b["positions"]),
                "families_covered": len(b["families"]),
                "avg_score": round(sum(b["scores"]) / len(b["scores"]), 1),
                "years_active": b["years_active"],
                "has_trade_assurance": b["has_trade_assurance"],
                "certifications": sorted(b["certifications"]),
                "positions": sorted(b["positions"]),
                "families": sorted(b["families"]),
                "item_names": b["item_names"],
            }
        )

    summary.sort(
        key=lambda x: (x["items_covered"], x["avg_score"], x["has_trade_assurance"]),
        reverse=True,
    )
    return summary


def build_report(
    positions_report: list[dict[str, Any]],
    *,
    source_excel: str,
    granularity: str,
    total_products: int,
    total_families: int,
    pages: int,
    stealth: bool,
    options: ScoreOptions,
    credits_spent: int = 0,
    family_plan: Optional[list[dict[str, Any]]] = None,
    scraped_counts: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    """Assemble the report dict written to final_recommendations.json."""
    report: dict[str, Any] = {
        "meta": {
            "source_excel": source_excel,
            "granularity": granularity,
            "total_products": total_products,
            "total_families": total_families,
            "api_call_reduction_pct": reduction_pct(total_products, total_families),
            "pages_per_family": pages,
            "proxy_mode": "stealth" if stealth else "premium",
            "top_n": options.top_n,
            "credits_spent": credits_spent,
            "filters": {
                "required_certs": list(options.required_certs),
                "min_years": options.min_years,
                "require_trade_assurance": options.require_trade_assurance,
                "strict_moq": options.strict_moq,
                "strict_certs": options.strict_certs,
                "strict_min_price": options.strict_min_price,
                "max_price": options.max_price,
            },
        },
        "positions": positions_report,
        "cross_item_vendors": build_cross_item_vendor_summary(positions_report, options.top_n),
    }
    if family_plan is not None:
        counts = scraped_counts or {}
        report["family_search_plan"] = [
            {**fam, "listings_scraped": counts.get(fam["family_id"], 0)} for fam in family_plan
        ]
    return report


# ----------------------------------------------------------------------
# Step 5: Excel export (4 tabs)
# ----------------------------------------------------------------------
def export_final_excel(
    report: dict[str, Any],
    family_plan: list[dict[str, Any]],
    scraped_counts: dict[str, int],
    output_path: Path,
    top_n: int,
) -> None:
    """Export 4-tab client-ready Excel report combining family plan + per-item vendor picks."""
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")

    def style_header(ws):
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
        ws.freeze_panes = "A2"

    # Tab 1: Per-Product Recommendations
    ws_rec = wb.active
    ws_rec.title = "Recommendations"
    ws_rec.append([
        "Position", "Family ID", "Family Name", "Item Name", "Client Qty", "Client Unit",
        "Client Budget ($)", "Client Brand", "Rank", "Recommendation", "Final Score",
        "Vendor Score", "RFQ Fit Score", "Supplier", "Supplier ID", "Vendor Price",
        "Vendor MOQ", "Vendor Unit", "Years Active", "Trade Assurance",
        "Certifications", "Brand Status", "Pros", "Warnings",
        "Matched Product Title", "URL",
    ])
    style_header(ws_rec)

    for entry in report["positions"]:
        item = entry["rfq_item"]
        budget = f"{item['min_price'] or ''} - {item['max_price'] or ''}".strip(" -")
        recs = entry["recommended"][:top_n]

        if not recs:
            ws_rec.append([
                item["position"], entry["family_id"], entry["family_name"], item["name"],
                item["qty"], item["unit"] or "", budget, item["brand_raw"] or "",
                "-", "No Qualified Vendors", "", "", "", "", "", "", "", "", "", "",
                "", "", "", "All vendors disqualified or 0 listings scraped", "", "",
            ])
            continue

        for rank, v in enumerate(recs, start=1):
            offer = v["best_offer"]
            ws_rec.append([
                item["position"],
                entry["family_id"],
                entry["family_name"],
                item["name"],
                item["qty"],
                item["unit"] or "",
                budget,
                item["brand_raw"] or "",
                rank,
                v["recommendation"],
                v["total_score"],
                v["vendor_score"],
                v["rfq_fit_score"],
                v["supplier"],
                v["supplier_id"] or "",
                offer["price_raw"] or "",
                int(offer["moq"]) if offer["moq"] is not None else "",
                offer["moq_unit"] or "",
                v["years_active"] if v["years_active"] is not None else "",
                "Yes" if v["has_trade_assurance"] else "No",
                ", ".join(v["certifications"]),
                v["brand_status"],
                " | ".join(v["pros"]),
                " | ".join(v["warnings"]),
                offer["title"],
                offer["url"] or "",
            ])

    # Tab 2: Deduplicated Family Search Plan
    ws_fam = wb.create_sheet("Family_Search_Plan")
    ws_fam.append([
        "Family ID", "Family Name", "Alibaba Search Query", "Item Count",
        "Cohesion (%)", "Listings Scraped", "Positions", "Total Qty",
        "Min Price", "Max Price", "Sample Products",
    ])
    style_header(ws_fam)

    for fam in family_plan:
        ws_fam.append([
            fam["family_id"],
            fam["family_name"],
            fam["search_query"],
            fam["item_count"],
            fam["cohesion_score"],
            scraped_counts.get(fam["family_id"], 0),
            ", ".join(str(p) for p in fam["positions"]),
            fam["total_qty"],
            fam["min_price"],
            fam["max_price"],
            " | ".join(fam["sample_items"]),
        ])

    # Tab 3: Cross-Item Vendor Consolidation
    ws_cross = wb.create_sheet("Cross_Item_Vendors")
    ws_cross.append([
        "Supplier", "Supplier ID", "Items Covered", "Families Covered", "Avg Score",
        "Years Active", "Trade Assurance", "Certifications", "Positions Covered",
        "Family IDs", "Item Names",
    ])
    style_header(ws_cross)

    for v in report["cross_item_vendors"]:
        ws_cross.append([
            v["supplier"],
            v["supplier_id"],
            v["items_covered"],
            v["families_covered"],
            v["avg_score"],
            v["years_active"] if v["years_active"] is not None else "",
            "Yes" if v["has_trade_assurance"] else "No",
            ", ".join(v["certifications"]),
            ", ".join(str(p) for p in v["positions"]),
            ", ".join(v["families"]),
            " | ".join(v["item_names"]),
        ])

    # Tab 4: Disqualified Vendors
    ws_disq = wb.create_sheet("Disqualified")
    ws_disq.append([
        "Position", "Family ID", "Item Name", "Supplier", "Supplier ID",
        "Vendor Price", "Vendor MOQ", "Disqualification Reasons", "URL",
    ])
    style_header(ws_disq)

    for entry in report["positions"]:
        item = entry["rfq_item"]
        for d in entry["disqualified"]:
            offer = d["best_offer"]
            ws_disq.append([
                item["position"],
                entry["family_id"],
                item["name"],
                d["supplier"],
                d["supplier_id"] or "",
                offer["price_raw"] or "",
                int(offer["moq"]) if offer["moq"] is not None else "",
                "; ".join(d["disqualification_reasons"]),
                offer["url"] or "",
            ])

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_name(output_path.name + ".tmp")
    wb.save(tmp)
    os.replace(tmp, output_path)
