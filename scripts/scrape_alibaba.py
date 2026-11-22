import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import json_repair
import requests
from dotenv import load_dotenv
from pipeline_errors import InputError

load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val and val.strip() else default


def env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    return float(val) if val and val.strip() else default


SCRAPINGBEE_API_KEY = os.getenv("SCRAPINGBEE_API_KEY")
SCRAPINGBEE_URL = os.getenv("SCRAPINGBEE_URL", "https://app.scrapingbee.com/api/v1/")
ALIBABA_SEARCH_URL = os.getenv("ALIBABA_SEARCH_URL", "https://www.alibaba.com/trade/search")
SCRAPINGBEE_WAIT_MS = env_int("SCRAPINGBEE_WAIT_MS", 3000)
SCRAPINGBEE_TIMEOUT = env_int("SCRAPINGBEE_TIMEOUT", 90)
SCRAPINGBEE_STEALTH_PROXY = env_bool("SCRAPINGBEE_STEALTH_PROXY", False)

DEFAULT_PAGES = env_int("DEFAULT_PAGES", 1)
MAX_PRODUCTS_PER_PAGE = env_int("MAX_PRODUCTS_PER_PAGE", 15)
REQUEST_DELAY_SECONDS = env_float("REQUEST_DELAY_SECONDS", 1.0)
DEFAULT_TERMS_FILE = os.getenv("DEFAULT_TERMS_FILE", "").strip() or None
DEFAULT_OUTPUT_FILE = os.getenv("DEFAULT_OUTPUT_FILE", "").strip() or None
DEBUG_DIR = os.getenv("DEBUG_DIR", "").strip() or None


def build_ai_rules(max_products: int) -> dict:
    """Request concise raw values from ScrapingBee AI, then normalize in Python."""
    return {
        "products": {
            "description": (
                f"up to the first {max_products} organic product listings shown in the "
                "Alibaba search results (skip ads and banners)"
            ),
            "type": "list",
            "output": {
                "title": "short product title",
                "price": "displayed price or price range",
                "moq_raw": "minimum order quantity with its unit, e.g. '20 pieces'",
                "supplier": "supplier or company name",
                "supplier_url": "supplier store or company profile URL if present",
                "years_active": "number of years the supplier has been active on Alibaba, e.g. '8 yrs'",
                "certifications": "comma-separated certifications or supplier badges shown on the card (e.g. CE, RoHS, ISO, Verified), or empty string if none",
                "has_trade_assurance": "true if Trade Assurance badge or text is shown for this listing, otherwise false",
                "url": "clean product URL without query parameters (?...)",
            },
        }
    }

def parse_moq(raw: object) -> tuple[Optional[int | float], Optional[str]]:
    """Split strings like 'Min. order: 20 pieces' into (20, 'pieces')."""
    if raw is None:
        return None, None
    text = str(raw).strip()
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([a-zA-Z][a-zA-Z0-9\s/-]*)?", text)
    if not match:
        return None, None

    num_str = match.group(1).replace(",", "")
    qty: int | float = float(num_str) if "." in num_str else int(num_str)
    unit = match.group(2).strip() if match.group(2) else None
    return qty, unit


def parse_years_active(raw: object) -> Optional[int]:
    """Extract integer years from values like '8 yrs', '12 yrs CN', or 5."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    match = re.search(r"\d+", str(raw))
    return int(match.group(0)) if match else None


def extract_supplier_id(supplier_url: object) -> Optional[str]:
    """Derive a supplier identifier from the storefront subdomain or profile URL."""
    if not isinstance(supplier_url, str) or not supplier_url.strip():
        return None

    url = supplier_url.strip()
    if url.startswith("//"):
        url = f"https:{url}"
    if not url.startswith("http"):
        url = f"https://{url}"

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("companyId", "company_id", "supplierId", "memberId"):
        if key in query and query[key]:
            return query[key][0]

    # Storefront subdomain pattern: https://esamconn.en.alibaba.com/...
    host = (parsed.hostname or "").lower()
    if host.endswith(".alibaba.com"):
        subdomain = host.split(".")[0]
        if subdomain not in {"www", "m", "en", "alibaba", "offer", "message"}:
            return subdomain

    # Fallback: numeric ID in path if present
    path_match = re.search(r"(\d{6,})", parsed.path)
    return path_match.group(1) if path_match else None


def parse_certifications(raw: object) -> list[str]:
    """Normalize certifications into a clean list of strings."""
    ignored = {"", "none", "n/a", "na", "null", "unknown", "-"}
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw]
    elif isinstance(raw, str):
        items = [x.strip() for x in re.split(r"[,;/|]+", raw)]
    else:
        return []
    return [item for item in items if item.lower() not in ignored]


def parse_bool(raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "yes", "1"}
    return bool(raw)


def clean_products(products: list, was_repaired: bool) -> list[dict]:
    """Normalize all fields and drop any incomplete trailing record from truncation."""
    raw_items = [p for p in products if isinstance(p, dict) and p.get("title")]

    if was_repaired and raw_items:
        last = raw_items[-1]
        if not last.get("price") and not last.get("supplier"):
            raw_items.pop()

    cleaned = []
    for item in raw_items:
        url = item.get("url")
        if isinstance(url, str):
            url = url.split("?", 1)[0].strip()
            if url.startswith("//"):
                url = f"https:{url}"
        else:
            url = None

        moq_val, moq_unit = parse_moq(item.get("moq_raw") or item.get("moq"))

        cleaned.append(
            {
                "title": item.get("title"),
                "price": item.get("price"),
                "moq": moq_val,
                "moq_unit": moq_unit,
                "supplier": item.get("supplier"),
                "supplier_id": extract_supplier_id(item.get("supplier_url")),
                "years_active": parse_years_active(item.get("years_active")),
                "certifications": parse_certifications(item.get("certifications")),
                "has_trade_assurance": parse_bool(item.get("has_trade_assurance")),
                "url": url,
            }
        )

    return cleaned


def build_search_url(term: str, page: int = 1) -> str:
    query = urlencode({"SearchText": term, "tab": "all", "page": page})
    return f"{ALIBABA_SEARCH_URL}?{query}"


def save_debug_response(debug_dir: str, term: str, page: int, raw_text: str) -> str:
    os.makedirs(debug_dir, exist_ok=True)
    safe_term = re.sub(r"[^a-zA-Z0-9_-]+", "_", term).strip("_")
    path = os.path.join(debug_dir, f"{safe_term}_p{page}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw_text)
    return path


def parse_ai_response(raw_text: str) -> tuple[dict, bool]:
    """Parse JSON strictly first; fall back to json-repair if truncated."""
    try:
        return json.loads(raw_text), False
    except json.JSONDecodeError:
        repaired = json_repair.loads(raw_text)
        if isinstance(repaired, dict):
            return repaired, True
        if isinstance(repaired, list):
            return {"products": repaired}, True
        raise ValueError(f"Unparseable response snippet: {raw_text[:200]}")


@dataclass
class ScrapeResult:
    """Structured outcome of one ScrapingBee page request."""

    products: list = field(default_factory=list)
    spb_cost: Optional[int] = None  # credits reported by the Spb-cost header
    repaired: bool = False  # json-repair fallback was needed
    debug_path: Optional[str] = None  # raw payload dump, if one was written


def _parse_cost(raw: Optional[str]) -> Optional[int]:
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def scrape_term_detailed(
    term: str,
    api_key: str,
    page: int = 1,
    stealth: bool = False,
    max_products: int = 20,
    debug_dir: Optional[str] = None,
    quiet: bool = False,
) -> ScrapeResult:
    """Fetch one Alibaba search page via ScrapingBee and return a structured result.

    The ``Authorization`` header carries the API key; the key is never placed in
    the URL, logged, or attached to raised exceptions.
    """
    target_url = build_search_url(term, page=page)

    headers = {"Authorization": f"Bearer {api_key}"}
    params = {
        "url": target_url,
        "render_js": "true",
        "wait": str(SCRAPINGBEE_WAIT_MS),
        "ai_extract_rules": json.dumps(build_ai_rules(max_products)),
    }

    if stealth:
        params["stealth_proxy"] = "true"
    else:
        params["premium_proxy"] = "true"

    def log(msg: str) -> None:
        if not quiet:
            print(msg, file=sys.stderr)

    log(f"Searching Alibaba for '{term}' (page {page})...")
    response = requests.get(
        SCRAPINGBEE_URL,
        headers=headers,
        params=params,
        timeout=SCRAPINGBEE_TIMEOUT,
    )
    response.raise_for_status()

    cost_raw = response.headers.get("Spb-cost", "unknown")
    raw_text = response.text
    debug_path: Optional[str] = None

    try:
        data, was_repaired = parse_ai_response(raw_text)
    except Exception:
        if debug_dir:
            saved = save_debug_response(debug_dir, term, page, raw_text)
            log(f"  Saved raw response to {saved}")
        raise

    products = clean_products(data.get("products", []), was_repaired)

    if was_repaired:
        if debug_dir:
            debug_path = save_debug_response(debug_dir, term, page, raw_text)
        log(f"  Done (credits: {cost_raw}) - repaired truncated JSON, recovered {len(products)} products")
    else:
        log(f"  Done (credits: {cost_raw}) - extracted {len(products)} products")

    return ScrapeResult(
        products=products,
        spb_cost=_parse_cost(cost_raw),
        repaired=was_repaired,
        debug_path=debug_path,
    )


def scrape_term(
    term: str,
    api_key: str,
    page: int = 1,
    stealth: bool = False,
    max_products: int = 20,
    debug_dir: Optional[str] = None,
) -> list[dict]:
    """CLI-compatible wrapper: returns only the cleaned product list."""
    return scrape_term_detailed(
        term,
        api_key,
        page=page,
        stealth=stealth,
        max_products=max_products,
        debug_dir=debug_dir,
    ).products


def load_terms(cli_terms: list[str], file_path: Optional[str]) -> list[str]:
    terms = [t.strip() for t in cli_terms if t.strip()]
    resolved_file = file_path
    if not terms and not resolved_file and DEFAULT_TERMS_FILE:
        resolved_file = DEFAULT_TERMS_FILE

    if resolved_file:
        if not os.path.exists(resolved_file):
            raise InputError(f"Terms file not found: {resolved_file}")
        with open(resolved_file, "r", encoding="utf-8") as f:
            for line in f:
                cleaned = line.strip()
                if cleaned and not cleaned.startswith("#"):
                    terms.append(cleaned)

    return terms


def main():
    parser = argparse.ArgumentParser(
        description="Search Alibaba by a list of terms using ScrapingBee."
    )
    parser.add_argument("terms", nargs="*", help="Search terms (overrides DEFAULT_TERMS_FILE)")
    parser.add_argument("-f", "--file", help="Text file with one search term per line")
    parser.add_argument("-p", "--pages", type=int, default=DEFAULT_PAGES)
    parser.add_argument(
        "--max-products",
        type=int,
        default=MAX_PRODUCTS_PER_PAGE,
        help=f"Max products to extract per page (default: {MAX_PRODUCTS_PER_PAGE})",
    )
    parser.add_argument(
        "--stealth",
        action=argparse.BooleanOptionalAction,
        default=SCRAPINGBEE_STEALTH_PROXY,
    )
    parser.add_argument("-o", "--output", default=DEFAULT_OUTPUT_FILE)
    parser.add_argument(
        "--debug-dir",
        default=DEBUG_DIR,
        help="Directory to save raw responses if JSON repair or errors occur",
    )
    args = parser.parse_args()

    if not SCRAPINGBEE_API_KEY:
        sys.exit("Error: SCRAPINGBEE_API_KEY is missing. Add it to your .env file.")

    try:
        terms = load_terms(args.terms, args.file)
    except InputError as exc:
        sys.exit(f"Error: {exc}")
    if not terms:
        sys.exit("Error: Pass search terms, use -f terms.txt, or set DEFAULT_TERMS_FILE in .env.")

    results = {}
    for term in terms:
        term_products = []
        for page in range(1, args.pages + 1):
            try:
                products = scrape_term(
                    term,
                    SCRAPINGBEE_API_KEY,
                    page=page,
                    stealth=args.stealth,
                    max_products=args.max_products,
                    debug_dir=args.debug_dir,
                )
                term_products.extend(products)
            except requests.HTTPError as exc:
                print(
                    f"  Failed for '{term}' (page {page}): HTTP {exc.response.status_code} - {exc.response.text[:200]}",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(f"  Failed for '{term}' (page {page}): {exc}", file=sys.stderr)

            time.sleep(REQUEST_DELAY_SECONDS)

        results[term] = term_products

    formatted = json.dumps(results, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(formatted)
        print(f"Saved results to {args.output}", file=sys.stderr)
    else:
        print(formatted)


if __name__ == "__main__":
    main()
