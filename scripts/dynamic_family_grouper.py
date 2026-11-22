import argparse
import json
import os
import re
import sys
from collections import Counter
from typing import Any, Optional

import json_repair
import numpy as np
import pandas as pd
from dotenv import load_dotenv
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from sklearn.cluster import AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

load_dotenv()

DEFAULT_INPUT = os.getenv("RFQ_EXCEL_FILE", "products.xlsx")
DEFAULT_GRANULARITY = os.getenv("FAMILY_GRANULARITY", "balanced")
DEFAULT_EXCEL_OUT = os.getenv("GROUPED_EXCEL_OUTPUT", "products_grouped.xlsx")
DEFAULT_JSON_OUT = os.getenv("FAMILY_PLAN_JSON", "family_search_plan.json")
DEFAULT_TERMS_OUT = os.getenv("FAMILY_TERMS_TXT", "family_terms.txt")

HEADER_ALIASES = {
    "position": "position",
    "pos": "position",
    "item_no": "position",
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
    "max_price": "max_price",
    "brand": "brand",
    "technical_specs": "technical_specs",
    "tech_specs": "technical_specs",
    "specs": "technical_specs",
    "specifications": "technical_specs",
}

# Purely domain-agnostic commercial modifiers, colors, sizes, and packaging words
GENERIC_MODIFIERS = {
    "a", "an", "the", "of", "in", "on", "to", "for", "with", "and", "or", "by", "at", "non",
    "new", "pro", "plus", "max", "mini", "micro", "ultra", "super", "high", "low",
    "large", "small", "medium", "compact", "slim", "thin", "heavy", "light", "duty",
    "extended", "long", "short", "wide", "dual", "single", "triple", "quad", "multi",
    "portable", "wireless", "wired", "silent", "quiet", "ergonomic", "adjustable",
    "universal", "standard", "basic", "advanced", "premium", "professional", "precision",
    "electric", "manual", "automatic", "auto", "smart", "digital", "analog", "cordless",
    "external", "internal", "built", "powered", "passive", "active", "modular", "foldable",
    "oem", "odm", "generic", "original", "wholesale", "custom", "pack", "set", "kit",
    "pair", "piece", "pieces", "unit", "units", "pcs", "box", "tube", "roll", "bag",
    "entry", "level", "mid", "full", "all", "one", "gen", "series", "edition", "grade",
    "color", "black", "white", "silver", "gray", "grey", "red", "blue", "green",
    "rgb", "led", "bronze", "gold", "stainless", "business", "office", "home",
    "commercial", "industrial", "desktop", "gaming",
}

# Generic JSON spec keys that appear across unrelated industries
GENERIC_SPEC_KEYS = {
    "type", "quantity", "qty", "size", "color", "colour", "material",
    "includes", "weight", "dimensions", "brand", "model", "warranty", "certifications",
}


def normalize_header(raw: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(raw).strip().lower()).strip("_")


def simple_singularize(word: str) -> str:
    """Lightweight plural-to-singular normalizer with zero external corpora."""
    if len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")) and len(word) > 4:
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is", "os", "ps")):
        return word[:-1]
    return word


def load_catalog(excel_path: str, sheet_name: Optional[str] = None) -> pd.DataFrame:
    if not os.path.exists(excel_path):
        sys.exit(f"Error: File not found: {excel_path}")

    if excel_path.lower().endswith(".csv"):
        df = pd.read_csv(excel_path)
    else:
        xls = pd.ExcelFile(excel_path)
        target_sheet = sheet_name or xls.sheet_names[0]
        if target_sheet not in xls.sheet_names:
            sys.exit(f"Error: Sheet '{target_sheet}' not found. Available: {xls.sheet_names}")
        df = pd.read_excel(excel_path, sheet_name=target_sheet)

    df = df.rename(columns={c: HEADER_ALIASES.get(normalize_header(c), normalize_header(c)) for c in df.columns})
    if "name" not in df.columns:
        sys.exit(f"Error: Required 'name' column not found. Columns: {list(df.columns)}")

    df = df[df["name"].notna() & (df["name"].astype(str).str.strip() != "")].copy()
    if "position" not in df.columns:
        df["position"] = range(1, len(df) + 1)

    for col in ("description", "unit", "brand", "technical_specs"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str).str.strip()

    for num_col in ("qty", "min_price", "max_price"):
        if num_col not in df.columns:
            df[num_col] = np.nan
        df[num_col] = pd.to_numeric(df[num_col], errors="coerce")

    return df.reset_index(drop=True)


def extract_brand_tokens(df: pd.DataFrame) -> set[str]:
    """Dynamically learn brand words from the sheet's own brand column."""
    tokens = set()
    ignore = {"oem", "odm", "generic", "similar", "equivalent", "chip", "none", "any", "or", "na", "brand"}
    for val in df["brand"].dropna().astype(str):
        cleaned = re.sub(r"\([^)]*\)", " ", val)
        for tok in re.findall(r"[a-zA-Z]{2,}", cleaned.lower()):
            if tok not in ignore:
                tokens.add(tok)
    return tokens


def parse_spec_keys(raw_specs: str) -> str:
    """Extract informative JSON schema keys from technical_specs without relying on values."""
    text = str(raw_specs).strip()
    if not text or not (text.startswith("{") or text.startswith("[")):
        return ""
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        try:
            obj = json_repair.loads(text)
        except Exception:
            return ""

    if not isinstance(obj, dict):
        return ""

    tokens = []
    for k in obj.keys():
        k_clean = re.sub(r"[^a-zA-Z0-9_]+", " ", str(k).lower()).strip()
        if not k_clean or k_clean in GENERIC_SPEC_KEYS:
            continue
        tokens.append(k_clean)
        for part in k_clean.split("_"):
            if len(part) >= 2 and part not in GENERIC_SPEC_KEYS and part not in {"max", "min"}:
                tokens.append(simple_singularize(part))
    return " ".join(tokens)


def extract_views(df: pd.DataFrame) -> pd.DataFrame:
    """Build 5 domain-agnostic text/schema views per product row."""
    brand_tokens = extract_brand_tokens(df)
    rows = []

    for _, row in df.iterrows():
        name = str(row["name"]).strip()
        desc = str(row["description"]).strip()

        # 1. Strip parenthetical variants & trailing prepositional qualifiers ("for ...", "with ...")
        s_no_paren = re.sub(r"\([^)]*\)", " ", name)
        s_main = re.split(
            r"\b(?:for|with|w/|without|featuring|including|compatible)\b",
            s_no_paren,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        s_main = re.sub(r"[/,+&]", " ", s_main)

        clean_tokens = []
        for tok in re.findall(r"[a-zA-Z0-9-]+", s_main):
            low = tok.lower()
            # Drop tokens containing digits (dimensions, voltages, pack counts, model codes)
            if re.search(r"\d", low):
                continue
            for sub in low.split("-"):
                sub = simple_singularize(sub)
                if len(sub) >= 2 and sub not in brand_tokens:
                    clean_tokens.append(sub)

        content_tokens = [t for t in clean_tokens if t not in GENERIC_MODIFIERS]
        if not content_tokens:
            content_tokens = clean_tokens or [
                simple_singularize(w.lower()) for w in re.findall(r"[a-zA-Z]{2,}", s_no_paren)
            ]

        terminal_noun = content_tokens[-1] if content_tokens else ""
        head_bigram = " ".join(content_tokens[-2:]) if len(content_tokens) >= 2 else terminal_noun
        core_phrase = " ".join(content_tokens)

        # 2. Cleaned description + name context
        desc_tokens = [
            simple_singularize(tok)
            for tok in re.findall(r"[a-zA-Z]{2,}", f"{s_no_paren} {desc}".lower())
        ]
        desc_clean = " ".join(
            t for t in desc_tokens if t not in GENERIC_MODIFIERS and t not in brand_tokens
        )

        rows.append(
            {
                "terminal_noun": terminal_noun,
                "head_bigram": head_bigram,
                "core_phrase": core_phrase,
                "spec_keys": parse_spec_keys(row["technical_specs"]),
                "desc_clean": desc_clean,
            }
        )

    return pd.DataFrame(rows)


def safe_tfidf_cosine(series: pd.Series, ngram_range: tuple[int, int]) -> tuple[np.ndarray, bool]:
    n = len(series)
    if series.str.strip().str.len().sum() == 0:
        return np.zeros((n, n)), False
    vec = TfidfVectorizer(ngram_range=ngram_range, token_pattern=r"(?u)\b\w+\b")
    try:
        mat = vec.fit_transform(series)
        return cosine_similarity(mat), True
    except ValueError:
        return np.zeros((n, n)), False


def compute_similarity_matrices(df: pd.DataFrame, vdf: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Compute Stage-1 (strict product identity) and Stage-2 (broad supplier vertical) similarities."""
    S_term, _ = safe_tfidf_cosine(vdf["terminal_noun"], (1, 1))
    S_head, _ = safe_tfidf_cosine(vdf["head_bigram"], (1, 2))
    S_core, _ = safe_tfidf_cosine(vdf["core_phrase"], (1, 2))
    S_spec, has_spec = safe_tfidf_cosine(vdf["spec_keys"], (1, 1))
    S_desc, _ = safe_tfidf_cosine(vdf["desc_clean"], (1, 2))

    # Soft log-price compatibility damping (only dampens when price ratio > ~4.5x)
    min_p = df["min_price"]
    max_p = df["max_price"]
    mid_p = (min_p.fillna(max_p) + max_p.fillna(min_p)) / 2.0
    if mid_p.notna().sum() >= 2:
        log_p = np.log10(np.maximum(mid_p.fillna(mid_p.median()).values, 0.1))
        diff = np.abs(log_p[:, None] - log_p[None, :])
        price_factor = np.where(diff <= 0.65, 1.0, np.exp(-0.65 * (diff - 0.65)))
    else:
        price_factor = np.ones((len(df), len(df)))

    w_spec1 = 0.18 if has_spec else 0.0
    w_desc1 = 0.12 if has_spec else 0.30
    S_strict = (
        0.30 * S_term + 0.25 * S_head + 0.15 * S_core + w_spec1 * S_spec + w_desc1 * S_desc
    ) * price_factor
    np.fill_diagonal(S_strict, 1.0)

    w_spec2 = 0.30 if has_spec else 0.0
    w_desc2 = 0.40 if has_spec else 0.70
    S_broad = (0.30 * S_core + w_spec2 * S_spec + w_desc2 * S_desc) * np.sqrt(price_factor)
    np.fill_diagonal(S_broad, 1.0)

    return np.clip(S_strict, 0.0, 1.0), np.clip(S_broad, 0.0, 1.0)


def cluster_products(
    S_strict: np.ndarray,
    S_broad: np.ndarray,
    granularity: str = "balanced",
    stage1_distance: float = 0.75,
    stage2_min_sim: Optional[float] = None,
) -> np.ndarray:
    """Two-stage agglomerative clustering without forcing unrelated singletons together."""
    n = S_strict.shape[0]
    if n <= 1:
        return np.zeros(n, dtype=int)

    D_strict = np.clip(1.0 - S_strict, 0.0, 1.0)
    stage1 = AgglomerativeClustering(
        n_clusters=None,
        metric="precomputed",
        linkage="average",
        distance_threshold=stage1_distance,
    )
    labels = stage1.fit_predict(D_strict)

    if granularity == "fine" and stage2_min_sim is None:
        return labels

    merge_sim = stage2_min_sim if stage2_min_sim is not None else (0.18 if granularity == "balanced" else 0.11)
    unique_c = sorted(set(labels))
    k1 = len(unique_c)
    if k1 <= 1:
        return labels

    members = [np.where(labels == c)[0] for c in unique_c]
    C_sim = np.eye(k1)
    for i in range(k1):
        for j in range(i + 1, k1):
            sim_ij = float(S_broad[np.ix_(members[i], members[j])].mean())
            C_sim[i, j] = C_sim[j, i] = sim_ij

    D_cluster = np.clip(1.0 - C_sim, 0.0, 1.0)
    stage2 = AgglomerativeClustering(
        n_clusters=None,
        metric="precomputed",
        linkage="average",
        distance_threshold=1.0 - merge_sim,
    )
    c_labels = stage2.fit_predict(D_cluster)
    remap = {unique_c[i]: int(c_labels[i]) for i in range(k1)}
    return np.array([remap[c] for c in labels], dtype=int)


def generate_family_label_and_query(
    grp_vdf: pd.DataFrame, corpus_df_counts: Counter, n_corpus: int, query_suffix: str
) -> tuple[str, str]:
    """Dynamically name a family and synthesize an Alibaba query from shared head/core terms."""
    n_cluster = len(grp_vdf)
    if n_cluster == 1:
        core = grp_vdf["core_phrase"].iloc[0]
        words = list(dict.fromkeys(core.split()))[:4]
        label = " ".join(words).title() if words else "Single Item"
        query = " ".join(words + query_suffix.split()).strip()
        return label, query

    doc_freq_in = Counter()
    head_boost = Counter()
    for _, row in grp_vdf.iterrows():
        c_words = row["core_phrase"].split()
        t_noun = row["terminal_noun"]
        h_bi = row["head_bigram"]
        terms = set(c_words)
        if h_bi and len(h_bi.split()) == 2:
            terms.add(h_bi)
        for t in terms:
            doc_freq_in[t] += 1
        if t_noun:
            head_boost[t_noun] += 1.0
        if h_bi and len(h_bi.split()) == 2:
            head_boost[h_bi] += 1.0

    scored = []
    for term, in_cnt in doc_freq_in.items():
        coverage = in_cnt / n_cluster
        global_cnt = max(1, corpus_df_counts.get(term, in_cnt))
        idf = np.log((n_corpus + 1) / (global_cnt + 1)) + 1.0
        bigram_bonus = 1.25 if (len(term.split()) == 2 and coverage >= 0.45) else 1.0
        h_mult = 1.0 + 0.6 * (head_boost[term] / n_cluster)
        score = (coverage ** 1.45) * idf * h_mult * bigram_bonus
        scored.append((score, term, coverage))

    scored.sort(reverse=True)

    selected_phrases: list[str] = []
    seen_words: set[str] = set()
    for _, term, cov in scored:
        words = term.split()
        if len(seen_words) >= 2 and cov < 0.40:
            continue
        if len(seen_words) >= 1 and n_cluster >= 3 and cov < 0.34:
            continue

        if len(words) == 2 and any(p in words for p in selected_phrases):
            selected_phrases = [p for p in selected_phrases if p not in words]
            selected_phrases.insert(0, term)
            seen_words.update(words)
        elif not set(words).issubset(seen_words):
            selected_phrases.append(term)
            seen_words.update(words)

        if len(seen_words) >= 4 or len(selected_phrases) >= 3:
            break

    query_words: list[str] = []
    for phrase in selected_phrases:
        for w in phrase.split():
            if w not in query_words:
                query_words.append(w)

    label = " / ".join(p.title() for p in selected_phrases[:3]) if selected_phrases else "Misc Family"
    query = " ".join(query_words[:5] + query_suffix.split()).strip()
    return label, query


def build_plan(
    df: pd.DataFrame,
    vdf: pd.DataFrame,
    labels: np.ndarray,
    S_strict: np.ndarray,
    query_suffix: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    corpus_counts = Counter()
    for _, row in vdf.iterrows():
        terms = set(row["core_phrase"].split())
        if row["head_bigram"] and len(row["head_bigram"].split()) == 2:
            terms.add(row["head_bigram"])
        for t in terms:
            corpus_counts[t] += 1

    out = df.copy()
    out["_cluster"] = labels
    n_corpus = len(vdf)

    plan = []
    meta_map = {}

    for rank, (cid, grp) in enumerate(
        sorted(out.groupby("_cluster"), key=lambda x: (-len(x[1]), int(x[1]["position"].min()))),
        start=1,
    ):
        idxs = grp.index.values
        fid = f"FAM_{rank:02d}"
        fname, query = generate_family_label_and_query(vdf.loc[idxs], corpus_counts, n_corpus, query_suffix)

        # Cohesion score (0-100): average internal pairwise similarity so you can spot loose merges
        if len(idxs) > 1:
            sub = S_strict[np.ix_(idxs, idxs)]
            tri = sub[np.triu_indices(len(idxs), k=1)]
            cohesion = round(float(tri.mean()) * 100.0, 1)
        else:
            cohesion = 100.0

        meta_map[cid] = (fid, fname, query, cohesion)

        min_p = float(grp["min_price"].min()) if grp["min_price"].notna().any() else None
        max_p = float(grp["max_price"].max()) if grp["max_price"].notna().any() else None
        total_qty = int(grp["qty"].sum()) if grp["qty"].notna().any() else None
        min_item_qty = int(grp["qty"].min()) if grp["qty"].notna().any() else None

        plan.append(
            {
                "family_id": fid,
                "family_name": fname,
                "search_query": query,
                "cohesion_score": cohesion,
                "item_count": int(len(grp)),
                "positions": [int(p) for p in grp["position"].tolist()],
                "sample_items": grp["name"].tolist()[:5],
                "total_qty": total_qty,
                "min_item_qty": min_item_qty,
                "units": sorted({u for u in grp["unit"].tolist() if u}),
                "min_price": min_p,
                "max_price": max_p,
                "brands": sorted({b for b in grp["brand"].tolist() if b}),
            }
        )

    out["family_id"] = [meta_map[c][0] for c in out["_cluster"]]
    out["family_name"] = [meta_map[c][1] for c in out["_cluster"]]
    out["search_query"] = [meta_map[c][2] for c in out["_cluster"]]
    out["cohesion_score"] = [meta_map[c][3] for c in out["_cluster"]]
    out = out.drop(columns=["_cluster"])
    return out, plan


def export_excel(df: pd.DataFrame, plan: list[dict[str, Any]], output_path: str) -> None:
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")

    ws_plan = wb.active
    ws_plan.title = "Family_Search_Plan"
    ws_plan.append([
        "family_id", "family_name", "search_query", "item_count", "cohesion_score",
        "source_positions", "total_qty", "min_item_qty", "units",
        "min_price", "max_price", "brands", "sample_products",
    ])
    for cell in ws_plan[1]:
        cell.font = header_font
        cell.fill = header_fill

    for fam in plan:
        ws_plan.append([
            fam["family_id"],
            fam["family_name"],
            fam["search_query"],
            fam["item_count"],
            fam["cohesion_score"],
            ", ".join(str(p) for p in fam["positions"]),
            fam["total_qty"],
            fam["min_item_qty"],
            ", ".join(fam["units"]),
            fam["min_price"],
            fam["max_price"],
            ", ".join(fam["brands"]),
            " | ".join(fam["sample_items"]),
        ])

    ws_all = wb.create_sheet("Products_With_Families")
    cols = [
        "position", "family_id", "family_name", "search_query", "cohesion_score",
        "name", "description", "unit", "qty", "min_price", "max_price",
        "brand", "technical_specs",
    ]
    ws_all.append(cols)
    for cell in ws_all[1]:
        cell.font = header_font
        cell.fill = header_fill

    for _, row in df.iterrows():
        ws_all.append([row[c] if pd.notna(row[c]) else "" for c in cols])

    for ws in (ws_plan, ws_all):
        ws.freeze_panes = "A2"

    wb.save(output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Domain-agnostic, zero-LLM product family grouper for any Excel/CSV catalog."
    )
    parser.add_argument("input_file", nargs="?", default=DEFAULT_INPUT, help="Input .xlsx or .csv file")
    parser.add_argument("--sheet", default=None, help="Worksheet name (defaults to first sheet)")
    parser.add_argument(
        "--granularity",
        choices=["fine", "balanced", "broad"],
        default=DEFAULT_GRANULARITY,
        help="Family consolidation aggressiveness (default: balanced)",
    )
    parser.add_argument(
        "--stage2-sim",
        type=float,
        default=None,
        help="Optional custom Stage-2 merge similarity threshold (0.05-0.40, overrides --granularity)",
    )
    parser.add_argument(
        "--query-suffix",
        default="wholesale OEM",
        help="Suffix appended to auto-generated Alibaba search queries (default: 'wholesale OEM')",
    )
    parser.add_argument("--excel-out", default=DEFAULT_EXCEL_OUT)
    parser.add_argument("--json-out", default=DEFAULT_JSON_OUT)
    parser.add_argument("--terms-out", default=DEFAULT_TERMS_OUT)
    args = parser.parse_args()

    df = load_catalog(args.input_file, sheet_name=args.sheet)
    vdf = extract_views(df)
    S_strict, S_broad = compute_similarity_matrices(df, vdf)
    labels = cluster_products(
        S_strict,
        S_broad,
        granularity=args.granularity,
        stage2_min_sim=args.stage2_sim,
    )
    tagged_df, plan = build_plan(df, vdf, labels, S_strict, query_suffix=args.query_suffix)

    export_excel(tagged_df, plan, args.excel_out)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source_file": args.input_file,
                "granularity": args.granularity,
                "total_products": len(tagged_df),
                "total_families": len(plan),
                "api_call_reduction_pct": round((1 - len(plan) / len(tagged_df)) * 100, 1),
                "families": plan,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    with open(args.terms_out, "w", encoding="utf-8") as f:
        for fam in plan:
            f.write(fam["search_query"] + "\n")

    n_prod, n_fam = len(tagged_df), len(plan)
    reduction = (1 - n_fam / n_prod) * 100
    print(f"\nDynamic Grouping ({args.granularity}) | {n_prod} items -> {n_fam} families ({reduction:.1f}% fewer API calls)")
    print(f"Estimated ScrapingBee credits (1 pg/family): {n_fam * 30:,} (premium) | {n_fam * 80:,} (stealth)\n")

    for fam in plan:
        pos_str = ",".join(str(p) for p in fam["positions"])
        print(
            f"  {fam['family_id']} ({fam['item_count']:2d} items | cohesion {fam['cohesion_score']:5.1f}%) "
            f"{fam['family_name']} [pos: {pos_str}]"
        )
        print(f"      Query: {fam['search_query']}")

    print(f"\nSaved: {args.excel_out}, {args.json_out}, {args.terms_out}")


if __name__ == "__main__":
    main()
