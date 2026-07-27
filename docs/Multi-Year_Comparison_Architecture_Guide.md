# Multi-Year Comparison — Architecture & Implementation Guide

**For:** SkySong EMS Analytics Dashboard (`03 SkySong_Dashboard`)
**Requirement:** Let users open and compare 2–4 fiscal years concurrently within one browser/user session, side-by-side, without state bleed or conflicts.
**Companion to:** `Technical_Report.md` (references §5.7, §7.1, §7.2 throughout).

---

## 0. The Core Constraint (read this first)

Every approach below assumes one prerequisite, because without it *all* of them leak state 100% of the time:

> The current backend holds a **single process-global `DataStore`** (`app/models/store.py`), replaced wholesale on every upload, and gunicorn is pinned to `--workers 1` *because* of that singleton (§5.7). The server can currently answer for exactly **one** dataset at a time.

So the real isolation boundary is **not** the browser tab, the iframe, or a web worker — it is a **dataset key on the backend**. The frontend techniques in §1 only decide *how many views you render* and *where client state lives*. They cannot manufacture backend isolation that doesn't exist.

**The reframe that makes this easy:** don't model "concurrent live sessions." Model **persisted, immutable datasets keyed by fiscal year**, retrieved read-only. Upload FY23 once → parse → persist under key `FY23`. Upload FY24 once → persist under `FY24`. A comparison view then just *reads* `FY23` and `FY24` by key. Nothing mutates during viewing, so nothing can bleed. This is your §7.2 items 1–2, and it is the single highest-leverage change.

---

## 1. State Management & Session Isolation

### 1.1 Where isolation actually has to happen

| Layer | Isolated today? | What's needed |
|---|---|---|
| **Backend dataset** | ❌ One global singleton | **Key data by fiscal year / dataset id** — the essential change |
| **Client JS state** (`dashData`, chart registry) | ⚠️ Per-tab only | Per-*panel* scoping if you render >1 dataset in one tab |
| **Browser storage** (`localStorage`) | ❌ Shared across all same-origin tabs | Use per-view keys or avoid it entirely (URL + in-memory) |

### 1.2 The backend change (non-negotiable prerequisite)

Replace the single `DataStore` with a **keyed registry** and thread every request through a dataset id:

- In-memory first step: `{dataset_id: DataStore}` instead of one global. Cheap, but still process-local and lost on restart, and it multiplies the single-worker problem rather than solving it.
- Correct target: **persist each parsed dataset** (SQLite recommended — see §3). Then `dataset_id` is just a lookup key, workers are stateless, restarts are survivable, and historical retention comes for free.
- Every `/api/*` endpoint takes a `dataset_id` (query param, path segment, or header). Your `_require_data()` gate becomes `_require_dataset(dataset_id)`.
- Upload returns a `dataset_id` (e.g. `FY24` or an opaque token) instead of clobbering the singleton. The client stores it — ideally **in the URL** so it's shareable, bookmarkable, and refresh-safe.

Once this exists, all three frontend patterns below become viable and *safe*. Before it exists, none are.

### 1.3 Evaluating the frontend patterns you asked about

**Tab-based isolation (unique dataset id in the URL)**
- Each tab loads `/dashboard?ds=FY23`, `/dashboard?ds=FY24`, etc. Client state is *already* naturally isolated — each tab is its own JS context, so your module-global `dashData` and chart registry don't collide across tabs.
- ✅ Lowest effort: no layout refactor, no per-panel state work.
- ⚠️ **The one footgun:** browser storage. `localStorage` is **shared across all same-origin tabs** — if you cache a dataset or filter state there, tabs *will* bleed. Use `sessionStorage` (per-tab) or keep state in the URL + in-memory only. Your current app already keeps `dashData` in a JS variable, which is safe; just don't "optimize" it into `localStorage`.

**In-app split panes (one tab, N panels)**
- One page, 2–4 panels, each panel fetches its own `dataset_id`. Best comparison UX (see §2), but the isolation risk **moves into your own code**.
- Your `dashboard.html` today uses a *single* global `dashData` and a `destroyAllCharts()` that nukes everything. For N panels you must refactor into **per-panel state objects** and **per-panel chart registries** — one shared global reused across panels is instant bleed.
- This forces the refactor already flagged in §7.2 item 5 (extracting/organizing the ~13 chart-render functions). Do that refactor *as* you add panels, not after.

**iframes / micro-frontends**
- Each iframe is its own browsing context with a **separate `window` and separate globals**, so client-side isolation of `dashData` is essentially free — load `/dashboard?ds=FY23` in one iframe and `?ds=FY24` in another.
- ⚠️ Cost: heaviest resource use (N full document boots, N Chart.js instances), and synced controls require `postMessage` plumbing across frames.
- **Micro-frontends (separately built/deployed apps) are disproportionate** for a single ~700-line dashboard. Don't. Plain same-origin iframes give you 90% of the isolation benefit without the build/deploy machinery.

**Web Workers / Service Workers — a clarification**
- **Web Workers isolate *computation*, not *data ownership*.** They give you a background thread with its own scope but no DOM; you `postMessage` data in and out. They do **not** provide multi-dataset backend isolation. And since your heavy lifting (pandas parsing/aggregation) is **server-side**, there's almost nothing to offload — the client just renders Chart.js. Web Workers are largely a **non-answer** to this requirement.
- **Service Workers are a shared cache/proxy, not an isolation boundary.** One Service Worker serves *all* tabs of the origin — it can't partition sessions. It *is* useful as a caching layer (intercept `/api/dashboard?ds=FY23`, serve from cache), but that's a §3 performance concern, not an isolation mechanism. Don't reach for it to solve state bleed.

---

## 2. UI/UX Approaches

### 2.1 First decide: juxtapose vs. superimpose

"Compare years" has two very different UX meanings, and the cheaper one is often *better*:

- **Superimpose** — plot FY23 and FY24 as two series on the *same* chart (monthly revenue, segment mix, discounts). For time-series and categorical charts this is the clearest possible comparison and is far cheaper than parallel dashboards. Recommend this as the **default** for the chart-heavy tabs.
- **Juxtapose** — render two-to-four full dashboards side by side. Necessary for things that don't overlay well (the Bookings table, the Data Quality report, the heatmap). Use this **selectively**, not for everything.

A strong product answer offers both: superimposed series on the analytical charts, plus a side-by-side split for the tabular/heatmap views.

### 2.2 Layout patterns for 2–4 datasets

- **2 datasets:** vertical split, 50/50 columns. The comfortable default on desktop.
- **3–4 datasets:** a responsive grid (2×2), or a "pinned primary + comparators" layout (one large panel + a rail of smaller ones). Four full dashboards side-by-side is unusable below ~1600px — prefer superimposed series here.
- **Independent config per panel:** each panel owns its dataset selector, filters, and tab position.
- **Sync toggle:** a global "sync controls" switch. When **on**, changing a filter/segment/time-range in one panel mirrors to all (small pub/sub over a shared state object). When **off**, panels are fully independent. This is the single most-valued feature in comparison UIs.
- **Synchronized scrolling** for the Bookings table so rows line up across panels.

### 2.3 Responsiveness when the screen shrinks

- **Reflow, don't cram:** below a breakpoint, side-by-side columns **stack vertically** (panels become full-width rows).
- **On narrow/mobile:** switch to a **tab/carousel switcher** — show one dataset at a time with a "FY23 | FY24 | FY25" toggle — because 3–4 parallel panels are unreadable at that width.
- **Superimposed charts win on small screens** — a single multi-series chart reflows gracefully where multiple panels can't. This is another reason to make superimpose the default.

---

## 3. Backend & API Design

### 3.1 Persist datasets keyed by fiscal year (the enabler)

Options, in order of fit for your scale (single internal tool, low traffic, a handful of years):

- **SQLite — recommended.** Zero-ops, restart-safe, persistent, keyed by fiscal year. Read-heavy / low-write, which is exactly SQLite's sweet spot. It simultaneously: (a) removes the `--workers 1` ceiling, (b) survives restarts, and (c) unlocks historical retention (§7.2 items 1–2). Store the parsed `bookings` / `host_summary` frames (e.g. one table per frame with a `dataset_id` column, or serialized parquet blobs keyed by id).
- **Postgres** — only if you later need genuine multi-user concurrent *writes*. Overkill today.
- **Redis** — good as a shared cache across workers, or as the store if you serialize DataFrames to parquet/pickle bytes. Adds an ops dependency; reach for it for caching (§3.3) before using it as the system of record.

### 3.2 API shape

- Add a **dataset key to every `/api/*` endpoint**: `GET /api/dashboard?ds=FY23`. Path form (`/api/FY23/dashboard`) works equally well — pick one and be consistent.
- Upload returns the key instead of mutating a singleton: `POST /upload/files → { dataset_id: "FY24" }`.
- Add a discovery endpoint: `GET /api/datasets → [{id, reporting_period, rows, uploaded_at}]` so the UI can populate its year selectors.
- Keep `build_full_dashboard()` exactly as-is — it's already a **pure function** over `(bookings, host_summary, reporting_period, validation)`. You just call it per `dataset_id` instead of over the singleton. This is the payoff of your existing pure-analytics design (§4.5).

### 3.3 Concurrency + caching for high-volume reads

Your realistic traffic is low (one operator), so don't over-engineer — but the requirement's "concurrent high-volume reads" is trivially satisfiable *because a stored year's payload is immutable*:

- **Server-side memoization:** cache `build_full_dashboard(dataset_id)` by key (in-process LRU on a single box; Redis if multi-worker). A stored year never changes, so the cache is effectively permanent until that year is re-uploaded — **invalidate by key on re-upload**, nothing else.
- **HTTP caching:** because per-dataset payloads are immutable, set a strong `ETag` / long `Cache-Control: max-age` on `/api/dashboard?ds=FY23`. Browsers (and any CDN) then serve repeats for free — this alone handles "high-volume concurrent reads" without touching your app.
- **Client-side IndexedDB** (not `localStorage`): cache fetched payloads **keyed by dataset id** so re-opening or re-comparing a year is instant and refetch-free. Use IndexedDB specifically because payloads are large structured objects — `localStorage` is ~5MB and string-only; IndexedDB handles large structured data and is the right tool here.
- **Then drop `--workers 1`** (§7.2 item 7). Once the store is external and reads are cache hits, multiple gunicorn workers scale reads linearly.

The pattern throughout: **immutable-per-key data → aggressive key-based caching with key-based invalidation.**

---

## 4. Top 3 Architectural Approaches — Comparison

> ⚠️ **All three assume §0/§1.2 is done** (backend keyed by dataset). "Risk of state leakage" below is rated *given a keyed backend*. Without it, all three are 100% leaky.

| Criterion | **Multi-Tab (URL-bound state)** | **In-App Split Panes** | **iFrame Isolation** |
|---|---|---|---|
| **Implementation complexity** | **Low** — read `ds` from URL; client state already per-tab; no layout refactor | **Medium–High** — must refactor the single global `dashData` + chart registry into per-panel state; build a layout + sync system | **Medium** — iframe per panel is cheap; true micro-frontend (separate builds) is High and disproportionate |
| **User experience** | **Moderate** — comparison via alt-tab / manual window tiling; no synced controls out of the box; fine for power users on big monitors | **Best** — purpose-built side-by-side, synced controls, superimpose *and* juxtapose in one cohesive app | **Moderate** — panels sit side-by-side, but synced controls need `postMessage`; scroll/focus quirks; feels less like one app |
| **Performance / resource overhead** | **Moderate** — N page loads, N Chart.js instances (CDN/fonts cached across tabs) | **Moderate–High** — N datasets + N×charts in **one** JS heap; single page/CDN load; must destroy charts on panel close | **Highest** — each iframe is a full document boot: N× app init, N× Chart.js, N DOM trees |
| **Risk of state leakage** | **Low** — separate JS contexts; safe **only if** you avoid shared `localStorage` (use `sessionStorage`/URL) | **Medium** — risk moves into *your* code: one shared global reused across panels = instant bleed; fully controllable with discipline | **Lowest (client-side)** — hard browsing-context boundary, separate `window`/globals; still needs keyed backend |

---

## 5. Recommended Path (concrete, phased)

**Phase 1 — Backend keying (mandatory foundation)**
1. Persist parsed datasets in **SQLite**, keyed by fiscal year (§3.1). Replace the singleton `DataStore` with keyed lookup; keep `build_full_dashboard()` untouched.
2. Add `ds` param to every `/api/*` endpoint; add `GET /api/datasets`; upload returns a `dataset_id` (§3.2).
3. Add key-based caching: `ETag`/`Cache-Control` on immutable payloads + in-process/Redis memoization (§3.3). Then remove `--workers 1`.

**Phase 2 — Comparison UX**
4. Make **superimposed multi-year series** the default on the analytical charts (cheaper *and* clearer). Use **split panes** for the Bookings table, Data Quality, and heatmap (§2.1).
5. Refactor `dashboard.html` from one global `dashData` into **per-panel state + per-panel chart registries** (this also discharges §7.2 item 5). Add a **sync-controls toggle** (§2.2).
6. Responsive rules: stack panels vertically on shrink; carousel switcher on mobile (§2.3).

**Phase 3 — Free wins**
7. Multi-tab URL-bound state works automatically once the backend is keyed — offer it to power users at ~zero extra cost.
8. Client-side **IndexedDB** cache keyed by dataset id for instant re-comparison (§3.3).

**Explicitly skip:**
- **Micro-frontends** — disproportionate for a 700-line dashboard.
- **Web Workers** — nothing meaningful to offload; processing is server-side.
- **Service Workers as an isolation mechanism** — they're a shared cache, not a boundary (optional later as a §3.3 cache layer only).
- **Storing dataset/filter state in `localStorage`** — the one reliable way to reintroduce cross-tab bleed.

---

## 6. Testing note (don't skip)

You currently have **no test suite** and an empty `tests/` dir (§7.1). Multi-dataset keying is exactly the kind of change that can silently reintroduce the 1.5×-inflation merge bug from §5.3 across datasets. Before shipping Phase 1, add regression tests that:
- load two different years and assert their totals are **independent** (no cross-key contamination),
- re-run the §4.3 reconciliation guard **per dataset**,
- confirm cache invalidation fires on re-upload of an existing key.

Your `/data` sample files are ready-made fixtures for this (§7.2 item 4).
