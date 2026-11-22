# AGENTS.md — Alibaba B2B Sourcing Pipeline (FastAPI Backend)

This file defines the architecture, invariants, API contracts, and development workflow for OpenCode when building and maintaining the Python **FastAPI** backend for the **Alibaba B2B Sourcing & Vendor Recommendation Pipeline**.

---

## 1. Project Context & Mission

The repository currently contains a modular Python CLI pipeline (`main.py` + `scripts/`) that:
1. Ingests a client Request for Quotation (RFQ) Excel workbook (`products.xlsx`).
2. Dynamically clusters products into deduplicated supplier families locally (zero LLM tokens, 49% to 67% fewer API calls).
3. Scrapes Alibaba.com search results once per family via ScrapingBee, checkpointing results into a query-keyed JSON cache.
4. Scores, filters, and ranks vendors per line item and across items, exporting JSON and a 4-tab Excel deliverable.

**Your goal** is to build a clean, production-grade **FastAPI backend** that exposes this pipeline over HTTP (and Server-Sent Events for live scrape progress) to power the SvelteKit frontend and API clients—while strictly preserving credit safety, local clustering, and full backward compatibility with the existing CLI.

---

## 2. Non-Negotiable Engineering Invariants

1. **Zero LLM Calls for Categorization:** Never replace, bypass, or augment `scripts/dynamic_family_grouper.py` with external LLM API calls. Product family clustering must remain 100% local and deterministic.
2. **Strict ScrapingBee Credit Safety:**
   - Every operation that consumes ScrapingBee credits MUST require explicit opt-in (`confirm_credit_spend: true`) and must NEVER run implicitly on upload, page load, or re-score.
   - Dry-run grouping and cache-based re-scoring MUST always consume **0 credits**.
   - **Never call ScrapingBee in automated tests.** Always mock HTTP/scraper calls in `pytest`.
3. **Checkpoint & Cache Integrity:**
   - Persist scraped listings immediately after each family query to `output/family_scrape_cache.json` keyed by the normalized search query.
   - Use atomic file writes (write temp file + `os.replace`) guarded by a concurrency lock so parallel requests or interrupted jobs never corrupt the cache.
4. **Preserve CLI Compatibility:**
   - Do not break `python main.py templates/products.xlsx ...` or any standalone `scripts/*.py` CLI entry points.
   - Refactor shared logic into importable functions only when needed, keeping existing CLI flags and defaults intact.
5. **Secret Hygiene:**
   - Never log, return in API responses, or expose `SCRAPINGBEE_API_KEY` in error traces or `/config` payloads.

---

## 3. Target Repository Layout

Keep existing pipeline modules intact and add the FastAPI application under `backend/` (or `api/` if preferred—confirm existing repo root first):

```text
.
├── AGENTS.md                            # This guide
├── .env                                 # Secrets, proxy settings, default client rules & weights
├── main.py                              # Existing CLI orchestrator (MUST remain functional)
├── templates/products.xlsx              # Sample 100-item RFQ catalog (sheet: "products"); default RFQ_EXCEL_FILE
├── docker-compose.yml                   # pocketbase + backend (+ frontend placeholder profile)
├── pocketbase/                          # Dockerfile (pinned, checksummed binary) + pb_migrations/
├── output/                              # Shared runtime artifacts
│   ├── products_grouped.xlsx
│   ├── family_search_plan.json
│   ├── family_scrape_cache.json
│   ├── final_recommendations.xlsx
│   └── final_recommendations.json
├── debug_raw/                           # Raw ScrapingBee payloads on json-repair/parse failures
├── scripts/                             # Existing core domain modules
│   ├── pipeline.py                      # Importable orchestration shared by main.py and the backend (no argparse/sys.exit)
│   ├── pipeline_errors.py               # InputError/ConfigError raised by loaders (CLI converts to sys.exit)
│   ├── scrape_alibaba.py                # ScrapingBee client + AI extraction + normalization (scrape_term_detailed returns Spb-cost)
│   ├── recommend_vendors.py             # Deduplication, hard filters & 0-100 scoring
│   ├── process_rfq_excel.py             # RFQ parser, spec merger & RFQ fit scoring
│   └── dynamic_family_grouper.py        # Domain-agnostic 5-view clustering engine
└── backend/                             # NEW FastAPI application
    ├── requirements.txt                 # FastAPI + existing pipeline deps (or pyproject.toml)
    ├── app/
    │   ├── __init__.py
    │   ├── main.py                      # FastAPI app factory, lifespan, CORS, router mounting
    │   ├── core/
    │   │   ├── config.py                # pydantic-settings mapped to root .env
    │   │   ├── locks.py                 # Cache file lock & active scrape job guards
    │   │   └── paths.py                 # Safe path resolution for uploads/, output/, debug_raw/
    │   ├── schemas/
    │   │   ├── config.py                # Safe public defaults & weight models
    │   │   ├── rfq.py                   # RFQ item, upload summary, dry-run grouping models
    │   │   ├── jobs.py                  # Scrape job request, status, SSE progress event models
    │   │   ├── scoring.py               # Hard filters, scoring weights, 4-tab deliverable models
    │   │   └── cache.py                 # Cache entry inspection/eviction schemas
    │   ├── services/
    │   │   ├── pipeline_bridge.py       # Clean Python imports/adapters for scripts/*.py
    │   │   ├── rfq_service.py           # Excel upload validation & dry-run family grouping
    │   │   ├── scrape_job_service.py    # Background job manager, SSE queue, cancellation, Spb-cost tracking
    │   │   ├── scoring_service.py       # 0-credit offline re-scoring, dashboard stats, Excel export
    │   │   └── cache_service.py         # Atomic cache read/write/eviction utilities
    │   └── routers/
    │       ├── health.py                # /api/v1/health, /api/v1/config
    │       ├── rfq.py                   # /api/v1/rfq/*
    │       ├── jobs.py                  # /api/v1/jobs/*
    │       ├── recommendations.py       # /api/v1/recommendations/*
    │       └── cache.py                 # /api/v1/cache/*
    └── tests/
        ├── conftest.py                  # Mock ScrapingBee fixtures, temp output dirs, sample RFQ
        ├── test_health_config.py
        ├── test_rfq_grouping.py
        ├── test_scrape_jobs.py
        ├── test_scoring_and_export.py
        └── test_cache.py
```

---

## 4. Tech Stack & Runtime Rules

- **Python:** 3.11+
- **Web Framework:** `fastapi`, `uvicorn[standard]`, `python-multipart` (for `.xlsx` uploads)
- **Validation & Settings:** `pydantic>=2.0`, `pydantic-settings`
- **Streaming / Async Utilities:** `sse-starlette` (or native FastAPI `StreamingResponse` with `text/event-stream`), `anyio`
- **Existing Pipeline Dependencies (retain):** `requests`, `python-dotenv`, `json-repair`, `openpyxl`, `pandas`, `scikit-learn`
- **Testing & Linting:** `pytest`, `pytest-asyncio`, `httpx` (with `ASGITransport`), `ruff`

### Concurrency & Event Loop Rules
- `scripts/scrape_alibaba.py` uses synchronous `requests` with up to 90s timeout (`SCRAPINGBEE_TIMEOUT=90`) and `REQUEST_DELAY_SECONDS=1.0`.
- `scripts/dynamic_family_grouper.py` runs synchronous `scikit-learn` clustering, and `openpyxl` I/O is synchronous.
- **Rule:** Never execute synchronous scraping, clustering, or Excel generation directly inside `async def` route handlers. Always offload CPU/blocking I/O via `await anyio.to_thread.run_sync(...)` or a managed background worker task.
- **Direct Import Over Subprocess:** Prefer importing functions/classes directly from `scripts/` inside `services/pipeline_bridge.py` rather than spawning `subprocess` calls to `main.py`. Direct imports enable structured exceptions, per-family progress callbacks for SSE, graceful job cancellation, and fast in-memory re-scoring.

---

## 5. Configuration & Domain Defaults (`.env`)

Load configuration from the repository root `.env` using `pydantic-settings`. Mirror all existing keys and defaults:

| Env Variable | Default | Notes |
| --- | --- | --- |
| `SCRAPINGBEE_API_KEY` | *(secret)* | Required for live scrape only. Never expose via API. |
| `SCRAPINGBEE_URL` | `https://app.scrapingbee.com/api/v1/` | ScrapingBee endpoint |
| `ALIBABA_SEARCH_URL` | `https://www.alibaba.com/trade/search` | Base Alibaba search URL |
| `SCRAPINGBEE_WAIT_MS` | `3000` | JS render wait time |
| `SCRAPINGBEE_TIMEOUT` | `90` | Request timeout seconds |
| `SCRAPINGBEE_STEALTH_PROXY` | `false` | `false` = 30 credits/page; `true` = 80 credits/page |
| `MAX_PRODUCTS_PER_PAGE` | `15` | Organic listings cap per page (skips ads) |
| `DEFAULT_PAGES` | `1` | Pages scraped per family |
| `REQUEST_DELAY_SECONDS` | `1.0` | Throttle between requests |
| `DEBUG_DIR` | `debug_raw` | Raw response dump dir on parse/repair failures |
| `RFQ_EXCEL_FILE` | `products.xlsx` | Default RFQ workbook path |
| `FAMILY_GRANULARITY` | `balanced` | Allowed: `fine`, `balanced`, `broad` |
| `RECOMMEND_TOP_N` | `5` | Top vendors retained per line item |
| `CLIENT_REQUIRED_CERTS` | `""` | Comma-separated default certifications |
| `CLIENT_MIN_YEARS` | `0` | Minimum supplier years active |
| `CLIENT_REQUIRE_TRADE_ASSURANCE` | `false` | Hard filter toggle |
| `CLIENT_STRICT_MOQ` | `false` | Hard filter toggle |
| `CLIENT_STRICT_CERTS` | `false` | Hard filter toggle |
| `STRICT_MIN_PRICE` | `false` | Hard filter vs soft penalty below `min price` |
| `WEIGHT_PRICE` | `30` | Vendor score weight |
| `WEIGHT_MOQ` | `20` | Vendor score weight |
| `WEIGHT_TRADE_ASSURANCE` | `20` | Vendor score weight |
| `WEIGHT_YEARS` | `15` | Vendor score weight |
| `WEIGHT_CERTS` | `15` | Vendor score weight |
| `WEIGHT_VENDOR_SCORE` | `75` | Final blended score weight |
| `WEIGHT_RFQ_FIT` | `25` | Final blended score weight |
| `CORS_ORIGINS` | `http://localhost:5173` | Comma-separated allowed frontend origins |

---

## 6. Domain Logic & Scoring Invariants

When designing schemas and services, preserve these exact domain semantics from `scripts/`:

- **RFQ Columns:** `position | name | description | unit | qty | min price | max price | brand | technical specs` (must tolerate header whitespace/aliases such as `' max price'`).
- **Credit Cost Formula:**
  - Premium proxy (`stealth=false`): `30` credits per page (25 JS+premium + 5 AI extraction).
  - Stealth proxy (`stealth=true`): `80` credits per page (75 stealth + 5 AI extraction).
  - Estimated cost for a run = `uncached_family_count * pages * rate_per_page`.
  - Always report both total families, already-cached families, net uncached families to scrape, and actual credits consumed from the `Spb-cost` header.
- **Vendor Tiers:**
  - `Strong Recommend`: score `>= 75`
  - `Consider`: score `>= 55` and `< 75`
  - `Caution`: score `< 55`
- **Risk Flags & Warnings:**
  - **Bait price:** Listed price `< 25%` of group median (flagged, not rewarded rank #1).
  - **MOQ negotiable:** `MOQ > target_qty` and `MOQ <= 1.5 * target_qty` receives partial credit + warning unless `strict_moq=true`.
  - **Unit mismatch:** Flag discrepancy between RFQ normalized `unit` and supplier `moq_unit`.
  - **Brand matching:** Preserve flexible brand phrasing detection (e.g., `"Anker or similar"`) vs strict brand requirement.
- **4-Tab Deliverable Parity:** Both JSON responses and exported `final_recommendations.xlsx` must align across:
  1. `Recommendations`
  2. `Family_Search_Plan`
  3. `Cross_Item_Vendors`
  4. `Disqualified`

---

## 7. API Contract Specification (`/api/v1`)

All endpoints live under `/api/v1`, use snake_case JSON fields, and return consistent error payloads (`{"detail": ..., "code": ...}`).

### A. System & Configuration
- `GET /api/v1/health`
  - Returns `{ "status": "ok", "scrapingbee_configured": bool, "cache_entries": int, "default_rfq_exists": bool }`.
- `GET /api/v1/config`
  - Returns safe defaults for filters, weights, proxy mode, granularity, and credit rates (`30` / `80`). **Must never include `SCRAPINGBEE_API_KEY`.**

### B. RFQ Ingestion & Zero-Credit Dry-Run Grouping
- `POST /api/v1/rfq/upload`
  - Accepts `multipart/form-data` with `.xlsx` file (enforce extension and size limit, e.g. 20 MB; sanitize filename; store under `output/uploads/{upload_id}.xlsx`).
  - Parses workbook via `process_rfq_excel.py` and returns `upload_id`, `filename`, `row_count`, detected sheets, parsed warnings (e.g. malformed spec JSON repaired), and a row preview.
- `GET /api/v1/rfq/files`
  - Lists available RFQ workbooks (including default `products.xlsx` and uploaded files).
- `POST /api/v1/rfq/group` *(0 credits — Dry Run)*
  - Request body: `rfq_file_id` (or default), `granularity` (`fine` | `balanced` | `broad`), `limit` (optional int), `positions` (optional range string like `"1-10"`), `pages` (int, default 1), `stealth` (bool, default false).
  - Runs `dynamic_family_grouper.py`, writes `output/family_search_plan.json` (and optionally `output/products_grouped.xlsx`), checks `family_scrape_cache.json` for cache hits, and returns:
    - `total_items`, `family_count`, `api_reduction_pct`
    - `cached_families`, `uncached_families`
    - `estimated_credits_premium`, `estimated_credits_stealth`, `estimated_credits_selected`
    - `families`: list with `family_id`, `family_name`, `search_query`, `cohesion_score`, `positions`, `is_cached`, `cached_listing_count`.

### C. Live Scraping Jobs (Credit-Spending)
- `POST /api/v1/jobs/scrape`
  - Request body: `rfq_file_id`, `granularity`, `limit`, `positions`, `pages`, `stealth`, `max_products_per_page`, `force_refresh` (bool, default false), `family_queries` (optional subset list), and **`confirm_credit_spend: bool`** (MUST be `true`, otherwise return HTTP `400`).
  - Validates `SCRAPINGBEE_API_KEY` is configured (return HTTP `422`/`503` if missing).
  - Enqueues a background job, returns HTTP `202` with `job_id`, `status: "queued"`, and projected credit budget.
  - Prevents duplicate concurrent live scrape jobs from racing on the same queries.
- `GET /api/v1/jobs/{job_id}`
  - Returns job state (`queued` | `running` | `completed` | `cancelled` | `failed`), progress counts (`completed_families`, `total_families`, `cache_hits`, `live_fetches`), `credits_spent` (sum of `Spb-cost`), per-family logs, and error summary.
- `GET /api/v1/jobs/{job_id}/events`
  - Server-Sent Events (`text/event-stream`) emitting real-time `progress`, `family_completed`, `credit_update`, `warning`, and terminal `completed` / `failed` / `cancelled` events for the SvelteKit UI.
- `POST /api/v1/jobs/{job_id}/cancel`
  - Sets a cancellation flag checked between family queries. Already-scraped families remain saved in `output/family_scrape_cache.json`.

### D. Zero-Credit Scoring, Dashboard & Exports
- `POST /api/v1/recommendations/score` *(0 credits)*
  - Request body:
    - Target slice: `rfq_file_id`, `granularity`, `limit`, `positions`, `top_n`
    - Hard filters: `require_trade_assurance`, `min_years`, `max_price`, `strict_moq`, `strict_certs`, `strict_min_price`, `required_certs` (list of strings)
    - Weights: `weight_price`, `weight_moq`, `weight_trade_assurance`, `weight_years`, `weight_certs`, `weight_vendor_score`, `weight_rfq_fit`
    - `persist_artifacts` (bool, default `true`)
  - Validates that weight groups are non-negative and sum properly (or normalizes explicitly and returns effective weights).
  - Scores exclusively from `output/family_scrape_cache.json` (0 credits).
  - Optionally writes `output/final_recommendations.json` and `output/final_recommendations.xlsx`.
  - Returns the full 4-tab deliverable payload plus precomputed dashboard analytics (tier distribution counts, score component averages, missing-cache family warnings, cross-item vendor counts).
- `GET /api/v1/recommendations/latest`
  - Reads and returns `output/final_recommendations.json` + `output/family_search_plan.json` summary (returns `404` or empty-state payload cleanly if no run exists yet).
- `GET /api/v1/recommendations/export/xlsx`
  - Streams `output/final_recommendations.xlsx` as `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` with `Content-Disposition: attachment`.

### E. Cache & Debug Management
- `GET /api/v1/cache`
  - Lists cached normalized queries, listing counts, sample suppliers, and metadata from `output/family_scrape_cache.json`.
- `GET /api/v1/cache/{query_key}`
  - Returns cached normalized listings for a specific query.
- `DELETE /api/v1/cache/{query_key}`
  - Removes a single query entry from the cache atomically so only that family is re-scraped next run.
- `GET /api/v1/debug/files`
  - Lists filenames in `DEBUG_DIR` (`debug_raw/`) so operators can inspect truncated/repaired ScrapingBee payloads.

---

## 8. Error Handling & Security Checklist

- **Path Traversal Prevention:** Never accept raw filesystem paths from clients for RFQ files, cache keys, or debug filenames. Resolve against allowlisted directories (`output/uploads/`, repo root for `products.xlsx`, `debug_raw/`) and verify `.resolve().is_relative_to(...)`.
- **Upload Validation:** Only accept `.xlsx` files. Validate required workbook headers before persisting or returning success.
- **Malformed/Truncated Scrape Resilience:** Surface `json-repair` fallback counts and `debug_raw/` dump paths as non-fatal job warnings rather than crashing the entire batch job.
- **HTTP Status Discipline:**
  - `200` for reads/synchronous dry-runs/scoring
  - `202` for async scrape job creation
  - `400` for missing `confirm_credit_spend`, invalid weights, or invalid `.xlsx` schema
  - `404` for missing job/file/cache key
  - `409` if a conflicting live scrape job is already running
  - `422` for Pydantic schema validation errors

---

## 9. OpenCode Execution Plan

Follow this exact implementation order when building the backend:

1. **Inspect Existing Code First:**
   - Read `main.py`, `scripts/scrape_alibaba.py`, `scripts/recommend_vendors.py`, `scripts/process_rfq_excel.py`, `scripts/dynamic_family_grouper.py`, and `.env` (never print secret values) to match exact function signatures, data structures, and JSON output keys.
2. **Scaffold `backend/` Core:**
   - Create `backend/app/core/config.py`, `paths.py`, `locks.py`, and `backend/app/main.py` with CORS and `/api/v1/health` + `/api/v1/config`.
3. **Build Pipeline Bridge (`services/pipeline_bridge.py`):**
   - Wrap existing `scripts/` functions cleanly without breaking CLI execution. If minor refactors in `scripts/` are required (e.g., returning structured dicts or accepting an optional progress callback), keep all original defaults and CLI signatures untouched.
4. **Implement Zero-Credit Endpoints First:**
   - `/api/v1/rfq/upload`, `/api/v1/rfq/group`, `/api/v1/recommendations/score`, `/api/v1/recommendations/latest`, `/api/v1/recommendations/export/xlsx`, `/api/v1/cache`.
5. **Implement Async Scrape Job Engine & SSE:**
   - `/api/v1/jobs/scrape`, `/api/v1/jobs/{job_id}`, `/api/v1/jobs/{job_id}/events`, `/api/v1/jobs/{job_id}/cancel` with immediate per-family cache checkpointing and `Spb-cost` aggregation.
6. **Write Comprehensive Mocked Tests:**
   - Cover all endpoints, credit-confirmation guardrails, cache atomicity, cancellation, Excel 4-tab export verification, and secret redaction.
7. **Run Verification Suite:**
   - Verify linting, FastAPI test suite, OpenAPI schema generation, and CLI backward compatibility.

---

## 10. Verification & Run Commands

Run these checks before considering any task complete:

```bash
# 1. Lint & format check
ruff check backend/

# 2. Run full backend test suite (MUST make 0 live ScrapingBee calls)
pytest backend/tests -v

# 3. Verify CLI backward compatibility (0 credits)
python main.py templates/products.xlsx --limit 10 --dry-run

# 4. Start local FastAPI dev server
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

Verify interactive OpenAPI docs load cleanly at `http://localhost:8000/docs` and that `/api/v1/config` omits all API keys.

---

## 11. Do / Do Not Quick Reference

- **DO** keep all categorization local via `dynamic_family_grouper.py`.
- **DO** require `confirm_credit_spend: true` for any endpoint that triggers ScrapingBee.
- **DO** offload blocking `requests`, `scikit-learn`, and `openpyxl` calls off the async event loop.
- **DO** write tests using mocked ScrapingBee payloads and temporary directories (`tmp_path`).
- **DO NOT** run `main.py` without `--dry-run` or `--from-cache` during automated development/testing.
- **DO NOT** hardcode category rules, brand lists, or absolute local machine paths.
- **DO NOT** expose `SCRAPINGBEE_API_KEY` in logs, responses, or test snapshots.

---

## 12. Authentication, Persistence & Deployment (added)

- **Frontend lives in another repo** (SvelteKit). It signs users in/up **directly against PocketBase** (JS SDK) and sends `Authorization: Bearer <pb token>` to this API. FastAPI never handles passwords.
- **Auth dependency** (`core/auth.py`): validates the token via PocketBase `auth-refresh` (cached `AUTH_CACHE_TTL_SECONDS`, keyed by token hash). Every route except `/health` and `/config` requires it. `GET /api/v1/me` returns the caller.
- **Credit-spending routes (`/jobs/*`)** additionally require a **verified email** (`REQUIRE_VERIFIED_EMAIL`, needs PocketBase SMTP) plus `confirm_credit_spend: true`. Self-registration is open and there is no per-user quota, so optional global caps exist: `MAX_CREDITS_PER_JOB`, `MAX_CREDITS_PER_DAY`. One active live job per user; no two jobs may claim the same query (HTTP 409).
- **Ownership:** uploads, jobs and run artifacts belong to a user; non-owners get **404** (not 403). Artifacts live in `output/users/{user_id}/`, uploads in `output/uploads/`, job snapshots in `output/jobs/`. The **scrape cache is global and shared** (same query = same listings = credits saved). Cache eviction (`DELETE /cache/{key}`) and `GET /debug/files` are **admin-only** (`ADMIN_EMAILS`).
- **PocketBase mirror** (`rfq_files`, `scrape_jobs`, `runs`) is written with the *user's own token* for history/UI only. It is display data, never authoritative for credits or job state.
- **SSE auth:** `EventSource` cannot set headers; the frontend must use a fetch-based SSE client. `Last-Event-ID` resume is supported.
- **Run with exactly ONE uvicorn worker.** Job manager, job guard and cache locks are in-process. Jobs left `queued/running` after a crash are marked `failed` on startup.
- **Cache semantics:** only families whose pages all succeeded *and* returned >= 1 listing are checkpointed; failed/empty families are never cached (retried next run). Legacy raw-shaped listings (`moq_raw`, `supplier_url`) are normalized on read.
- **Scripts rules:** loaders raise `InputError`, never `sys.exit` (CLI entry points convert). `scrape_term` stays as the CLI-compatible wrapper over `scrape_term_detailed`. Scoring overrides (weights, global max price) are parameters of `evaluate_rfq_position`; never mutate module globals per request.
- **Deploy:** `cp .env.example .env && docker compose up -d --build`. Frontend is a placeholder behind `--profile frontend` (`FRONTEND_CONTEXT`). PocketBase binary is pinned and sha256-verified in `pocketbase/Dockerfile`.
- **Python:** target 3.11+; Docker image uses 3.12; dev venv is 3.14.

### Verification (updated)
```bash
ruff check backend/ main.py scripts/pipeline.py scripts/pipeline_errors.py
pytest backend/tests -v                       # mocked; a guard fixture fails on any live requests call
python main.py templates/products.xlsx --limit 10 --dry-run
docker compose config -q
```
