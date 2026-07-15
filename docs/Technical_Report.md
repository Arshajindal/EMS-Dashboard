# SkySong EMS Analytics Dashboard — Technical Report

**Document type:** Engineering source of truth
**Audience:** Developers, maintainers, and technical stakeholders
**Repository:** `03 SkySong_Dashboard`
**Status:** Deployed (Render.com, single-instance, v1)

---

## 1. Executive Summary

The SkySong EMS Analytics Dashboard is a self-hosted Flask web application that converts three raw Excel exports from Arizona State University's EMS (Event Management System) booking platform into a single, interactive sales-analytics dashboard.

**Problem it solves:** EMS exports SkySong's booking and revenue data as three separate, structurally awkward spreadsheets — a Net Sales by Booking report, a Gross Sales by Booking report, and a Gross Sales by Host report. Each uses a merged-cell, multi-row-per-record layout designed for print, not analysis. Historically, understanding revenue trends, client segments, discounting behavior, and room utilization required manually cross-referencing all three files in Excel — a slow, error-prone, and non-repeatable process.

**What the system does:** A user uploads the three EMS exports through a browser. The backend parses the merged-cell layout into a clean, row-per-booking dataset, cross-validates the Net and Gross figures against each other, classifies every booking into a client segment, and derives fiscal-year/quarter groupings. The frontend then renders this as a six-tab dashboard — Revenue Trends, Clients & Segments, Operations, Discounts, a searchable Bookings table, and a Data Quality report — backed by a JSON API.

**Architecture in one sentence:** A single-process Flask monolith with no database — an uploaded dataset is parsed into pandas DataFrames held in a process-global, thread-lock-guarded in-memory store, and every chart on the dashboard is a pure function over that store, exposed through a JSON API and rendered client-side with Chart.js.

**Current scope and honest limitations:** This is a v1 internal tool built for a single analyst/operator workflow — there is no authentication, no persistent database, and no multi-user isolation (a new upload replaces the previous dataset for *everyone* currently viewing the app). These are documented, deliberate scope decisions for the current use case, not oversights, but they are the first things to revisit before broadening access (see §7).

---

## 2. System Architecture & Design

### 2.1 High-Level Architecture

The system is a **single-process, server-rendered monolith** with a JSON API layer for chart data. There is no separate frontend build, no database, and no microservices — the entire application is one Flask app deployed as one process.

```
                        ┌───────────────────────────────────────────┐
                        │              Browser (client)              │
                        │  upload.html / dashboard.html (Jinja2 SSR) │
                        │  Vanilla JS + Chart.js 4 (CDN)             │
                        └───────────────┬─────────────────────────────┘
                                        │  HTTP (multipart upload / fetch JSON)
                                        ▼
        ┌──────────────────────────────────────────────────────────────┐
        │                     Flask Application (gunicorn, 1 worker)     │
        │                                                                  │
        │  ┌────────────┐   ┌────────────┐   ┌────────────┐             │
        │  │  main_bp    │   │ upload_bp   │   │  api_bp     │            │
        │  │  (pages)    │   │ (/upload/*) │   │ (/api/*)    │            │
        │  └─────┬──────┘   └──────┬──────┘   └──────┬──────┘            │
        │        │                 │                  │                    │
        │        │           ┌─────▼──────┐           │                    │
        │        │           │  parser.py  │           │                    │
        │        │           │ (Excel →    │           │                    │
        │        │           │  DataFrame) │           │                    │
        │        │           └─────┬──────┘           │                    │
        │        │                 │                  │                    │
        │        │           ┌─────▼──────────────────▼──────┐            │
        │        └──────────▶│      models/store.py            │            │
        │                    │  process-global DataStore        │            │
        │                    │  (thread-lock guarded singleton) │            │
        │                    └─────┬──────────────────────────┘            │
        │                          │                                       │
        │                    ┌─────▼──────┐                                │
        │                    │analytics.py │                                │
        │                    │ (KPIs/chart  │                               │
        │                    │  aggregation)│                               │
        │                    └────────────┘                                │
        └──────────────────────────────────────────────────────────────┘
                    │                                    │
                    ▼                                    ▼
            /uploads (disk, raw Excel)            /data (bundled demo Excel)
```

### 2.2 Component Breakdown

**Frontend** — no build step, no bundler, no framework. Three Jinja2 templates:
- `templates/base.html` — page shell: CSS custom-property theme system (light/dark), top navbar, toast notifications, a global loading overlay, and the `themechange` custom-event plumbing used to re-theme charts.
- `templates/upload.html` — drag-and-drop file picker with client-side filename-based role preview (purely cosmetic; the server re-detects roles independently) and a "Load Demo Data" shortcut.
- `templates/dashboard.html` — the dashboard itself: KPI card row, a six-tab interface, and ~700 lines of inline JavaScript that fetches `/api/dashboard` once and renders all charts from that single payload using Chart.js 4 + the `chartjs-chart-matrix` plugin (for the booking heatmap).

**Backend** — a Flask application factory (`app/__init__.py`) registering three Blueprints:
- `app/routes/main.py` — HTML page routes (`/`, `/dashboard`, `/upload-page`). Also derives a human-readable fiscal-year label from the raw "Reporting Period" string extracted from the source file.
- `app/routes/upload.py` — accepts the multipart file upload, auto-detects each file's role from its filename, invokes the parser, and populates the global store.
- `app/routes/api.py` — the JSON API consumed by `dashboard.html`. One aggregate endpoint (`/api/dashboard`) plus granular per-chart endpoints (`/api/kpis`, `/api/top-hosts`, `/api/discounts`, etc.) for potential lazy-loading, a paginated/filterable/sortable `/api/bookings` endpoint, and `/api/health` for liveness checks.

**"Database"** — there isn't one. `app/models/store.py` defines a single process-global `DataStore` dataclass (bookings DataFrame, host_summary DataFrame, reporting period, validation report, source filenames), replaced wholesale on every successful upload and guarded by a `threading.Lock`. Raw uploaded Excel files are also written to `/uploads` on disk, but are never read back — they exist only as an audit trail for the current process lifetime (ephemeral on Render's filesystem; lost on redeploy/restart).

**External integrations** — none beyond CDN-hosted static assets: Chart.js, the Chart.js matrix plugin, and Google Fonts (Inter). No third-party APIs, no authentication provider, no email/notification service, no analytics/tracking.

### 2.3 Data Flow

```
1. User uploads 3 Excel files  ──▶  POST /upload/files
2. Each file's role (net / gross_booking / host) is guessed from its filename
   ("net" / "gross" / "host" substring match), with a positional fallback
   if exactly 3 files were sent but role detection failed.
3. Files saved to /uploads on disk.
4. parse_ems_files(net_path, gross_path, host_path) is invoked:
     a. Each Excel sheet is read with header=None (no fixed header row).
     b. _build_field_map() scans the header block for label text to locate
        every column dynamically (see §4.1).
     c. _parse_booking_sheet() runs a row-state machine that reassembles
        merged-cell, multi-row booking records into one row per booking
        (see §4.2), independently for the Net file and the Gross file.
     d. _cross_validate() compares Net vs Gross by Res ID and flags any
        row where Net > Gross (a sign of misclassified files).
     e. _merge_net_and_gross() joins the Net and Gross frames into one
        bookings table, 1:1, with a reconciliation guard (see §4.3 and
        §5.3 — this is the most important piece of logic in the system).
     f. discount / discount_pct / segment / segment_source / fiscal_year
        columns are derived.
5. The resulting ParsedDataset (bookings DataFrame, host_summary DataFrame,
   ValidationReport, reporting_period string) replaces the global DataStore.
6. Browser is redirected to /dashboard.
7. dashboard.html's loadDashboard() calls GET /api/dashboard once.
8. build_full_dashboard() runs ~14 pure analytics functions over the
   bookings/host_summary DataFrames and returns one JSON payload.
9. Chart.js renders ~13 charts + 2 ranked tables client-side from that
   payload. The Bookings tab additionally calls GET /api/bookings on
   demand (search/filter/sort/paginate) rather than shipping all rows
   up front.
```

### 2.4 Key Data Structures

**`bookings` DataFrame** (one row per booking, the central table everything is computed from):

| Column | Type | Notes |
|---|---|---|
| `start`, `end` | datetime64 | Parsed from the EMS export |
| `duration_hrs` | float | `(end - start)` in hours |
| `host` | str | Client/organization name, exactly as it appears in the source file |
| `event_name`, `room` | str | Room is recovered from a continuation row (see §4.2) |
| `payment_type`, `status` | str | Free-text from EMS |
| `res_id`, `book_id` | str | Reservation ID (not guaranteed unique — see §5.3) and Book ID |
| `Net Sales`, `Gross Sales` | float | Never fabricated; missing values become `0.0` and are logged |
| `discount`, `discount_pct` | float | Derived: `Gross - Net`, clipped at 0 |
| `segment`, `segment_source` | str | Client segment + which resolution tier produced it (`override` / `host_file` / `heuristic`) |
| `fiscal_year` | str | ASU fiscal year (`FYxx`, July–June) |
| `month`, `month_label`, `weekday`, `hour`, `date`, `year`, `quarter` | — | Pre-computed grouping keys used throughout `analytics.py` |

**`host_summary` DataFrame** (from the Host file — used *only* as a name→type lookup, never as a source of dollar totals; see §5.4 for why):

| Column | Type |
|---|---|
| `host_type` | str |
| `host` | str |
| `setup_count` | int |
| `attendance` | int |
| `gross_sales` | float |

**`ValidationReport`** — counts of rows read/parsed/skipped, zero-sales bookings, duplicate Res IDs, Net>Gross mismatches, plus free-text `warnings`/`errors` lists surfaced verbatim in the dashboard's "Data Quality" tab.

---

## 3. Technical Stack & Decisions

| Layer | Technology | Why this, not an alternative |
|---|---|---|
| Backend framework | **Flask 3** | The app has no auth, no ORM, no templating needs beyond Jinja2, and three logical route groups. Flask's Blueprint model gives that separation with near-zero framework overhead. Django's batteries (ORM, admin, auth) would be unused weight for a database-less, single-tenant tool. |
| Data processing | **pandas + numpy** | The entire domain problem — group by month/segment/host, sum/average revenue columns, pivot into a heatmap — is exactly what pandas' `groupby().agg()` and `pivot`/`unstack` are for. Hand-rolled loops over `openpyxl` rows would be slower to write, slower to run, and far more bug-prone for this class of aggregation. |
| Excel I/O | **openpyxl** (.xlsx) + **xlrd** (.xls) | Both are pandas' standard engines for their respective formats; needed because EMS can export either extension depending on version. |
| "Database" | **None — in-memory `DataStore` singleton** | The dataset is one fiscal year of bookings (tens of thousands of rows at most) for a single internal tool with one active operator. A real database would add migration/connection-pooling/ops overhead disproportionate to the need. This is an explicit, documented trade-off (see the docstring in `app/models/store.py`), not an oversight — and it is the first thing to change if the tool ever needs multi-user or historical persistence (§7). |
| Frontend | **Vanilla JS + Chart.js 4 (CDN), no bundler** | The UI is fundamentally "fetch one JSON payload, render N charts from it" with no client-side routing and modest state. A React/Vue + Vite pipeline would add build tooling, a `node_modules` tree, and a deploy step for a problem that ~700 lines of direct DOM/Chart.js calls solve adequately. Trade-off: some duplication across the ~13 chart-render functions in `dashboard.html` (flagged in §7 as a refactor candidate once chart count grows further). |
| Heatmap charting | **chartjs-chart-matrix** | The only well-maintained plugin that renders a true matrix/heatmap natively inside Chart.js, avoiding a second charting library just for one chart. |
| WSGI server | **gunicorn, `--workers 1`** | This is a *hard requirement*, not a tuning choice: the `DataStore` is a process-global Python object. Running more than one worker process would mean some requests are served by a worker that never received the uploaded data, silently showing an empty dashboard. Scaling workers requires externalizing the store first (§7). |
| Hosting | **Render.com** (`render.yaml` blueprint, free tier) | Zero-ops, git-push-to-deploy PaaS matching a low-traffic, single-organization internal tool. Avoids the manual VPC/instance/CI setup an AWS/GCP deployment would require for equivalent functionality. |
| CORS | **flask-cors (optional import)** | Wrapped in a `try/except ImportError` so the app still boots without it; enables `/api/*` to be called cross-origin if the frontend is ever split into its own deployment later. |
| Auth | **None (v1 scope)** | Deliberate scope decision — the deployed link is treated as an internal, trusted-audience tool. This is the single most important item to revisit before wider distribution (§7). |

---

## 4. Implementation Details

This section walks through how the four hardest pieces of the system were actually built, with real code excerpts (not simplified pseudocode) from the current codebase.

### 4.1 Dynamic Schema Resolution

EMS exports are not guaranteed to keep columns in the same position release-to-release, and their headers are split across several stacked rows rather than one clean header row. Hardcoding column indices would silently break the moment ASU's EMS export template changes. Instead, `app/utils/parser.py` scans the first N rows for label text and builds a `{canonical_field: column_index}` map at parse time:

```python
BOOKING_FIELD_ALIASES: dict[str, list[str]] = {
    "start":        ["start"],
    "end":          ["end"],
    "host":         ["host"],
    "event_name":   ["event name/location", "event name", "location"],
    "payment_type": ["payment type", "billing type"],
    "status":       ["booking status", "status"],
    "res_id":       ["res id", "reservation id"],
    "net_sales":    ["net sales", "net revenue", "net amount"],
    "gross_sales":  ["gross sales", "gross revenue", "gross amount"],
}

def _build_field_map(raw_df, field_aliases, header_rows=12) -> dict[str, int]:
    # Scan the header block, match longest aliases first, exact text
    # before substring, so a specific label like "host type" claims its
    # column before the generic "host" alias is even considered.
    ...
```

A caller may also pass a `schema_map` override (`{field: column_index_or_header_text}`) as a manual escape hatch for a file whose headers don't match the built-in aliases at all — the auto-detection is the default path, not the only path.

### 4.2 Merged-Cell Row Reassembly (Row-State Machine)

Each booking in the raw export spans **two or three physical spreadsheet rows** due to merged cells: a primary row with the booking's start/end/host/sales figure, followed by a continuation row holding the Book ID (in the Res ID column) and another holding the Room name (in the Event Name column). `_parse_booking_sheet` walks the sheet once, top to bottom, maintaining a single `pending` record that is flushed whenever a new event-start row or a total/page-break row is encountered:

```python
records, pending = [], {}
for _, row in raw_df.iterrows():
    if _is_total_row(row):
        if pending:
            records.append(pending); pending = {}
        continue

    if pd.notna(v_start) and pd.notna(v_end) and pd.notna(v_host):
        # New booking begins — flush whatever was pending, start fresh
        if pending:
            records.append(pending); pending = {}
        pending = {"start": start, "end": end, "host": ..., "room": "", ...}
        continue

    # Continuation row: fill in whichever field this row carries
    if pending:
        if pd.notna(v_resid) and pd.isna(v_event):
            pending["book_id"] = _clean_str(v_resid)
        if pd.notna(v_event) and pd.isna(v_start):
            pending["room"] = _clean_str(v_event)
```

This single linear pass turns an arbitrary-length, print-oriented spreadsheet into a clean one-row-per-booking DataFrame without any lookahead or recursive logic.

### 4.3 Net/Gross Merge with a Reconciliation Guard

This is the most consequential piece of logic in the codebase (see §5.3 for the incident it fixes). The Net file and Gross file are two independent exports of the *same* underlying bookings, and must be joined 1:1 on `(res_id, start)`. The problem: EMS reuses the same Res ID for recurring or multi-room bookings that share a start timestamp, so `(res_id, start)` is **not** a unique key — a naive `merge()` on that pair produces a many-to-many join that multiplies dollar totals for every duplicate-key group.

```python
merge_keys = ["res_id", "start"]
df_net["_occurrence"] = df_net.groupby(merge_keys).cumcount()
df_gross["_occurrence"] = df_gross.groupby(merge_keys).cumcount()
join_keys = merge_keys + ["_occurrence"]

bookings = df_net.merge(
    df_gross[join_keys + ["Gross Sales"]], on=join_keys, how="left"
)

# Reconciliation guard: re-sum both source files independently and
# compare against the merged totals. Any drift becomes a hard error
# instead of a silently wrong dashboard number.
pre_gross, post_gross = df_gross["Gross Sales"].sum(), bookings["Gross Sales"].sum()
if abs(post_gross - pre_gross) > 0.01:
    report.errors.append(
        f"Gross Sales total changed during the Net/Gross merge "
        f"(source={pre_gross:.2f}, merged={post_gross:.2f}). "
        "Refusing to trust these totals — investigate duplicate Res IDs."
    )
```

Disambiguating with a per-group `cumcount()` occurrence index before merging forces the join to be strictly 1:1 (the Nth Net row for a given key pairs with the Nth Gross row for that same key). The reconciliation guard is the safety net: it independently re-sums both source files and the merged result, and raises a hard `ValidationReport` error — which aborts the parse with a visible message rather than silently serving wrong totals — if they don't match to the cent.

### 4.4 Client Segmentation: Three-Tier Resolution

Every booking needs a `segment` (ASU / Government / SkySong Tenants / etc.) for the Clients & Segments tab and the discount breakdown. Revenue segmentation is resolved through a strict priority order, applied per-host:

```python
SEGMENT_OVERRIDES: dict[str, str] = {"ASU EDPLUS": "ASU EdPlus"}

def _resolve(host: str) -> tuple[str, str]:
    key = _normalize_host_key(host)
    if key in SEGMENT_OVERRIDES:
        return SEGMENT_OVERRIDES[key], "override"      # 1. explicit override
    if key in lookup:                                    # 2. Host-file ground truth
        return lookup[key], "host_file"
    return _classify_host_segment(host), "heuristic"    # 3. keyword fallback
```

1. **Explicit override** — a small hardcoded map for hosts that must always resolve a specific way regardless of source data (e.g. ASU EdPlus is a standalone peer entity, not a SkySong tenant, even though the Host file may group it otherwise).
2. **Host-summary file lookup** — the ground-truth source: the Host file's `host_type` column, matched by a whitespace/hyphen-normalized host name key (`_normalize_host_key`) so cosmetic formatting differences between files don't break the match.
3. **Keyword heuristic** — a fallback for any host that doesn't appear in the Host file at all (matches on substrings like `"SKYSONG"`, `"GOVERNMENT"`, `"HEALTH"`, etc.).

The match-rate between tiers 2 and 3 is logged as a validation warning (`"X/Y bookings (Z%) matched to a host_type from the Host summary file"`) so a user can gauge how much of the segmentation is authoritative versus guessed.

### 4.5 API Layer Pattern

Every `/api/*` endpoint (except `/health`) follows the same two-step pattern: refuse to compute anything if no dataset is loaded, and sanitize NaN/Infinity out of the response (pandas aggregations routinely produce `NaN` for empty groups, which `jsonify` cannot serialize):

```python
def _require_data():
    if not is_loaded():
        return jsonify({"error": "No data loaded. Please upload files first."}), 404
    return None

def _clean(obj):
    """Recursively replace NaN/Inf so jsonify doesn't choke."""
    if isinstance(obj, dict):  return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [_clean(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj

@api_bp.route("/dashboard")
def api_dashboard():
    if (err := _require_data()): return err
    store = get_store()
    payload = build_full_dashboard(store.bookings, store.host_summary,
                                    store.reporting_period, store.validation)
    return jsonify(_clean(payload))
```

`analytics.py` functions are all pure — they take DataFrames in, return plain dicts/lists out, guard every division by zero via `_safe_div`, and return an empty-shaped dict (`{"labels": [], ...}`) rather than raising when given an empty DataFrame, so the frontend never has to special-case "no data yet" beyond checking `is_loaded()`.

### 4.6 Frontend Rendering Pattern

`dashboard.html` fetches the entire payload once and fans it out to per-chart render functions, all keyed off Chart.js:

```javascript
async function loadDashboard() {
  const res  = await fetch('/api/dashboard');
  dashData   = await res.json();
  renderAll(dashData);
}
function renderAll(d) {
  renderKPIs(d.kpis);
  renderMonthlyRevenue(d.monthly_revenue);
  renderSegment(d.segment_analysis);
  renderTopHosts(d.top_hosts);
  // ...~10 more render*() calls, one per chart/table
}
```

Because Chart.js bakes colors into each chart instance at creation time, a light/dark theme toggle can't restyle existing charts in place. The fix is a destroy-and-rebuild pattern triggered by a custom DOM event dispatched from the theme toggle:

```javascript
document.addEventListener('themechange', () => {
  if (!dashData) return;
  destroyAllCharts();
  setChartDefaults();
  renderAll(dashData);   // re-render every chart from the already-fetched payload
});
```

No re-fetch is needed on theme change — `dashData` is cached client-side from the original `/api/dashboard` call.

---

## 5. Challenges, Blockers, & Resolutions

### 5.1 Print-oriented, merged-cell source format

**Challenge:** EMS exports are designed to be printed, not parsed — bookings span multiple physical rows via merged cells, headers are stacked across several rows, and the same sheet interleaves date-header rows, data rows, subtotal rows, and page-break rows with no consistent delimiter between them.

**Resolution:** A single-pass row-state machine (§4.2) that classifies each row by *which cells are populated* (a date-header row has a start value but no host; an event row has start+end+host; a continuation row has neither start nor end) rather than by row position, and accumulates a `pending` record until the next unambiguous event-start row.

### 5.2 Column layout not guaranteed stable

**Challenge:** Hardcoded column indices are brittle against any future change to the EMS export template — an added or reordered column would silently shift every downstream field.

**Resolution:** Dynamic, alias-based header scanning (§4.1) with an explicit manual override escape hatch (`schema_map`) for edge cases the built-in aliases can't resolve.

### 5.3 Revenue totals ~1.5x too high (the "data mismatch" incident)

**Challenge:** Early in development, the dashboard's Gross and Net Sales totals were reporting roughly 1.5x their correct value. The root cause: the Net and Gross booking files were joined with a plain `pandas.merge()` on `(res_id, start)`. That pair is **not unique** — EMS assigns the same reservation ID to recurring or multi-room bookings sharing a start timestamp. A pandas merge on a non-unique key performs a many-to-many join: every duplicate-key row on the left is cross-joined against every duplicate-key row on the right sharing that key, which multiplies (not merely duplicates) their dollar totals.

**Resolution:** Fixed in commit `76ddbc4` ("fix data mismatch issue") by disambiguating duplicate keys with a per-group `cumcount()` occurrence index before merging (§4.3), forcing every join to be strictly 1:1. Just as important: a **reconciliation guard** was added that independently re-sums both source files and the merged result and raises a hard `ValidationReport` error on any drift greater than one cent. This converts the entire *class* of future bug — any join or grouping mistake that silently inflates or deflates totals — from "silently wrong dashboard" into "loud, visible parse failure," which is a much cheaper failure mode to catch.

### 5.4 Two files with overlapping revenue meanings

**Challenge:** Both the booking-level files and the Host summary file contain a `gross_sales`-shaped figure, but they are scoped differently (the Host file's totals are not row-aligned with individual bookings). Using both as revenue sources anywhere risked double-counting or silent mismatches between charts.

**Resolution:** An explicit, documented rule enforced throughout `analytics.py`: **all dollar figures come from the booking-level `bookings` DataFrame, always.** The Host summary file is used *exclusively* as a `host → host_type` lookup during parsing (§4.4) and, separately, as a source for two non-monetary figures that exist nowhere else (`setup_count`, `attendance`). This rule is stated in a comment at the top of every analytics function that touches `host_summary`, specifically so a future contributor doesn't "helpfully" wire in `host_summary['gross_sales']` somewhere and reintroduce a mismatch.

### 5.5 Uploader could swap the Net and Gross files

**Challenge:** Nothing prevents a user from selecting the wrong file for the wrong upload slot; a filename containing "net" doesn't guarantee the file's actual header holds a Net Sales column.

**Resolution:** The parser never trusts the caller's file-role label — it inspects the *detected* header content and self-corrects, swapping `df_net`/`df_gross` and logging a warning if the two are found to be reversed. If both files resolve to the *same* sales column, that's escalated to a hard error rather than a silent guess, since there's no way to know which one is actually correct.

### 5.6 Theme toggle didn't restyle existing charts

**Challenge:** Chart.js bakes its color options into each chart instance at construction time; simply changing CSS custom properties on a theme toggle left every already-rendered chart showing the old theme's colors (fixed in commit `dd96d92`, "fix chart tooltip contrast in light theme").

**Resolution:** The destroy-and-rebuild-from-cached-payload pattern described in §4.6 — no network round-trip needed, just a full Chart.js re-instantiation against the same in-memory `dashData`.

### 5.7 Single global store vs. concurrency

**Challenge:** An in-memory, process-global dataset is inherently incompatible with multiple gunicorn worker processes — a request could be served by a worker that never received the last upload.

**Resolution:** Accepted as a scoping decision for a single-operator internal tool rather than solved architecturally — `render.yaml` pins gunicorn to `--workers 1`, and the trade-off is documented directly in `app/models/store.py`'s docstring ("For multi-worker deployments, swap this out for Redis or SQLite"). This is flagged again in §7 as the top scalability item.

---

## 6. Deployment & Operations

### 6.1 Local Development Setup

Prerequisites: Python 3.12+ (the project pins `PYTHON_VERSION: 3.12.6` for Render; any modern Python 3 works locally).

```bash
git clone <repo-url>
cd "03 SkySong_Dashboard"

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# then edit .env and set a real SECRET_KEY

python run.py
# → http://localhost:5000
```

**Environment variables** (see `.env.example` and `app/__init__.py`):

| Variable | Required | Default | Notes |
|---|---|---|---|
| `SECRET_KEY` | **Yes**, outside debug mode | none | The app **refuses to boot** (`RuntimeError`) if unset and `FLASK_DEBUG` is not `true`. In debug mode it falls back to a random ephemeral key with a warning. |
| `FLASK_DEBUG` | No | `false` | `true` enables Flask debug mode and the ephemeral-`SECRET_KEY` fallback. |
| `PORT` | No | `5000` | Used by `run.py`'s dev server; Render injects its own `$PORT` in production. |

**Dependencies** (`requirements.txt`): `flask>=3.0.0`, `flask-cors>=4.0.0` (optional at runtime), `pandas>=2.1.0`, `numpy>=1.26.0`, `openpyxl>=3.1.2`, `xlrd>=2.0.1`, `gunicorn>=21.2.0`.

**Sample data:** `/data` contains real EMS export files that can be loaded instantly via the "Load Demo Data" button on the upload page (`POST /upload/demo`) — useful for local development and demos without needing fresh EMS exports.

### 6.2 Production Deployment

Hosted on **Render.com** via the `render.yaml` blueprint checked into the repo root:

```yaml
services:
  - type: web
    name: skysong-dashboard
    env: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn "run:app" --workers 1 --bind 0.0.0.0:$PORT --timeout 120
    envVars:
      - key: PYTHON_VERSION
        value: 3.12.6
      - key: FLASK_DEBUG
        value: false
      - key: SECRET_KEY
        generateValue: true
```

- Deployment is git-based: Render rebuilds and redeploys automatically on pushes to the connected branch (standard Render PaaS behavior — there is **no separate CI/CD pipeline** in this repository; no `.github/workflows` exist as of this report).
- `SECRET_KEY` is generated once by Render and persisted as an environment variable, not committed to source.
- `--workers 1` is a hard architectural constraint, not a resource-tuning choice (§5.7) — do not increase it without first externalizing `DataStore`.
- The free-tier plan means the instance spins down on inactivity and cold-starts on the next request; expect a multi-second delay after idle periods.
- `/uploads` and `/data` are created at startup (`os.makedirs(..., exist_ok=True)`) on Render's ephemeral filesystem — files written there do **not** survive a redeploy or restart. This is inconsequential to correctness since parsed data lives only in the in-memory store, but it means the "audit trail" of raw uploaded files is not durable.

### 6.3 Monitoring, Logging, and Maintenance

- **Health check:** `GET /api/health` returns `{loaded, rows, reporting_period, source_files}` — a lightweight way to confirm the process is up *and* whether a dataset is currently loaded, without needing to open the dashboard.
- **Logging:** relies on gunicorn/Flask's default request logging to stdout (captured by Render's log viewer). There is no structured logging, no error tracking (e.g. Sentry), and no metrics/alerting integration currently wired in — flagged in §7.
- **Data-quality visibility:** every parse produces a `ValidationReport` (row counts, skipped rows, zero-sales bookings, duplicate Res IDs, Net>Gross mismatches, free-text warnings/errors) surfaced directly in the dashboard's "Data Quality" tab — this is the primary maintenance signal for whether an EMS export parsed cleanly.
- **Routine maintenance:** if ASU's EMS export template gains a new column or renames an existing header, no route or analytics code needs to change — add the new label string to `BOOKING_FIELD_ALIASES` or `HOST_FIELD_ALIASES` in `app/utils/parser.py` and the dynamic schema resolution (§4.1) picks it up automatically.

---

## 7. Future Work & Scalability

### 7.1 Known Limitations (Current State)

- **Single active dataset, system-wide.** The in-memory `DataStore` is a single global singleton — if two people upload at different times, the second upload silently replaces the first for *every* current viewer. There is no per-user or per-session isolation.
- **No persistence across restarts.** A Render redeploy, restart, or crash wipes the loaded dataset entirely; the dashboard reverts to the upload screen until someone re-uploads.
- **No historical retention.** Uploading a new fiscal year's files replaces the previous dataset outright — there is no way to compare across fiscal years or reporting periods within the app itself.
- **No authentication or authorization.** Anyone with the URL can view, upload, and overwrite the active dataset. Acceptable for a trusted internal link today; a real gap the moment the URL circulates more broadly.
- **Single gunicorn worker is a hard concurrency ceiling**, imposed by the architecture (§5.7), not tunable without other changes.
- **No automated test suite.** A `tests/` directory exists in the repo but is currently empty — there is no regression coverage for the parser, and specifically none guarding against a recurrence of the merge fan-out bug described in §5.3, which is exactly the kind of logic that regresses silently without tests.
- **Config/behavior inconsistency:** `app/__init__.py` lists `{"xlsx", "xls", "csv"}` as `ALLOWED_EXTENSIONS`, but `app/routes/upload.py` only actually accepts `{"xlsx", "xls"}` — CSV is advertised in config but rejected in practice. Worth reconciling (either drop CSV from config or add real CSV support).
- **No structured logging or error monitoring**, beyond the default request log and the `/api/health` endpoint (§6.3).

### 7.2 Recommendations for Scaling

1. **Externalize the data store.** Move from the in-process `DataStore` singleton to SQLite (simplest, still zero-ops) or Postgres (if true multi-user concurrent access is needed). This is the single highest-leverage change — it simultaneously removes the single-worker constraint, enables restart-safe persistence, and unlocks historical retention.
2. **Retain historical uploads.** Once persisted, key stored datasets by reporting period/fiscal year rather than always overwriting, enabling year-over-year and quarter-over-quarter comparison views that the current architecture cannot support at all.
3. **Add authentication** (ASU SSO or Google OAuth would be the natural fit for an ASU-internal tool) before distributing the link beyond a small trusted group.
4. **Build a regression test suite around `parser.py`.** The sample files already committed in `/data` are ready-made fixtures. Priority coverage: the merged-cell row-state machine (§4.2), the net/gross merge reconciliation guard (§4.3 — this is the function that has already caused one production-correctness incident), and the segment-resolution priority order (§4.4).
5. **Extract the inline dashboard JavaScript** out of `dashboard.html` into versioned static files once the chart count grows further, to reduce the duplication that's already visible across the ~13 chart-render functions — this doesn't require adopting a full framework, just moving `<script>` content into `static/js/`.
6. **Add structured logging and error monitoring** (e.g. Sentry) for real production visibility beyond the current `/api/health` liveness check.
7. **Move to multi-worker gunicorn** once the store is externalized, to raise the concurrent-request ceiling.
8. **Reconcile the CSV inconsistency** noted in §7.1 — either implement real CSV parsing support (EMS may support CSV export) or remove it from `ALLOWED_EXTENSIONS` so config matches behavior.

### 7.3 Future AI/ML Scope

These are exploratory directions, not committed roadmap items:

- **Anomaly detection** on monthly booking volume, revenue, or discount rates — automatically flagging statistically unusual spikes or drops rather than relying solely on the current human-authored validation-warning list.
- **Revenue forecasting** (e.g. simple time-series projection of monthly Gross/Net Sales) becomes viable once historical multi-fiscal-year data is retained (§7.2, item 2) — there isn't enough retained history today to forecast meaningfully.
- **NLP-assisted host segmentation** to reduce reliance on the hand-maintained keyword list in `_classify_host_segment` (§4.4, tier 3) — a lightweight text classifier trained on already-segmented host names (from the Host-file ground-truth tier) could shrink the fraction of bookings falling back to the keyword heuristic.
- **Natural-language querying** over the bookings table (e.g. "show top clients by discount rate last quarter") using the existing pandas DataFrame as a queryable backend — a plausible extension of the existing `/api/bookings` filter/sort/paginate endpoint rather than a new subsystem.

---

*End of report.*
