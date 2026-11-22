import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

# Resolve project root and add ./scripts to sys.path before importing the 4 modules
ROOT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if SCRIPTS_DIR.is_dir() and str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
elif str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Load .env from project root before module constants initialize
load_dotenv(ROOT_DIR / ".env")

import dynamic_family_grouper as dfg
import process_rfq_excel as pfe
import recommend_vendors as rv
import scrape_alibaba as sa


def parse_positions_arg(spec: str) -> set[int]:
    """Parse expressions like '1-10' or '1,2,5,8-12' into a set of position ints."""
    selected: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s.strip()), int(end_s.strip())
            selected.update(range(min(start, end), max(start, end) + 1))
        else:
            selected.add(int(part))
    return selected


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

    wb.save(output_path)


def print_terminal_summary(report: dict[str, Any], top_n: int) -> None:
    meta = report["meta"]
    print("\n" + "=" * 72)
    print(
        f"FINAL RECOMMENDATIONS | {meta['total_products']} products -> "
        f"{meta['total_families']} families ({meta['api_call_reduction_pct']}% fewer API calls)"
    )
    print("=" * 72)

    for entry in report["positions"]:
        item = entry["rfq_item"]
        recs = entry["recommended"]
        disq = entry["disqualified"]
        print(
            f"\n[Pos #{item['position']} | {entry['family_id']}] {item['name']} "
            f"(Qty: {item['qty']} {item['unit'] or ''} | Budget: ${item['min_price'] or '?'}-${item['max_price'] or '?'})"
        )
        print(f"  Family query: \"{entry['family_search_query']}\" -> {len(recs)} qualified, {len(disq)} disqualified")

        if not recs:
            print("  No vendors met the criteria.")
            continue

        for rank, v in enumerate(recs[:top_n], start=1):
            offer = v["best_offer"]
            ta = "Yes" if v["has_trade_assurance"] else "No"
            print(
                f"  #{rank} [{v['total_score']}/100 {v['recommendation']}] {v['supplier']} "
                f"(Vendor:{v['vendor_score']} | Fit:{v['rfq_fit_score']})"
            )
            print(
                f"      Price: {offer['price_raw'] or 'N/A'} | MOQ: {offer['moq']} {offer['moq_unit']} | "
                f"Active: {v['years_active'] or '?'}y | TA: {ta} | Brand: {v['brand_status']}"
            )
            if v["warnings"]:
                print(f"      ! {'; '.join(v['warnings'])}")

    multi_vendors = [v for v in report["cross_item_vendors"] if v["items_covered"] >= 2]
    if multi_vendors:
        print("\n--- TOP MULTI-ITEM VENDORS (Shipment Consolidation Candidates) ---")
        for v in multi_vendors[:5]:
            pos_str = ", ".join(str(p) for p in v["positions"])
            print(
                f"  * {v['supplier']} — covers {v['items_covered']} items (pos: {pos_str}) | "
                f"Avg Score: {v['avg_score']}/100 | TA: {'Yes' if v['has_trade_assurance'] else 'No'}"
            )


def main():
    parser = argparse.ArgumentParser(
        description="End-to-end Alibaba sourcing pipeline: dynamic family grouping -> ScrapingBee -> vendor recommendations."
    )
    parser.add_argument(
        "excel_file",
        nargs="?",
        default=pfe.DEFAULT_RFQ_FILE,
        help=f"Input .xlsx product sheet (default: {pfe.DEFAULT_RFQ_FILE})",
    )
    parser.add_argument("--sheet", default=pfe.DEFAULT_SHEET_NAME, help="Worksheet name (defaults to first/active sheet)")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N products (great for small tests, e.g. --limit 10)",
    )
    parser.add_argument(
        "--positions",
        default=None,
        help="Process specific position numbers/ranges, e.g. '1-10' or '1,5,8,11,26'",
    )
    parser.add_argument(
        "--granularity",
        choices=["fine", "balanced", "broad"],
        default=dfg.DEFAULT_GRANULARITY,
        help=f"Dynamic family consolidation level (default: {dfg.DEFAULT_GRANULARITY})",
    )
    parser.add_argument("--stage2-sim", type=float, default=None, help="Optional custom Stage-2 merge similarity threshold")
    parser.add_argument("--query-suffix", default="wholesale OEM", help="Suffix appended to family search queries")

    # Execution & credit control
    parser.add_argument("--dry-run", action="store_true", help="Group into families and show credit cost without scraping")
    parser.add_argument("--from-cache", action="store_true", help="Skip ScrapingBee calls and re-score using cached family results")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore cached family scrapes and re-fetch via ScrapingBee")
    parser.add_argument("-p", "--pages", type=int, default=sa.DEFAULT_PAGES, help="Pages to scrape per family")
    parser.add_argument("--max-products", type=int, default=sa.MAX_PRODUCTS_PER_PAGE, help="Max listings per scraped page")
    parser.add_argument("--stealth", action=argparse.BooleanOptionalAction, default=sa.SCRAPINGBEE_STEALTH_PROXY)

    # Client scoring & filter rules
    parser.add_argument("--required-certs", default=rv.DEFAULT_REQUIRED_CERTS, help="Comma-separated global certs (e.g. 'CE,RoHS')")
    parser.add_argument("--min-years", type=int, default=rv.DEFAULT_MIN_YEARS, help="Minimum supplier years active")
    parser.add_argument("--require-trade-assurance", action=argparse.BooleanOptionalAction, default=rv.DEFAULT_REQUIRE_TA)
    parser.add_argument("--strict-moq", action=argparse.BooleanOptionalAction, default=rv.DEFAULT_STRICT_MOQ)
    parser.add_argument("--strict-certs", action=argparse.BooleanOptionalAction, default=rv.DEFAULT_STRICT_CERTS)
    parser.add_argument("--strict-min-price", action=argparse.BooleanOptionalAction, default=pfe.DEFAULT_STRICT_MIN_PRICE)
    parser.add_argument("--top", type=int, default=rv.DEFAULT_TOP_N, help="Top N vendors to keep per product")

    # Output paths
    parser.add_argument("--out-dir", default="output", help="Directory for cache, grouped plan, and final reports")
    parser.add_argument("--cache-file", default=None, help="Custom family scrape cache path (defaults to <out-dir>/family_scrape_cache.json)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cache_path = Path(args.cache_file) if args.cache_file else out_dir / "family_scrape_cache.json"
    grouped_xlsx_path = out_dir / "products_grouped.xlsx"
    family_plan_path = out_dir / "family_search_plan.json"
    final_xlsx_path = out_dir / "final_recommendations.xlsx"
    final_json_path = out_dir / "final_recommendations.json"

    # ------------------------------------------------------------------
    # STEP 1: Load Excel via both process_rfq_excel and dynamic_family_grouper
    # ------------------------------------------------------------------
    rfq_items = pfe.load_rfq_excel(args.excel_file, sheet_name=args.sheet)
    df = dfg.load_catalog(args.excel_file, sheet_name=args.sheet)

    if args.positions:
        selected_pos = parse_positions_arg(args.positions)
        rfq_items = [it for it in rfq_items if it["position"] in selected_pos]
        df = df[df["position"].astype(int).isin(selected_pos)].copy()

    if args.limit is not None:
        rfq_items = rfq_items[: args.limit]
        keep_pos = {it["position"] for it in rfq_items}
        df = df[df["position"].astype(int).isin(keep_pos)].head(args.limit).copy()

    df = df.reset_index(drop=True)
    if df.empty or not rfq_items:
        sys.exit("Error: No product rows remain after applying filters.")

    # ------------------------------------------------------------------
    # STEP 2: Dynamically segregate into supplier families (0 AI tokens)
    # ------------------------------------------------------------------
    vdf = dfg.extract_views(df)
    S_strict, S_broad = dfg.compute_similarity_matrices(df, vdf)
    labels = dfg.cluster_products(
        S_strict,
        S_broad,
        granularity=args.granularity,
        stage2_min_sim=args.stage2_sim,
    )
    tagged_df, family_plan = dfg.build_plan(
        df, vdf, labels, S_strict, query_suffix=args.query_suffix
    )

    dfg.export_excel(tagged_df, family_plan, str(grouped_xlsx_path))
    with open(family_plan_path, "w", encoding="utf-8") as f:
        json.dump(family_plan, f, indent=2, ensure_ascii=False)

    n_prod = len(tagged_df)
    n_fam = len(family_plan)
    reduction_pct = round((1 - n_fam / n_prod) * 100, 1)
    cr_per_page = 80 if args.stealth else 30
    est_credits = n_fam * args.pages * cr_per_page
    unoptimized_credits = n_prod * args.pages * cr_per_page

    print(
        f"\n[Step 1-2] Segregated {n_prod} products -> {n_fam} families "
        f"({reduction_pct}% fewer API calls)",
        file=sys.stderr,
    )
    print(
        f"           Estimated ScrapingBee cost ({'stealth' if args.stealth else 'premium'}, "
        f"{args.pages} pg/family): ~{est_credits:,} credits (vs ~{unoptimized_credits:,} ungrouped)",
        file=sys.stderr,
    )
    for fam in family_plan:
        pos_str = ",".join(str(p) for p in fam["positions"])
        print(
            f"           - {fam['family_id']} ({fam['item_count']} items | pos: {pos_str}) -> "
            f"\"{fam['search_query']}\"",
            file=sys.stderr,
        )

    if args.dry_run:
        print(f"\nDry run complete. Review families in {grouped_xlsx_path} before scraping.")
        return

    # ------------------------------------------------------------------
    # STEP 3: Scrape once per family with checkpoint caching (scrape_alibaba)
    # ------------------------------------------------------------------
    if not args.from_cache and not sa.SCRAPINGBEE_API_KEY:
        sys.exit("Error: SCRAPINGBEE_API_KEY is missing in .env (or pass --from-cache).")

    cache = pfe.load_cache(str(cache_path))
    scraped_by_family: dict[str, list[dict[str, Any]]] = {}

    print("\n[Step 3] Fetching Alibaba listings per family...", file=sys.stderr)
    for fam in family_plan:
        fid = fam["family_id"]
        query = fam["search_query"]
        # Key cache by normalized query so identical queries reuse data even if family IDs shift
        cache_key = query.strip().lower()
        cached_entry = cache.get(cache_key)

        use_cached = (
            not args.force_refresh
            and isinstance(cached_entry, dict)
            and isinstance(cached_entry.get("products"), list)
        )

        if use_cached or args.from_cache:
            products = (cached_entry or {}).get("products", [])
            print(f"  [{fid}] Loaded {len(products)} cached listings for \"{query}\"", file=sys.stderr)
            scraped_by_family[fid] = products
            continue

        fam_products: list[dict[str, Any]] = []
        for page in range(1, args.pages + 1):
            try:
                page_items = sa.scrape_term(
                    term=query,
                    api_key=sa.SCRAPINGBEE_API_KEY,
                    page=page,
                    stealth=args.stealth,
                    max_products=args.max_products,
                    debug_dir=sa.DEBUG_DIR,
                )
                fam_products.extend(page_items)
            except requests.HTTPError as exc:
                print(
                    f"  [{fid}] HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"  [{fid}] Failed (page {page}): {exc}", file=sys.stderr)
            time.sleep(sa.REQUEST_DELAY_SECONDS)

        scraped_by_family[fid] = fam_products
        cache[cache_key] = {
            "family_id": fid,
            "family_name": fam["family_name"],
            "search_query": query,
            "products": fam_products,
        }
        # Save immediately after each family so credits are never wasted on interruption
        pfe.save_cache(str(cache_path), cache)

    # ------------------------------------------------------------------
    # STEP 4: Map family listings back to every product & score vendors
    # ------------------------------------------------------------------
    print("\n[Step 4] Mapping family vendors back to individual products & scoring...", file=sys.stderr)
    pos_to_family: dict[int, dict[str, Any]] = {}
    for fam in family_plan:
        for pos in fam["positions"]:
            pos_to_family[int(pos)] = fam

    global_certs = [c.strip() for c in args.required_certs.split(",") if c.strip()]
    positions_report: list[dict[str, Any]] = []

    for item in rfq_items:
        fam = pos_to_family.get(int(item["position"]))
        fid = fam["family_id"] if fam else "UNASSIGNED"
        listings = scraped_by_family.get(fid, [])

        evaluated = pfe.evaluate_rfq_position(
            item=item,
            listings=listings,
            global_required_certs=global_certs,
            min_years=args.min_years,
            require_ta=args.require_trade_assurance,
            strict_moq=args.strict_moq,
            strict_certs=args.strict_certs,
            strict_min_price=args.strict_min_price,
        )
        evaluated["family_id"] = fid
        evaluated["family_name"] = fam["family_name"] if fam else "Unassigned"
        evaluated["family_search_query"] = fam["search_query"] if fam else item["search_query"]
        positions_report.append(evaluated)

    cross_item_vendors = build_cross_item_vendor_summary(positions_report, top_n=args.top)

    report = {
        "meta": {
            "source_excel": args.excel_file,
            "granularity": args.granularity,
            "total_products": n_prod,
            "total_families": n_fam,
            "api_call_reduction_pct": reduction_pct,
            "pages_per_family": args.pages,
            "proxy_mode": "stealth" if args.stealth else "premium",
        },
        "positions": positions_report,
        "cross_item_vendors": cross_item_vendors,
    }

    # ------------------------------------------------------------------
    # STEP 5: Print summary & export final Excel + JSON deliverables
    # ------------------------------------------------------------------
    print_terminal_summary(report, top_n=args.top)

    scraped_counts = {fid: len(prods) for fid, prods in scraped_by_family.items()}
    export_final_excel(report, family_plan, scraped_counts, final_xlsx_path, top_n=args.top)

    with open(final_json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\nOutputs saved in '{out_dir}/':", file=sys.stderr)
    print(f"  - Final Excel report : {final_xlsx_path}", file=sys.stderr)
    print(f"  - Final JSON report  : {final_json_path}", file=sys.stderr)
    print(f"  - Family scrape cache: {cache_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
