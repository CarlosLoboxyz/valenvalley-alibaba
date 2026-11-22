import argparse
import csv
import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: Optional[int]) -> Optional[int]:
    val = os.getenv(name)
    return int(val) if val and val.strip() else default


def env_float(name: str, default: Optional[float]) -> Optional[float]:
    val = os.getenv(name)
    return float(val) if val and val.strip() else default


# Load defaults from .env
DEFAULT_INPUT_FILE = os.getenv("RECOMMEND_INPUT_FILE", "results.json")
DEFAULT_TOP_N = env_int("RECOMMEND_TOP_N", 5) or 5
DEFAULT_TARGET_QTY = env_int("CLIENT_TARGET_QTY", None)
DEFAULT_MAX_PRICE = env_float("CLIENT_MAX_PRICE", None)
DEFAULT_REQUIRED_CERTS = os.getenv("CLIENT_REQUIRED_CERTS", "").strip()
DEFAULT_MIN_YEARS = env_int("CLIENT_MIN_YEARS", 0) or 0
DEFAULT_REQUIRE_TA = env_bool("CLIENT_REQUIRE_TRADE_ASSURANCE", False)
DEFAULT_STRICT_MOQ = env_bool("CLIENT_STRICT_MOQ", False)
DEFAULT_STRICT_CERTS = env_bool("CLIENT_STRICT_CERTS", False)

RAW_WEIGHTS = {
    "price": env_float("WEIGHT_PRICE", 30.0) or 0.0,
    "moq": env_float("WEIGHT_MOQ", 20.0) or 0.0,
    "trade_assurance": env_float("WEIGHT_TRADE_ASSURANCE", 20.0) or 0.0,
    "years_active": env_float("WEIGHT_YEARS", 15.0) or 0.0,
    "certifications": env_float("WEIGHT_CERTS", 15.0) or 0.0,
}


def normalized_weights(raw: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, v) for v in raw.values())
    if total <= 0:
        return {k: 0.2 for k in raw}
    return {k: max(0.0, v) / total for k, v in raw.items()}


def parse_price(raw: object) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Parse strings like '$4.10-4.20' or 'US $1,200 / piece' into (min, max, mid)."""
    if raw is None:
        return None, None, None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return val, val, val

    # Drop unit suffixes like '/ 100 pieces' before extracting currency amounts
    text = str(raw).split("/", 1)[0]
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
    nums = [n for n in nums if n > 0]
    if not nums:
        return None, None, None

    p_min = nums[0]
    p_max = nums[1] if len(nums) > 1 and nums[1] >= nums[0] else nums[0]
    p_mid = round((p_min + p_max) / 2.0, 4)
    return p_min, p_max, p_mid


def parse_moq_value(raw: object) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(raw))
    return float(match.group(0).replace(",", "")) if match else None


def normalize_cert_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        items = [str(c).strip() for c in raw if str(c).strip()]
    elif isinstance(raw, str):
        items = [c.strip() for c in re.split(r"[,;/|]+", raw) if c.strip()]
    else:
        return []
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def vendor_key(item: dict, fallback_idx: int) -> str:
    supplier_id = item.get("supplier_id")
    if isinstance(supplier_id, str) and supplier_id.strip():
        return f"id:{supplier_id.strip().lower()}"
    supplier = item.get("supplier")
    if isinstance(supplier, str) and supplier.strip():
        cleaned = re.sub(r"[^a-z0-9]+", "", supplier.lower())
        if cleaned:
            return f"name:{cleaned}"
    return f"unknown:{fallback_idx}"


def pick_best_offer(offers: list[dict], target_qty: Optional[int]) -> dict:
    """Select a vendor's most relevant product listing for the client's quantity."""
    def sort_key(offer: dict):
        moq = offer["moq"]
        price_mid = offer["price_mid"]
        # Prefer offers that satisfy target_qty, then lowest valid price
        if target_qty is not None:
            if moq is not None and moq <= target_qty:
                moq_tier = 0
            elif moq is not None and moq <= target_qty * 1.5:
                moq_tier = 1
            elif moq is None:
                moq_tier = 2
            else:
                moq_tier = 3
        else:
            moq_tier = 0 if moq is not None else 1

        price_rank = price_mid if price_mid is not None else float("inf")
        moq_rank = moq if moq is not None else float("inf")
        return (moq_tier, price_rank, moq_rank)

    return min(offers, key=sort_key)


def aggregate_vendors(listings: list[dict], target_qty: Optional[int]) -> list[dict]:
    """Group listings by supplier and merge vendor-level signals."""
    grouped: dict[str, list[dict]] = defaultdict(list)

    for idx, item in enumerate(listings):
        if not isinstance(item, dict):
            continue
        p_min, p_max, p_mid = parse_price(item.get("price"))
        moq_val = parse_moq_value(item.get("moq"))
        years = item.get("years_active")
        years_int = int(years) if isinstance(years, (int, float)) else None

        enriched = {
            "title": item.get("title") or "Untitled product",
            "price_raw": item.get("price"),
            "price_min": p_min,
            "price_max": p_max,
            "price_mid": p_mid,
            "moq": moq_val,
            "moq_unit": item.get("moq_unit") or "units",
            "supplier": item.get("supplier") or "Unknown Supplier",
            "supplier_id": item.get("supplier_id"),
            "years_active": years_int,
            "certifications": normalize_cert_list(item.get("certifications")),
            "has_trade_assurance": bool(item.get("has_trade_assurance")),
            "url": item.get("url"),
        }
        grouped[vendor_key(item, idx)].append(enriched)

    vendors = []
    for key, offers in grouped.items():
        best = pick_best_offer(offers, target_qty)
        all_years = [o["years_active"] for o in offers if o["years_active"] is not None]
        merged_certs = normalize_cert_list(
            [c for o in offers for c in o["certifications"]]
        )
        supplier_id = next((o["supplier_id"] for o in offers if o["supplier_id"]), None)

        vendors.append(
            {
                "vendor_key": key,
                "supplier": best["supplier"],
                "supplier_id": supplier_id,
                "years_active": max(all_years) if all_years else None,
                "has_trade_assurance": any(o["has_trade_assurance"] for o in offers),
                "certifications": merged_certs,
                "best_offer": {
                    "title": best["title"],
                    "price_raw": best["price_raw"],
                    "price_min": best["price_min"],
                    "price_max": best["price_max"],
                    "price_mid": best["price_mid"],
                    "moq": best["moq"],
                    "moq_unit": best["moq_unit"],
                    "url": best["url"],
                },
                "listing_count": len(offers),
            }
        )

    return vendors


def check_hard_filters(
    vendor: dict,
    target_qty: Optional[int],
    strict_moq: bool,
    max_price: Optional[float],
    required_certs: list[str],
    strict_certs: bool,
    min_years: int,
    require_ta: bool,
) -> list[str]:
    """Return a list of disqualification reasons (empty means vendor qualifies)."""
    reasons = []
    offer = vendor["best_offer"]

    if require_ta and not vendor["has_trade_assurance"]:
        reasons.append("Missing required Trade Assurance")

    if min_years > 0:
        yrs = vendor["years_active"]
        if yrs is None or yrs < min_years:
            reasons.append(f"Years active ({yrs}) below minimum ({min_years})")

    if max_price is not None and offer["price_mid"] is not None:
        if offer["price_min"] > max_price:
            reasons.append(f"Price (${offer['price_min']:.2f}) exceeds max budget (${max_price:.2f})")

    if strict_moq and target_qty is not None:
        moq = offer["moq"]
        if moq is None or moq > target_qty:
            reasons.append(f"MOQ ({moq}) exceeds strict target quantity ({target_qty})")

    if strict_certs and required_certs:
        v_certs = [c.lower() for c in vendor["certifications"]]
        missing = [
            req for req in required_certs if not any(req.lower() in vc for vc in v_certs)
        ]
        if missing:
            reasons.append(f"Missing required certifications: {', '.join(missing)}")

    return reasons


TIER_STRONG_MIN = 75
TIER_CONSIDER_MIN = 55


def tier_for_score(score: float) -> str:
    """Single source of truth for recommendation tiers."""
    if score >= TIER_STRONG_MIN:
        return "Strong Recommend"
    if score >= TIER_CONSIDER_MIN:
        return "Consider"
    return "Caution"


def score_vendors(
    vendors: list[dict],
    weights: dict[str, float],
    target_qty: Optional[int],
    required_certs: list[str],
) -> list[dict]:
    """Compute transparent 0-100 sub-scores and total weighted score."""
    valid_prices = [
        v["best_offer"]["price_mid"]
        for v in vendors
        if v["best_offer"]["price_mid"] is not None
    ]
    median_price = statistics.median(valid_prices) if valid_prices else None

    # Exclude obvious bait prices (<25% of median) from setting the price-scale floor
    if median_price and len(valid_prices) >= 3:
        normal_prices = [p for p in valid_prices if p >= median_price * 0.25] or valid_prices
    else:
        normal_prices = valid_prices

    p_min = min(normal_prices) if normal_prices else None
    p_max = max(normal_prices) if normal_prices else None

    # For relative MOQ scoring when no target_qty is supplied
    log_moqs = [
        math.log1p(v["best_offer"]["moq"])
        for v in vendors
        if v["best_offer"]["moq"] is not None
    ]
    lm_min = min(log_moqs) if log_moqs else None
    lm_max = max(log_moqs) if log_moqs else None

    ranked = []
    for v in vendors:
        offer = v["best_offer"]
        pros = []
        warnings = []

        # 1. Price score (0-100)
        price_mid = offer["price_mid"]
        if price_mid is None:
            price_score = 35.0
            warnings.append("Price not listed — request quote")
        elif median_price and len(valid_prices) >= 3 and price_mid < median_price * 0.25:
            price_score = 50.0
            warnings.append(
                f"Suspiciously low price (${price_mid:.2f} vs ${median_price:.2f} median) — verify real unit cost"
            )
        elif p_min is not None and p_max is not None and p_max > p_min:
            price_score = max(0.0, min(100.0, 100.0 * (p_max - price_mid) / (p_max - p_min)))
            if price_score >= 80:
                pros.append(f"Competitive pricing ({offer['price_raw']})")
        else:
            price_score = 100.0

        # 2. MOQ score (0-100)
        moq = offer["moq"]
        if target_qty is not None:
            if moq is None:
                moq_score = 40.0
                warnings.append("MOQ not listed — confirm before ordering")
            elif moq <= target_qty:
                moq_score = 100.0
                pros.append(f"Fits target order ({int(moq)} {offer['moq_unit']} <= {target_qty})")
            elif moq <= target_qty * 1.5:
                ratio = (moq - target_qty) / (target_qty * 0.5)
                moq_score = round(70.0 - 40.0 * ratio, 1)
                warnings.append(
                    f"MOQ ({int(moq)}) slightly above target ({target_qty}) — likely negotiable"
                )
            else:
                moq_score = 0.0
                warnings.append(f"MOQ ({int(moq)}) is far above target ({target_qty})")
        else:
            if moq is None:
                moq_score = 40.0
                warnings.append("MOQ not listed")
            elif lm_min is not None and lm_max is not None and lm_max > lm_min:
                moq_score = 100.0 * (lm_max - math.log1p(moq)) / (lm_max - lm_min)
                if moq_score >= 80:
                    pros.append(f"Low MOQ ({int(moq)} {offer['moq_unit']})")
            else:
                moq_score = 100.0

        # 3. Years active score (0-100; 8+ years = full score)
        years = v["years_active"]
        if years is None:
            years_score = 25.0
            warnings.append("Supplier tenure unknown")
        else:
            years_score = min(100.0, round((years / 8.0) * 100.0, 1))
            if years >= 5:
                pros.append(f"Established supplier ({years} yrs active)")
            elif years < 2:
                warnings.append(f"New supplier ({years} yr active)")

        # 4. Trade Assurance score (0-100)
        if v["has_trade_assurance"]:
            ta_score = 100.0
            pros.append("Trade Assurance protected")
        else:
            ta_score = 0.0
            warnings.append("No Trade Assurance badge detected")

        # 5. Certifications score (0-100)
        v_certs = v["certifications"]
        v_certs_lower = [c.lower() for c in v_certs]
        if required_certs:
            matched = [
                req for req in required_certs if any(req.lower() in vc for vc in v_certs_lower)
            ]
            missing = [req for req in required_certs if req not in matched]
            base = 80.0 * (len(matched) / len(required_certs))
            extras = max(0, len(v_certs) - len(matched))
            cert_score = min(100.0, base + min(20.0, extras * 10.0))
            if matched:
                pros.append(f"Matches certs: {', '.join(matched)}")
            if missing:
                warnings.append(f"Missing certs on card: {', '.join(missing)} (confirm via RFQ)")
        else:
            cert_score = min(100.0, len(v_certs) * 25.0)
            if v_certs:
                pros.append(f"Certifications/badges: {', '.join(v_certs)}")
            else:
                warnings.append("No certifications shown on search card — verify directly")

        breakdown = {
            "price": round(price_score, 1),
            "moq": round(moq_score, 1),
            "years_active": round(years_score, 1),
            "trade_assurance": round(ta_score, 1),
            "certifications": round(cert_score, 1),
        }

        total_score = round(
            sum(breakdown[k] * weights[k] for k in breakdown), 1
        )

        tier = tier_for_score(total_score)

        ranked.append(
            {
                **v,
                "total_score": total_score,
                "recommendation": tier,
                "score_breakdown": breakdown,
                "pros": pros,
                "warnings": warnings,
            }
        )

    ranked.sort(
        key=lambda x: (
            x["total_score"],
            x["has_trade_assurance"],
            x["years_active"] or 0,
        ),
        reverse=True,
    )
    return ranked


def print_terminal_report(report: dict, top_n: int, show_disqualified: bool) -> None:
    req = report["client_requirements"]
    print("\n=== ALIBABA VENDOR RECOMMENDATIONS ===")
    print(
        f"Requirements | Target Qty: {req['target_qty'] or 'Flexible'} | "
        f"Max Price: {('$' + str(req['max_price'])) if req['max_price'] else 'Any'} | "
        f"Required Certs: {', '.join(req['required_certs']) or 'None'} | "
        f"Trade Assurance Required: {req['require_trade_assurance']}"
    )

    for term, data in report["results"].items():
        qualified = data["recommended"]
        disqualified = data["disqualified"]
        print(f"\n--- Search Term: '{term}' ({len(qualified)} qualified, {len(disqualified)} disqualified) ---")

        if not qualified:
            print("  No vendors met the mandatory criteria.")
        for rank, v in enumerate(qualified[:top_n], start=1):
            offer = v["best_offer"]
            b = v["score_breakdown"]
            ta_label = "Yes" if v["has_trade_assurance"] else "No"
            yrs_label = f"{v['years_active']}y" if v["years_active"] is not None else "?"
            moq_label = (
                f"{int(offer['moq'])} {offer['moq_unit']}"
                if offer["moq"] is not None
                else "N/A"
            )
            certs_label = ", ".join(v["certifications"]) if v["certifications"] else "None listed"

            print(
                f"  #{rank} [{v['total_score']}/100 - {v['recommendation']}] "
                f"{v['supplier']} (ID: {v['supplier_id'] or 'N/A'})"
            )
            print(
                f"      Price: {offer['price_raw'] or 'N/A'} | MOQ: {moq_label} | "
                f"Active: {yrs_label} | Trade Assurance: {ta_label} | Certs: {certs_label}"
            )
            print(
                f"      Scores -> Price:{b['price']}  MOQ:{b['moq']}  "
                f"TA:{b['trade_assurance']}  Years:{b['years_active']}  Certs:{b['certifications']}"
            )
            if v["pros"]:
                print(f"      + {'; '.join(v['pros'])}")
            if v["warnings"]:
                print(f"      ! {'; '.join(v['warnings'])}")
            if offer["url"]:
                print(f"      URL: {offer['url']}")

        if show_disqualified and disqualified:
            print("  Disqualified vendors:")
            for d in disqualified:
                print(f"    - {d['supplier']}: {'; '.join(d['disqualification_reasons'])}")


def export_report(report: dict, output_path: str, top_n: int) -> None:
    if output_path.lower().endswith(".csv"):
        fieldnames = [
            "search_term",
            "rank",
            "recommendation",
            "total_score",
            "supplier",
            "supplier_id",
            "price_raw",
            "price_mid",
            "moq",
            "moq_unit",
            "years_active",
            "has_trade_assurance",
            "certifications",
            "score_price",
            "score_moq",
            "score_trade_assurance",
            "score_years_active",
            "score_certifications",
            "pros",
            "warnings",
            "product_title",
            "url",
        ]
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for term, data in report["results"].items():
                for rank, v in enumerate(data["recommended"][:top_n], start=1):
                    offer = v["best_offer"]
                    b = v["score_breakdown"]
                    writer.writerow(
                        {
                            "search_term": term,
                            "rank": rank,
                            "recommendation": v["recommendation"],
                            "total_score": v["total_score"],
                            "supplier": v["supplier"],
                            "supplier_id": v["supplier_id"] or "",
                            "price_raw": offer["price_raw"] or "",
                            "price_mid": offer["price_mid"] if offer["price_mid"] is not None else "",
                            "moq": int(offer["moq"]) if offer["moq"] is not None else "",
                            "moq_unit": offer["moq_unit"],
                            "years_active": v["years_active"] if v["years_active"] is not None else "",
                            "has_trade_assurance": v["has_trade_assurance"],
                            "certifications": ", ".join(v["certifications"]),
                            "score_price": b["price"],
                            "score_moq": b["moq"],
                            "score_trade_assurance": b["trade_assurance"],
                            "score_years_active": b["years_active"],
                            "score_certifications": b["certifications"],
                            "pros": " | ".join(v["pros"]),
                            "warnings": " | ".join(v["warnings"]),
                            "product_title": offer["title"],
                            "url": offer["url"] or "",
                        }
                    )
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\nExported recommendations to {output_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Parse scraped Alibaba listings, score vendors, and recommend top suppliers."
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        default=DEFAULT_INPUT_FILE,
        help=f"Scraped JSON file from scrape_alibaba.py (default: {DEFAULT_INPUT_FILE})",
    )
    parser.add_argument(
        "--target-qty",
        type=int,
        default=DEFAULT_TARGET_QTY,
        help="Client's desired order quantity for MOQ scoring",
    )
    parser.add_argument(
        "--strict-moq",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_STRICT_MOQ,
        help="Disqualify vendors whose MOQ exceeds --target-qty",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=DEFAULT_MAX_PRICE,
        help="Maximum unit price (USD) budget ceiling",
    )
    parser.add_argument(
        "--required-certs",
        default=DEFAULT_REQUIRED_CERTS,
        help="Comma-separated certifications the client wants (e.g. 'CE,RoHS,ISO')",
    )
    parser.add_argument(
        "--strict-certs",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_STRICT_CERTS,
        help="Disqualify vendors missing any --required-certs",
    )
    parser.add_argument(
        "--min-years",
        type=int,
        default=DEFAULT_MIN_YEARS,
        help="Minimum years active on Alibaba",
    )
    parser.add_argument(
        "--require-trade-assurance",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_REQUIRE_TA,
        help="Disqualify vendors without Trade Assurance",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"Number of top vendors to display/export per search term (default: {DEFAULT_TOP_N})",
    )
    parser.add_argument(
        "--show-disqualified",
        action="store_true",
        help="Print disqualified vendors and why they were filtered out",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Optional output path (.json or .csv)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.input_file):
        sys.exit(f"Error: Input file not found: {args.input_file}")

    with open(args.input_file, "r", encoding="utf-8") as f:
        scraped_data = json.load(f)

    if not isinstance(scraped_data, dict):
        sys.exit("Error: Expected a JSON object mapping search terms to product lists.")

    required_certs = [c.strip() for c in args.required_certs.split(",") if c.strip()]
    weights = normalized_weights(RAW_WEIGHTS)

    results = {}
    for term, listings in scraped_data.items():
        if not isinstance(listings, list):
            continue
        vendors = aggregate_vendors(listings, target_qty=args.target_qty)
        qualified = []
        disqualified = []

        for v in vendors:
            reasons = check_hard_filters(
                vendor=v,
                target_qty=args.target_qty,
                strict_moq=args.strict_moq,
                max_price=args.max_price,
                required_certs=required_certs,
                strict_certs=args.strict_certs,
                min_years=args.min_years,
                require_ta=args.require_trade_assurance,
            )
            if reasons:
                disqualified.append({**v, "disqualification_reasons": reasons})
            else:
                qualified.append(v)

        ranked = score_vendors(
            vendors=qualified,
            weights=weights,
            target_qty=args.target_qty,
            required_certs=required_certs,
        )
        results[term] = {
            "recommended": ranked,
            "disqualified": disqualified,
        }

    report = {
        "client_requirements": {
            "target_qty": args.target_qty,
            "strict_moq": args.strict_moq,
            "max_price": args.max_price,
            "required_certs": required_certs,
            "strict_certs": args.strict_certs,
            "min_years": args.min_years,
            "require_trade_assurance": args.require_trade_assurance,
        },
        "weights_used": {k: round(v * 100, 1) for k, v in weights.items()},
        "results": results,
    }

    print_terminal_report(report, top_n=args.top, show_disqualified=args.show_disqualified)

    if args.output:
        export_report(report, args.output, top_n=args.top)


if __name__ == "__main__":
    main()
