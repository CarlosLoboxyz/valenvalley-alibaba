"""Zero-credit offline scoring, dashboard analytics and Excel export.

Scores exclusively from ``family_scrape_cache.json``. ``scrape_families`` is called with
``from_cache=True`` and ``api_key=None``, so a ScrapingBee request is structurally
impossible from this module.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..core.auth import AuthUser
from ..core.config import Settings
from ..core.errors import ApiError
from ..core.paths import AppPaths
from ..schemas.scoring import ScoreRequest
from .pipeline_bridge import InputError, pipeline
from .pocketbase_mirror import PocketBaseMirror
from .rfq_service import RFQService

VENDOR_KEYS = ("price", "moq", "trade_assurance", "years_active", "certifications")
RISK_FLAGS = {
    "bait_price": "Suspiciously low price",
    "moq_negotiable": "likely negotiable",
    "unit_mismatch": "Unit mismatch",
}
COMPONENTS = ("price", "moq", "years_active", "trade_assurance", "certifications")


def _percent(raw: dict[str, float]) -> dict[str, float]:
    total = sum(raw.values())
    return {k: round(100.0 * v / total, 2) for k, v in raw.items()}


def resolve_weights(req: ScoreRequest, s: Settings) -> tuple[dict[str, float], float, float, dict[str, Any]]:
    """Validate and normalize weights. Returns (vendor_raw, w_vendor, w_fit, effective)."""
    pick = lambda v, d: d if v is None else v  # noqa: E731
    vendor = {
        "price": pick(req.weight_price, s.weight_price),
        "moq": pick(req.weight_moq, s.weight_moq),
        "trade_assurance": pick(req.weight_trade_assurance, s.weight_trade_assurance),
        "years_active": pick(req.weight_years, s.weight_years),
        "certifications": pick(req.weight_certs, s.weight_certs),
    }
    wv = pick(req.weight_vendor_score, s.weight_vendor_score)
    wf = pick(req.weight_rfq_fit, s.weight_rfq_fit)

    problems = [k for k, v in {**vendor, "vendor_score": wv, "rfq_fit": wf}.items() if v < 0]
    if problems:
        raise ApiError(400, "invalid_weights", f"Weights must be non-negative: {', '.join(problems)}")
    if sum(vendor.values()) <= 0:
        raise ApiError(400, "invalid_weights", "Vendor score weights must not all be zero")
    if wv + wf <= 0:
        raise ApiError(400, "invalid_weights", "weight_vendor_score and weight_rfq_fit must not both be zero")

    effective = {"vendor": _percent(vendor), "blend": _percent({"vendor_score": wv, "rfq_fit": wf})}
    return vendor, wv, wf, effective


def _avg(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def report_to_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Derive the 4-tab JSON deliverable plus dashboard analytics from a stored report."""
    meta = report["meta"]
    top_n = int(meta.get("top_n", 5))

    recommendations: list[dict[str, Any]] = []
    disqualified: list[dict[str, Any]] = []
    tiers = {"Strong Recommend": 0, "Consider": 0, "Caution": 0}
    comp: dict[str, list[float]] = {k: [] for k in ("vendor_score", "rfq_fit_score", *COMPONENTS)}
    flags = {k: 0 for k in RISK_FLAGS}
    without = 0

    for entry in report["positions"]:
        item = entry["rfq_item"]
        shown = entry["recommended"][:top_n]
        if not shown:
            without += 1
        recommendations.append(
            {
                "position": item["position"],
                "name": item["name"],
                "qty": item["qty"],
                "unit": item["unit"],
                "min_price": item["min_price"],
                "max_price": item["max_price"],
                "brand": item["brand_raw"],
                "family_id": entry["family_id"],
                "family_name": entry["family_name"],
                "family_search_query": entry["family_search_query"],
                "required_certs_applied": entry["required_certs_applied"],
                "vendors": shown,
            }
        )
        for v in shown:
            tiers[v["recommendation"]] = tiers.get(v["recommendation"], 0) + 1
            comp["vendor_score"].append(v["vendor_score"])
            comp["rfq_fit_score"].append(v["rfq_fit_score"])
            for c in COMPONENTS:
                val = (v.get("score_breakdown") or {}).get(c)
                if isinstance(val, (int, float)):
                    comp[c].append(float(val))
            for flag, needle in RISK_FLAGS.items():
                if any(needle in w for w in v["warnings"]):
                    flags[flag] += 1
        for d in entry["disqualified"]:
            offer = d["best_offer"]
            disqualified.append(
                {
                    "position": item["position"],
                    "family_id": entry["family_id"],
                    "item_name": item["name"],
                    "supplier": d["supplier"],
                    "supplier_id": d["supplier_id"] or "",
                    "price": offer["price_raw"] or "",
                    "moq": offer["moq"],
                    "reasons": d["disqualification_reasons"],
                    "url": offer["url"] or "",
                }
            )

    cross = report.get("cross_item_vendors", [])
    missing = list(meta.get("missing_cache_families", []))
    warnings = [f"No cached listings for family {f}; run a scrape job to populate it" for f in missing]
    return {
        "meta": meta,
        "recommendations": recommendations,
        "family_search_plan": report.get("family_search_plan", []),
        "cross_item_vendors": cross,
        "disqualified": disqualified,
        "dashboard": {
            "tier_distribution": tiers,
            "score_component_averages": {k: _avg(v) for k, v in comp.items()},
            "positions_total": len(report["positions"]),
            "positions_without_vendors": without,
            "cross_item_vendors_total": len(cross),
            "cross_item_vendors_multi": sum(1 for v in cross if v["items_covered"] >= 2),
            "risk_flag_counts": flags,
            "missing_cache_families": missing,
        },
        "warnings": warnings,
    }


class ScoringService:
    def __init__(self, settings: Settings, paths: AppPaths, rfq: RFQService, mirror: PocketBaseMirror):
        self.settings = settings
        self.paths = paths
        self.rfq = rfq
        self.mirror = mirror

    def _artifact(self, user: AuthUser, name: str) -> Path:
        return self.paths.user_dir(user.id) / name

    def score(self, user: AuthUser, req: ScoreRequest) -> dict[str, Any]:
        s = self.settings
        vendor_w, wv, wf, effective = resolve_weights(req, s)
        top_n = req.top_n or s.recommend_top_n

        required = req.required_certs if req.required_certs is not None else s.required_certs_list
        options = pipeline.ScoreOptions(
            top_n=top_n,
            required_certs=[c.strip() for c in required if c and c.strip()],
            min_years=req.min_years if req.min_years is not None else s.client_min_years,
            require_trade_assurance=_dflt(req.require_trade_assurance, s.client_require_trade_assurance),
            strict_moq=_dflt(req.strict_moq, s.client_strict_moq),
            strict_certs=_dflt(req.strict_certs, s.client_strict_certs),
            strict_min_price=_dflt(req.strict_min_price, s.strict_min_price),
            max_price=req.max_price,
            weights=vendor_w,
            weight_vendor_score=wv,
            weight_rfq_fit=wf,
        )

        built = self.rfq.build_plan(user, req)
        plan = built["plan"]
        try:
            run = pipeline.scrape_families(
                plan, self.rfq.cache.store,
                pages=1, stealth=False, max_products=1,
                api_key=None, from_cache=True,  # 0 credits by construction
            )
        except InputError as exc:  # unreachable in from_cache mode; defensive
            raise ApiError(400, "invalid_request", str(exc)) from exc

        positions = pipeline.score_positions(built["rfq_items"], plan, run.scraped_by_family, options)
        report = pipeline.build_report(
            positions,
            source_excel=built["source_name"],
            granularity=built["granularity"],
            total_products=len(built["tagged_df"]),
            total_families=len(plan),
            pages=1,
            stealth=False,
            options=options,
            credits_spent=0,
            family_plan=plan,
            scraped_counts=run.scraped_counts,
        )
        report["meta"]["rfq_file_id"] = req.rfq_file_id
        report["meta"]["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        report["meta"]["effective_weights"] = effective
        report["meta"]["missing_cache_families"] = [
            o.family_id for o in run.outcomes if o.status == "missing"
        ]

        if req.persist_artifacts:
            pipeline.write_json_atomic(self._artifact(user, "final_recommendations.json"), report)
            pipeline.write_json_atomic(self._artifact(user, "family_search_plan.json"), plan)
            pipeline.export_final_excel(
                report, plan, run.scraped_counts, self._artifact(user, "final_recommendations.xlsx"), top_n
            )
            payload = report_to_payload(report)
            self.mirror.upsert(
                user, "runs", "kind", "latest",
                {"summary": {
                    "meta": report["meta"],
                    "dashboard": payload["dashboard"],
                }},
            )

        payload = report_to_payload(report)
        return {
            **payload,
            "effective_weights": effective,
            "credits_consumed": 0,
            "persisted": req.persist_artifacts,
        }

    def latest(self, user: AuthUser) -> dict[str, Any]:
        path = self._artifact(user, "final_recommendations.json")
        if not path.is_file():
            return {"exists": False, "result": None}
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            return {"exists": True, "result": report_to_payload(report)}
        except (OSError, ValueError, KeyError) as exc:
            raise ApiError(500, "artifact_corrupt", "Stored recommendations are unreadable") from exc

    def export_path(self, user: AuthUser) -> Path:
        path = self._artifact(user, "final_recommendations.xlsx")
        if not path.is_file():
            raise ApiError(404, "export_not_found", "No exported recommendations yet; run /recommendations/score first")
        return path


def _dflt(value: Optional[bool], default: bool) -> bool:
    return default if value is None else value
