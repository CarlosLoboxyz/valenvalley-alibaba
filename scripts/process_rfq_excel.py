import argparse
import json
import os
import re
import sys
import time
from typing import Any, Optional

import json_repair
import requests
from dotenv import load_dotenv
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill

# Reuse your existing scraper and vendor scorer modules
import recommend_vendors as rv
import scrape_alibaba as sa

load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val and val.strip() else default


DEFAULT_RFQ_FILE = os.getenv("RFQ_EXCEL_FILE", "rfq_items.xlsx")
DEFAULT_SHEET_NAME = os.getenv("RFQ_SHEET_NAME", "").strip() or None
DEFAULT_CACHE_FILE = os.getenv("RFQ_CACHE_FILE", "rfq_scraped_cache.json")
DEFAULT_OUTPUT_FILE = os.getenv("RFQ_OUTPUT_FILE", "rfq_recommendations.xlsx")
WEIGHT_VENDOR_SCORE = env_float("WEIGHT_VENDOR_SCORE", 75.0)
WEIGHT_RFQ_FIT = env_float("WEIGHT_RFQ_FIT", 25.0)
DEFAULT_STRICT_MIN_PRICE = env_bool("STRICT_MIN_PRICE", False)
DEFAULT_INCLUDE_BRAND_IN_SEARCH = env_bool("INCLUDE_BRAND_IN_SEARCH", False)

# Map normalized column header variations to canonical fields
HEADER_ALIASES = {
    "position": "position",
    "pos": "position",
    "item_no": "position",
    "item_number": "position",
    "line": "position",
    "name": "name",
    "item_name": "name",
    "product": "name",
    "product_name": "name",
    "description": "description",
    "desc": "description",
    "unit": "unit",
    "uom": "unit",
    "qty": "qty",
    "quantity": "qty",
    "min_price": "min_price",
    "minprice": "min_price",
    "min_unit_price": "min_price",
    "max_price": "max_price",
    "maxprice": "max_price",
    "max_unit_price": "max_price",
    "brand": "brand",
    "preferred_brand": "brand",
    "technical_specs": "technical_specs",
    "tech_specs": "technical_specs",
    "specs": "technical_specs",
    "specifications": "technical_specs",
}

CERT_KEYS = {"certifications", "certification", "certs", "standards", "compliance"}

UNIT_GROUPS = {
    "piece": {"pc", "pcs", "piece", "pieces", "unit", "units", "ea", "each"},
    "set": {"set", "sets", "kit", "kits", "pair", "pairs"},
    "box": {"box", "boxes", "ctn", "ctns", "carton", "cartons", "case", "cases", "pack", "packs"},
    "kg": {"kg", "kgs", "kilogram", "kilograms"},
    "g": {"g", "gram", "grams"},
    "meter": {"m", "meter", "meters", "metre", "metres"},
    "roll": {"roll", "rolls"},
    "bag": {"bag", "bags", "sack", "sacks"},
    "liter": {"l", "liter", "liters", "litre", "litres"},
}

STOPWORDS = {
    "with", "for", "and", "the", "from", "into", "that", "this", "type",
    "high", "quality", "standard", "similar", "equivalent", "approx",
    "pieces", "piece", "unit", "units", "size", "color", "material",
}


def normalize_header(raw: Any) -> str:
    if raw is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", str(raw).strip().lower()).strip("_")


def parse_number(raw: Any) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = str(raw).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    return float(match.group(0)) if match else None


def normalize_unit(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    token = re.sub(r"[^a-z]+", "", unit.strip().lower())
    for canonical, aliases in UNIT_GROUPS.items():
        if token in aliases:
            return canonical
    return token or None


def parse_brand_requirement(raw_brand: Any) -> tuple[Optional[str], bool]:
    """Split 'Anker or similar' into ('Anker', True) or 'Bosch' into ('Bosch', False)."""
    if raw_brand is None:
        return None, True
    text = str(raw_brand).strip()
    if not text or text.lower() in {"any", "none", "n/a", "na", "-", "generic", "oem"}:
        return None, True

    similar_pattern = re.compile(
        r"\b(or\s+similar|or\s+equivalent|or\s+equal|similar|equivalent|equiv\.?|alt\.?|alternative)\b",
        re.IGNORECASE,
    )
    allow_similar = bool(similar_pattern.search(text))
    cleaned = similar_pattern.sub("", text)
    cleaned = re.sub(r"[()/\-,]+", " ", cleaned).strip()
    return (cleaned or None), allow_similar


def parse_technical_specs(raw_specs: Any) -> tuple[dict[str, Any], list[str], str]:
    """
    Parse technical specs cell (JSON object, JSON list, or plain text).
    Returns: (specs_dict, extracted_certifications, formatted_specs_text)
    """
    if raw_specs is None:
        return {}, [], ""
    text = str(raw_specs).strip()
    if not text:
        return {}, [], ""

    parsed: Any = None
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            try:
                parsed = json_repair.loads(text)
            except Exception:
                parsed = None

    specs_dict: dict[str, Any] = {}
    certs: list[str] = []

    if isinstance(parsed, dict):
        parts = []
        for k, v in parsed.items():
            key_str = str(k).strip()
            if v is None or v == "":
                continue
            if key_str.lower() in CERT_KEYS:
                certs.extend(rv.normalize_cert_list(v))
                val_str = ", ".join(rv.normalize_cert_list(v))
            elif isinstance(v, (list, tuple)):
                val_str = ", ".join(str(x).strip() for x in v if str(x).strip())
            elif isinstance(v, dict):
                val_str = ", ".join(f"{sk}: {sv}" for sk, sv in v.items())
            else:
                val_str = str(v).strip()

            specs_dict[key_str] = v
            if val_str:
                parts.append(f"{key_str}: {val_str}")
        return specs_dict, rv.normalize_cert_list(certs), "; ".join(parts)

    if isinstance(parsed, list):
        items = [str(x).strip() for x in parsed if str(x).strip()]
        return {"specs": items}, [], "; ".join(items)

    # Plain-text fallback
    return {"raw_specs": text}, [], text


def merge_description_and_specs(description: str, specs_text: str) -> str:
    desc = description.strip()
    specs = specs_text.strip()
    if desc and specs:
        return f"{desc} | Specs: {specs}"
    return desc or specs


def extract_compact_spec_tokens(specs_dict: dict[str, Any], max_tokens: int = 3) -> list[str]:
    """Pull short, high-signal spec values (e.g. '65W', '12V', 'IP67') for Alibaba search."""
    tokens = []
    for key, val in specs_dict.items():
        if key.lower() in CERT_KEYS or key == "raw_specs":
            continue
        if isinstance(val, bool) or val is None:
            continue
        val_str = str(val).strip()
        # Keep short values (1-3 words, <= 20 chars) so search queries stay focused
        if 1 <= len(val_str) <= 20 and len(val_str.split()) <= 3:
            if val_str.lower() not in {"yes", "no", "true", "false", "n/a", "none"}:
                tokens.append(val_str)
        if len(tokens) >= max_tokens:
            break
    return tokens


def build_search_query(
    name: str,
    brand_name: Optional[str],
    allow_similar: bool,
    specs_dict: dict[str, Any],
    include_brand_in_search: bool = False,
) -> str:
    """Build a concise Alibaba search query without overloading SearchText."""
    parts = []
    if brand_name and (not allow_similar or include_brand_in_search):
        parts.append(brand_name)

    parts.append(name.strip())

    # Add up to 2-3 compact technical spec values not already in the product name
    name_lower = name.lower()
    for tok in extract_compact_spec_tokens(specs_dict, max_tokens=3):
        if tok.lower() not in name_lower:
            parts.append(tok)

    query = " ".join(parts)
    query = re.sub(r"\s+", " ", query).strip()
    # Keep query under ~85 chars so Alibaba returns results instead of 0 matches
    if len(query) > 85:
        query = query[:85].rsplit(" ", 1)[0]
    return query


def extract_relevance_keywords(item: dict[str, Any]) -> list[str]:
    """Build keyword set from technical specs + description to score listing relevance."""
    keywords: list[str] = []

    for key, val in item["technical_specs"].items():
        if key.lower() in CERT_KEYS:
            continue
        for token in re.findall(r"[a-zA-Z0-9]+(?:[-./][a-zA-Z0-9]+)?", str(val)):
            low = token.lower()
            if len(low) >= 2 and low not in STOPWORDS:
                keywords.append(low)

    for token in re.findall(r"[a-zA-Z0-9]+(?:[-./][a-zA-Z0-9]+)?", item["description"]):
        low = token.lower()
        if len(low) >= 3 and low not in STOPWORDS:
            keywords.append(low)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)
    return unique[:15]


def load_rfq_excel(
    excel_path: str,
    sheet_name: Optional[str] = None,
    include_brand_in_search: bool = False,
) -> list[dict[str, Any]]:
    """Read and normalize all line items from the predefined RFQ Excel workbook."""
    if not os.path.exists(excel_path):
        sys.exit(f"Error: Excel file not found: {excel_path}")

    wb = load_workbook(filename=excel_path, read_only=True, data_only=True)
    try:
        if sheet_name:
            if sheet_name not in wb.sheetnames:
                sys.exit(f"Error: Sheet '{sheet_name}' not found. Available: {wb.sheetnames}")
            ws = wb[sheet_name]
        else:
            ws = wb.active

        rows_iter = ws.iter_rows(values_only=True)
        header_row = None
        for row in rows_iter:
            if any(cell is not None and str(cell).strip() for cell in row):
                header_row = row
                break

        if not header_row:
            sys.exit("Error: Excel worksheet is empty.")

        col_map: dict[int, str] = {}
        for idx, cell in enumerate(header_row):
            norm = normalize_header(cell)
            if norm in HEADER_ALIASES:
                col_map[idx] = HEADER_ALIASES[norm]

        if "name" not in col_map.values():
            sys.exit(
                f"Error: Could not find required 'name' column. Found headers: {list(header_row)}"
            )

        items: list[dict[str, Any]] = []
        auto_pos = 1

        for row in rows_iter:
            if not any(cell is not None and str(cell).strip() for cell in row):
                continue

            raw_record: dict[str, Any] = {}
            for idx, canonical in col_map.items():
                raw_record[canonical] = row[idx] if idx < len(row) else None

            name = str(raw_record.get("name") or "").strip()
            if not name:
                continue

            pos_num = parse_number(raw_record.get("position"))
            position = int(pos_num) if pos_num is not None else auto_pos
            auto_pos += 1

            description = str(raw_record.get("description") or "").strip()
            unit = str(raw_record.get("unit") or "").strip() or None
            qty_num = parse_number(raw_record.get("qty"))
            qty = int(round(qty_num)) if qty_num is not None else None
            min_price = parse_number(raw_record.get("min_price"))
            max_price = parse_number(raw_record.get("max_price"))

            brand_raw = str(raw_record.get("brand") or "").strip() or None
            brand_name, allow_similar = parse_brand_requirement(brand_raw)

            specs_dict, spec_certs, specs_text = parse_technical_specs(
                raw_record.get("technical_specs")
            )
            merged_desc = merge_description_and_specs(description, specs_text)
            search_query = build_search_query(
                name=name,
                brand_name=brand_name,
                allow_similar=allow_similar,
                specs_dict=specs_dict,
                include_brand_in_search=include_brand_in_search,
            )

            item = {
                "position": position,
                "name": name,
                "description": description,
                "merged_description": merged_desc,
                "unit": unit,
                "unit_canonical": normalize_unit(unit),
                "qty": qty,
                "min_price": min_price,
                "max_price": max_price,
                "brand_raw": brand_raw,
                "brand_name": brand_name,
                "allow_similar": allow_similar,
                "technical_specs": specs_dict,
                "technical_specs_text": specs_text,
                "extracted_certs": spec_certs,
                "search_query": search_query,
            }
            item["relevance_keywords"] = extract_relevance_keywords(item)
            items.append(item)
    finally:
        wb.close()

    return items


def load_cache(cache_path: Optional[str]) -> dict[str, Any]:
    if cache_path and os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    return {}


def save_cache(cache_path: Optional[str], cache_data: dict[str, Any]) -> None:
    if not cache_path:
        return
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, indent=2, ensure_ascii=False)


def evaluate_rfq_position(
    item: dict[str, Any],
    listings: list[dict[str, Any]],
    global_required_certs: list[str],
    min_years: int,
    require_ta: bool,
    strict_moq: bool,
    strict_certs: bool,
    strict_min_price: bool,
) -> dict[str, Any]:
    """Filter and score scraped listings for a single Excel RFQ line item."""
    combined_certs = rv.normalize_cert_list(global_required_certs + item["extracted_certs"])
    weights = rv.normalized_weights(rv.RAW_WEIGHTS)

    vendors = rv.aggregate_vendors(listings, target_qty=item["qty"])
    qualified = []
    disqualified = []

    for v in vendors:
        reasons = rv.check_hard_filters(
            vendor=v,
            target_qty=item["qty"],
            strict_moq=strict_moq,
            max_price=item["max_price"],
            required_certs=combined_certs,
            strict_certs=strict_certs,
            min_years=min_years,
            require_ta=require_ta,
        )

        offer = v["best_offer"]
        if strict_min_price and item["min_price"] is not None and offer["price_max"] is not None:
            if offer["price_max"] < item["min_price"]:
                reasons.append(
                    f"Price (${offer['price_max']:.2f}) below strict min price (${item['min_price']:.2f})"
                )

        if reasons:
            disqualified.append({**v, "disqualification_reasons": reasons})
        else:
            qualified.append(v)

    base_ranked = rv.score_vendors(
        vendors=qualified,
        weights=weights,
        target_qty=item["qty"],
        required_certs=combined_certs,
    )

    total_blend = max(1.0, WEIGHT_VENDOR_SCORE + WEIGHT_RFQ_FIT)
    w_vendor = WEIGHT_VENDOR_SCORE / total_blend
    w_fit = WEIGHT_RFQ_FIT / total_blend

    final_ranked = []
    for v in base_ranked:
        offer = v["best_offer"]
        pros = list(v["pros"])
        warnings = list(v["warnings"])
        haystack = f"{offer['title']} {v['supplier']}".lower()

        # 1. Technical specs & description keyword fit (0-100)
        keywords = item["relevance_keywords"]
        if keywords:
            matched_kw = [kw for kw in keywords if kw in haystack]
            spec_score = round(100.0 * len(matched_kw) / len(keywords), 1)
            if matched_kw:
                pros.append(f"Matched specs: {', '.join(matched_kw[:6])}")
            else:
                warnings.append("Listing title lacks specific technical spec keywords — confirm via RFQ")
        else:
            matched_kw = []
            spec_score = 75.0

        # 2. Brand fit (0-100)
        brand_status = "N/A"
        if item["brand_name"]:
            brand_found = item["brand_name"].lower() in haystack
            if brand_found:
                brand_score = 100.0
                brand_status = f"Exact ({item['brand_name']})"
                pros.append(f"Matches requested brand ({item['brand_name']})")
            elif item["allow_similar"]:
                brand_score = 65.0
                brand_status = f"Alternative to {item['brand_name']}"
                warnings.append(
                    f"Alternative/OEM to '{item['brand_name']}' — confirm technical equivalence"
                )
            else:
                brand_score = 15.0
                brand_status = f"Mismatch (requested {item['brand_name']})"
                warnings.append(f"Requested brand '{item['brand_name']}' not found in listing")
            rfq_fit_score = round(0.6 * spec_score + 0.4 * brand_score, 1)
        else:
            brand_score = None
            rfq_fit_score = spec_score

        # 3. Min-price floor check (soft penalty if not strict)
        price_penalty = 0.0
        if item["min_price"] is not None and offer["price_max"] is not None:
            if offer["price_max"] < item["min_price"]:
                price_penalty = 10.0
                warnings.append(
                    f"Listed price (${offer['price_max']:.2f}) is below target floor (${item['min_price']:.2f}) — verify specs/grade"
                )

        # 4. Unit compatibility check
        vendor_unit_norm = normalize_unit(offer.get("moq_unit"))
        if item["unit_canonical"] and vendor_unit_norm and item["unit_canonical"] != vendor_unit_norm:
            warnings.append(
                f"Unit mismatch: RFQ asks '{item['unit']}', vendor lists '{offer['moq_unit']}'"
            )

        vendor_score = v["total_score"]
        final_score = round(
            max(0.0, (vendor_score * w_vendor) + (rfq_fit_score * w_fit) - price_penalty), 1
        )

        if final_score >= 75:
            tier = "Strong Recommend"
        elif final_score >= 55:
            tier = "Consider"
        else:
            tier = "Caution"

        final_ranked.append(
            {
                **v,
                "vendor_score": vendor_score,
                "rfq_fit_score": rfq_fit_score,
                "spec_match_score": spec_score,
                "brand_score": brand_score,
                "brand_status": brand_status,
                "matched_specs": matched_kw,
                "total_score": final_score,
                "recommendation": tier,
                "pros": pros,
                "warnings": warnings,
            }
        )

    final_ranked.sort(
        key=lambda x: (
            x["total_score"],
            x["has_trade_assurance"],
            x["years_active"] or 0,
        ),
        reverse=True,
    )

    return {
        "rfq_item": item,
        "required_certs_applied": combined_certs,
        "recommended": final_ranked,
        "disqualified": disqualified,
    }


def export_excel_report(report: dict[str, Any], output_path: str, top_n: int) -> None:
    """Write a multi-sheet client-ready Excel workbook with Recommendations, Disqualified, and Input."""
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")

    # Sheet 1: Recommendations
    ws_rec = wb.active
    ws_rec.title = "Recommendations"
    rec_headers = [
        "Position", "Item Name", "Search Query", "Client Qty", "Client Unit",
        "Price Range (Client)", "Client Brand", "Rank", "Recommendation",
        "Final Score", "Vendor Score", "RFQ Fit Score", "Supplier", "Supplier ID",
        "Vendor Price", "Vendor MOQ", "Vendor Unit", "Years Active",
        "Trade Assurance", "Certifications", "Brand Status", "Pros", "Warnings",
        "Matched Product Title", "URL",
    ]
    ws_rec.append(rec_headers)
    for cell in ws_rec[1]:
        cell.font = header_font
        cell.fill = header_fill

    for entry in report["positions"]:
        item = entry["rfq_item"]
        p_range = f"{item['min_price'] or ''} - {item['max_price'] or ''}".strip(" -")
        recs = entry["recommended"][:top_n]
        if not recs:
            ws_rec.append([
                item["position"], item["name"], item["search_query"], item["qty"],
                item["unit"] or "", p_range, item["brand_raw"] or "",
                "-", "No Qualified Vendors", "", "", "", "", "", "", "", "", "",
                "", "", "", "", "All vendors filtered out or 0 results", "", "",
            ])
            continue

        for rank, v in enumerate(recs, start=1):
            offer = v["best_offer"]
            ws_rec.append([
                item["position"],
                item["name"],
                item["search_query"],
                item["qty"],
                item["unit"] or "",
                p_range,
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

    # Sheet 2: Disqualified
    ws_disq = wb.create_sheet("Disqualified")
    disq_headers = [
        "Position", "Item Name", "Supplier", "Supplier ID",
        "Vendor Price", "Vendor MOQ", "Disqualification Reasons", "URL",
    ]
    ws_disq.append(disq_headers)
    for cell in ws_disq[1]:
        cell.font = header_font
        cell.fill = header_fill

    for entry in report["positions"]:
        item = entry["rfq_item"]
        for d in entry["disqualified"]:
            offer = d["best_offer"]
            ws_disq.append([
                item["position"],
                item["name"],
                d["supplier"],
                d["supplier_id"] or "",
                offer["price_raw"] or "",
                int(offer["moq"]) if offer["moq"] is not None else "",
                "; ".join(d["disqualification_reasons"]),
                offer["url"] or "",
            ])

    # Sheet 3: Parsed RFQ Input (shows merged description + generated query)
    ws_in = wb.create_sheet("Parsed RFQ Input")
    in_headers = [
        "Position", "Name", "Merged Description & Specs", "Unit", "Qty",
        "Min Price", "Max Price", "Brand", "Extracted Certs", "Generated Search Query",
    ]
    ws_in.append(in_headers)
    for cell in ws_in[1]:
        cell.font = header_font
        cell.fill = header_fill

    for entry in report["positions"]:
        item = entry["rfq_item"]
        ws_in.append([
            item["position"],
            item["name"],
            item["merged_description"],
            item["unit"] or "",
            item["qty"],
            item["min_price"],
            item["max_price"],
            item["brand_raw"] or "",
            ", ".join(entry["required_certs_applied"]),
            item["search_query"],
        ])

    for ws in (ws_rec, ws_disq, ws_in):
        ws.freeze_panes = "A2"

    wb.save(output_path)
    print(f"\nSaved Excel report to {output_path}", file=sys.stderr)


def create_sample_excel(path: str) -> None:
    """Generate a sample RFQ Excel file matching the expected schema."""
    wb = Workbook()
    ws = wb.active
    ws.title = "RFQ"
    ws.append([
        "position", "name", "description", "unit", "qty",
        "min price", "max price", "brand", "technical specs",
    ])
    ws.append([
        1,
        "6-in-1 USB-C Hub",
        "Aluminum USB-C multiport adapter with 4K HDMI and SD/TF reader",
        "pieces",
        100,
        3.50,
        8.50,
        "Anker or similar",
        json.dumps({"hdmi": "4K 60Hz", "pd_power": "100W", "material": "Aluminum", "certifications": ["CE", "RoHS"]}),
    ])
    ws.append([
        2,
        "Silicone Spatula Set",
        "Food-grade heat-resistant kitchen spatula set, seamless design",
        "sets",
        250,
        1.80,
        5.00,
        "",
        json.dumps({"material": "BPA-free silicone", "max_temp": "230C", "certifications": ["FDA", "LFGB"]}),
    ])
    wb.save(path)
    print(f"Created sample RFQ workbook: {path}", file=sys.stderr)


def print_summary(report: dict[str, Any], top_n: int) -> None:
    print("\n=== RFQ EXCEL VENDOR RECOMMENDATIONS ===")
    for entry in report["positions"]:
        item = entry["rfq_item"]
        recs = entry["recommended"]
        disq = entry["disqualified"]
        print(
            f"\n[Pos #{item['position']}] {item['name']} | Qty: {item['qty']} {item['unit'] or ''} | "
            f"Budget: ${item['min_price'] or '?'} - ${item['max_price'] or '?'} | "
            f"Brand: {item['brand_raw'] or 'Any'}"
        )
        print(f"  Query: \"{item['search_query']}\" ({len(recs)} qualified, {len(disq)} disqualified)")

        for rank, v in enumerate(recs[:top_n], start=1):
            offer = v["best_offer"]
            print(
                f"  #{rank} [{v['total_score']}/100 {v['recommendation']}] {v['supplier']} "
                f"(Vendor:{v['vendor_score']} | Fit:{v['rfq_fit_score']})"
            )
            print(
                f"      Price: {offer['price_raw'] or 'N/A'} | MOQ: {offer['moq']} {offer['moq_unit']} | "
                f"Active: {v['years_active'] or '?'}y | TA: {'Yes' if v['has_trade_assurance'] else 'No'} | "
                f"Brand: {v['brand_status']}"
            )
            if v["warnings"]:
                print(f"      ! {'; '.join(v['warnings'])}")


def main():
    parser = argparse.ArgumentParser(
        description="Process a predefined RFQ Excel sheet, scrape Alibaba, and recommend vendors."
    )
    parser.add_argument("excel_file", nargs="?", default=DEFAULT_RFQ_FILE, help="Input .xlsx RFQ file")
    parser.add_argument("--sheet", default=DEFAULT_SHEET_NAME, help="Worksheet name (defaults to active sheet)")
    parser.add_argument("--create-sample", metavar="PATH", help="Generate a sample RFQ .xlsx file and exit")
    parser.add_argument("--dry-run", action="store_true", help="Parse Excel & preview search queries without scraping")
    parser.add_argument("--from-cache", action="store_true", help="Skip ScrapingBee calls and score from cache file only")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore cached rows and re-scrape all positions")
    parser.add_argument("--cache-file", default=DEFAULT_CACHE_FILE, help="JSON checkpoint file for scraped listings")
    parser.add_argument("-p", "--pages", type=int, default=sa.DEFAULT_PAGES)
    parser.add_argument("--max-products", type=int, default=sa.MAX_PRODUCTS_PER_PAGE)
    parser.add_argument("--stealth", action=argparse.BooleanOptionalAction, default=sa.SCRAPINGBEE_STEALTH_PROXY)
    parser.add_argument("--include-brand-in-search", action=argparse.BooleanOptionalAction, default=DEFAULT_INCLUDE_BRAND_IN_SEARCH)
    parser.add_argument("--required-certs", default=rv.DEFAULT_REQUIRED_CERTS, help="Global certs added to every position")
    parser.add_argument("--min-years", type=int, default=rv.DEFAULT_MIN_YEARS)
    parser.add_argument("--require-trade-assurance", action=argparse.BooleanOptionalAction, default=rv.DEFAULT_REQUIRE_TA)
    parser.add_argument("--strict-moq", action=argparse.BooleanOptionalAction, default=rv.DEFAULT_STRICT_MOQ)
    parser.add_argument("--strict-certs", action=argparse.BooleanOptionalAction, default=rv.DEFAULT_STRICT_CERTS)
    parser.add_argument("--strict-min-price", action=argparse.BooleanOptionalAction, default=DEFAULT_STRICT_MIN_PRICE)
    parser.add_argument("--top", type=int, default=rv.DEFAULT_TOP_N)
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_FILE, help="Output .xlsx or .json report path")
    args = parser.parse_args()

    if args.create_sample:
        create_sample_excel(args.create_sample)
        return

    items = load_rfq_excel(
        args.excel_file,
        sheet_name=args.sheet,
        include_brand_in_search=args.include_brand_in_search,
    )
    if not items:
        sys.exit("Error: No valid product rows found in the Excel file.")

    if args.dry_run:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return

    cache = load_cache(args.cache_file)
    if not args.from_cache and not sa.SCRAPINGBEE_API_KEY:
        sys.exit("Error: SCRAPINGBEE_API_KEY is missing in .env (or pass --from-cache).")

    # Step 1: Scrape (or load from checkpoint cache) per position
    scraped_by_pos: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        pos_key = str(item["position"])
        query = item["search_query"]
        cached_entry = cache.get(pos_key)

        use_cached = (
            not args.force_refresh
            and isinstance(cached_entry, dict)
            and cached_entry.get("search_query") == query
            and isinstance(cached_entry.get("products"), list)
        )

        if use_cached or args.from_cache:
            products = (cached_entry or {}).get("products", [])
            print(
                f"[Pos #{pos_key}] Loaded {len(products)} cached listings for '{query}'",
                file=sys.stderr,
            )
            scraped_by_pos[pos_key] = products
            continue

        pos_products = []
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
                pos_products.extend(page_items)
            except requests.HTTPError as exc:
                print(
                    f"  [Pos #{pos_key}] HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"  [Pos #{pos_key}] Failed (page {page}): {exc}", file=sys.stderr)
            time.sleep(sa.REQUEST_DELAY_SECONDS)

        scraped_by_pos[pos_key] = pos_products
        cache[pos_key] = {
            "position": item["position"],
            "name": item["name"],
            "search_query": query,
            "products": pos_products,
        }
        # Checkpoint after every row so interrupted runs never waste credits
        save_cache(args.cache_file, cache)

    # Step 2: Score and rank vendors per Excel line item
    global_certs = [c.strip() for c in args.required_certs.split(",") if c.strip()]
    positions_report = []
    for item in items:
        listings = scraped_by_pos.get(str(item["position"]), [])
        evaluated = evaluate_rfq_position(
            item=item,
            listings=listings,
            global_required_certs=global_certs,
            min_years=args.min_years,
            require_ta=args.require_trade_assurance,
            strict_moq=args.strict_moq,
            strict_certs=args.strict_certs,
            strict_min_price=args.strict_min_price,
        )
        positions_report.append(evaluated)

    report = {
        "source_excel": args.excel_file,
        "positions": positions_report,
    }

    print_summary(report, top_n=args.top)

    if args.output:
        if args.output.lower().endswith(".xlsx"):
            export_excel_report(report, args.output, top_n=args.top)
        else:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            print(f"\nSaved JSON report to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
