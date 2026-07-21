# SkySong EMS Analytics Dashboard

Flask + pandas monolith, no ORM, no build step.
Full architecture: docs/Technical_Report.md
Active migration: docs/Multi-Year_Comparison_Architecture_Guide.md
(Phase 1 landed: dataset-keyed SQLite store, app/models/store.py, replacing the
old global DataStore singleton. Phase 2 — comparison UI — not started.)

## Hard invariants — do not violate

- All dollar figures come from `bookings`, never `host_summary`. The Host file is a
  host -> host_type lookup plus setup_count/attendance only. Wiring
  host_summary['gross_sales'] into any chart reintroduces the double-counting bug
  fixed in commit 76ddbc4. (Report §5.4)
- Never merge Net/Gross on (res_id, start) alone — that pair is not unique. Always
  disambiguate with a per-group cumcount() occurrence index first, and never remove
  or weaken the reconciliation guard that re-sums both files and hard-errors on
  drift > $0.01. (Report §4.3, §5.3)
- analytics.py functions stay pure: DataFrames in, plain dict/list out, no I/O, no
  global state, _safe_div for every division.
- gunicorn is at --workers 2 (render.yaml), raised from 1 once the Phase 1 test
  suite and manual smoke verification passed. Don't bump it further without
  re-checking SQLite WAL write contention under real concurrent load first.
- No auth exists yet. Don't write code that assumes a logged-in user until auth is
  actually added.

## Current migration (multi-year comparison)

Phase 1 (backend dataset keying) is fully landed: app/models/store.py persists
datasets in SQLite (WAL mode, app/models/db.py), keyed by dataset_id derived
from reporting_period. Every /api/* route (except /health) resolves
`?ds=<dataset_id>`, falling back to the most-recently-uploaded dataset when
omitted — this is what lets dashboard.html keep working unmodified. Per-dataset
ETag/Cache-Control caching is on every chart/table endpoint, invalidated by key
on re-upload (see app/routes/api.py's _cached_json). tests/ has pytest coverage
including the §6 regression suite (tests/test_regression_section6.py) against
real /data fixtures. render.yaml is at --workers 2. Phase 2 (comparison UI) has
not started.
- build_full_dashboard() and analytics.py keep their current signatures — call
  them per dataset_id, don't change what they take or return.
- Never put dataset or filter state in localStorage in dashboard.html — it's shared
  across same-origin tabs and will bleed state across years. Use sessionStorage, the
  URL (?ds=), or in-memory JS state only.

## Commands

- Run locally: python run.py  ->  http://localhost:5000
- Install deps: pip install -r requirements.txt
- Install dev deps (adds pytest): pip install -r requirements-dev.txt
- Tests: pytest  (tests/ — store unit tests, multi-dataset API isolation,
  upload integration, §6 regression suite against real /data fixtures)

## Conventions

- Excel columns are located dynamically via BOOKING_FIELD_ALIASES / _build_field_map()
  — never hardcode a column index.
- New EMS export fields go in BOOKING_FIELD_ALIASES or HOST_FIELD_ALIASES in
  app/utils/parser.py, not into route or analytics code.