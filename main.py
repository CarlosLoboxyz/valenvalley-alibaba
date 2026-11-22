import argparse
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Resolve project root and add ./scripts to sys.path before importing the pipeline modules
ROOT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if SCRIPTS_DIR.is_dir() and str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
elif str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Load .env from project root before module constants initialize
load_dotenv(ROOT_DIR / ".env")

import dynamic_family_grouper as dfg  # noqa: E402
import pipeline  # noqa: E402
import process_rfq_excel as pfe  # noqa: E402
import recommend_vendors as rv  # noqa: E402
import scrape_alibaba as sa  # noqa: E402
from pipeline_errors import InputError  # noqa: E402

# Re-exported for backward compatibility (these used to live in this file)
parse_positions_arg = pipeline.parse_positions_arg
build_cross_item_vendor_summary = pipeline.build_cross_item_vendor_summary
export_final_excel = pipeline.export_final_excel


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

    try:
        run_cli(args)
    except InputError as exc:
        sys.exit(f"Error: {exc}")


def run_cli(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cache_path = Path(args.cache_file) if args.cache_file else out_dir / "family_scrape_cache.json"
    grouped_xlsx_path = out_dir / "products_grouped.xlsx"
    family_plan_path = out_dir / "family_search_plan.json"
    final_xlsx_path = out_dir / "final_recommendations.xlsx"
    final_json_path = out_dir / "final_recommendations.json"

    # STEP 1-2: load, filter, and cluster locally (0 AI tokens, 0 credits)
    rfq_items, df = pipeline.load_and_filter(
        args.excel_file, sheet_name=args.sheet, positions=args.positions, limit=args.limit
    )
    tagged_df, family_plan = pipeline.build_family_plan(
        df, granularity=args.granularity, stage2_sim=args.stage2_sim, query_suffix=args.query_suffix
    )

    dfg.export_excel(tagged_df, family_plan, str(grouped_xlsx_path))
    pipeline.write_json_atomic(family_plan_path, family_plan)

    n_prod = len(tagged_df)
    n_fam = len(family_plan)
    reduction_pct = pipeline.reduction_pct(n_prod, n_fam)
    cr_per_page = pipeline.credits_per_page(args.stealth)
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

    # STEP 3: scrape once per family with checkpoint caching
    if not args.from_cache and not sa.SCRAPINGBEE_API_KEY:
        sys.exit("Error: SCRAPINGBEE_API_KEY is missing in .env (or pass --from-cache).")

    store = pipeline.FileCacheStore(cache_path)

    def log_event(event: dict[str, Any]) -> None:
        if event["type"] == "family_completed":
            print(
                f"  [{event['family_id']}] {event['status']}: {event['listing_count']} listings "
                f"for \"{event['search_query']}\"",
                file=sys.stderr,
            )
        elif event["type"] == "warning":
            print(f"  [{event.get('family_id', '-')}] {event['message']}", file=sys.stderr)

    print("\n[Step 3] Fetching Alibaba listings per family...", file=sys.stderr)
    run = pipeline.scrape_families(
        family_plan,
        store,
        pages=args.pages,
        stealth=args.stealth,
        max_products=args.max_products,
        api_key=sa.SCRAPINGBEE_API_KEY,
        from_cache=args.from_cache,
        force_refresh=args.force_refresh,
        debug_dir=sa.DEBUG_DIR,
        delay_seconds=sa.REQUEST_DELAY_SECONDS,
        on_event=log_event,
        quiet=False,
    )

    # STEP 4: map family listings back to every product & score vendors
    print("\n[Step 4] Mapping family vendors back to individual products & scoring...", file=sys.stderr)
    options = pipeline.ScoreOptions(
        top_n=args.top,
        required_certs=[c.strip() for c in args.required_certs.split(",") if c.strip()],
        min_years=args.min_years,
        require_trade_assurance=args.require_trade_assurance,
        strict_moq=args.strict_moq,
        strict_certs=args.strict_certs,
        strict_min_price=args.strict_min_price,
    )
    positions_report = pipeline.score_positions(rfq_items, family_plan, run.scraped_by_family, options)
    report = pipeline.build_report(
        positions_report,
        source_excel=args.excel_file,
        granularity=args.granularity,
        total_products=n_prod,
        total_families=n_fam,
        pages=args.pages,
        stealth=args.stealth,
        options=options,
        credits_spent=run.credits_spent,
        family_plan=family_plan,
        scraped_counts=run.scraped_counts,
    )

    # STEP 5: print summary & export final Excel + JSON deliverables
    print_terminal_summary(report, top_n=args.top)
    export_final_excel(report, family_plan, run.scraped_counts, final_xlsx_path, top_n=args.top)
    pipeline.write_json_atomic(final_json_path, report)

    print(f"\nOutputs saved in '{out_dir}/':", file=sys.stderr)
    print(f"  - Final Excel report : {final_xlsx_path}", file=sys.stderr)
    print(f"  - Final JSON report  : {final_json_path}", file=sys.stderr)
    print(f"  - Family scrape cache: {cache_path}", file=sys.stderr)
    if run.credits_spent:
        print(f"  - Credits spent (Spb-cost): {run.credits_spent:,}", file=sys.stderr)


if __name__ == "__main__":
    main()
