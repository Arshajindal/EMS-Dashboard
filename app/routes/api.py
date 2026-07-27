"""
API Blueprint – JSON endpoints consumed by the dashboard frontend.
Every route resolves a dataset_id (query param `ds`, falling back to the
most-recently-uploaded dataset) and requires that dataset to exist.
"""
import math
from flask import Blueprint, jsonify, request, current_app
from app.models.store import (
    get_dataset,
    dataset_exists,
    most_recent_dataset_id,
    list_datasets,
    get_dashboard_cache,
    set_dashboard_cache,
)
from app.utils.analytics import (
    build_full_dashboard,
    compute_kpis,
    monthly_revenue_trend,
    monthly_event_volume,
    segment_analysis,
    top_hosts,
    room_utilisation,
    booking_heatmap,
    weekday_summary,
    status_breakdown,
    payment_type_analysis,
    discount_analysis,
    quarterly_summary,
    duration_distribution,
    host_type_detail,
)

api_bp = Blueprint("api", __name__)


def _resolve_ds():
    return request.args.get("ds") or most_recent_dataset_id()


def _require_dataset(dataset_id):
    """Returns (dataset_dict, None) or (None, error_response)."""
    requested = request.args.get("ds")
    if not dataset_id:
        return None, (jsonify({"error": "No data loaded. Please upload files first."}), 404)
    if not dataset_exists(dataset_id):
        if requested:
            msg = f"Unknown dataset '{requested}'. Check /api/datasets for valid ids."
        else:
            msg = "No data loaded. Please upload files first."
        return None, (jsonify({"error": msg}), 404)
    return get_dataset(dataset_id), None


def _clean(obj):
    """Recursively replace NaN/Inf so jsonify doesn't choke."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _etag_for(ds):
    return f'"{ds["id"]}:{ds["updated_at"]}"'


def _cached_json(ds, compute_fn):
    """
    Wraps a per-dataset payload with ETag/Cache-Control. The ETag is derived
    from (dataset_id, updated_at), so a re-upload that UPSERTs the row bumps
    updated_at and naturally invalidates every cached response keyed on it —
    no separate cache-busting mechanism needed. compute_fn is only invoked on
    a cache miss (304 short-circuits before it's called).
    """
    etag = _etag_for(ds)
    if request.headers.get("If-None-Match") == etag:
        resp = current_app.response_class(status=304)
    else:
        resp = jsonify(_clean(compute_fn()))
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Full dashboard payload (single call to hydrate the whole page)
# ─────────────────────────────────────────────────────────────────────────────

@api_bp.route("/dashboard")
def api_dashboard():
    dataset_id = _resolve_ds()
    ds, err = _require_dataset(dataset_id)
    if err:
        return err

    etag = _etag_for(ds)
    if request.headers.get("If-None-Match") == etag:
        resp = current_app.response_class(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    payload = get_dashboard_cache(ds["id"])
    if payload is None:
        raw = build_full_dashboard(
            ds["bookings"],
            ds["host_summary"],
            ds["reporting_period"],
            ds["validation"],
        )
        payload = _clean(raw)
        set_dashboard_cache(ds["id"], payload)

    resp = jsonify(payload)
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Datasets meta
# ─────────────────────────────────────────────────────────────────────────────

@api_bp.route("/datasets")
def api_datasets():
    out = [
        {
            "id": d["id"],
            "reporting_period": d["reporting_period"],
            "rows": d["row_count"],
            "uploaded_at": d["updated_at"],
        }
        for d in list_datasets()
    ]
    return jsonify(_clean(out))


# ─────────────────────────────────────────────────────────────────────────────
# Individual chart endpoints (for lazy-loading / tab switching)
# ─────────────────────────────────────────────────────────────────────────────

@api_bp.route("/kpis")
def api_kpis():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: compute_kpis(ds["bookings"], ds["host_summary"]))


@api_bp.route("/monthly-revenue")
def api_monthly_revenue():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: monthly_revenue_trend(ds["bookings"]))


@api_bp.route("/monthly-volume")
def api_monthly_volume():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: monthly_event_volume(ds["bookings"]))


@api_bp.route("/segments")
def api_segments():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: segment_analysis(ds["bookings"], ds["host_summary"]))


@api_bp.route("/top-hosts")
def api_top_hosts():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    n = request.args.get("n", 15, type=int)
    sort_by = request.args.get("sort_by", "net", type=str)
    return _cached_json(ds, lambda: top_hosts(ds["bookings"], ds["host_summary"], n=n, sort_by=sort_by))


@api_bp.route("/rooms")
def api_rooms():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: room_utilisation(ds["bookings"]))


@api_bp.route("/heatmap")
def api_heatmap():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: booking_heatmap(ds["bookings"]))


@api_bp.route("/weekday")
def api_weekday():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: weekday_summary(ds["bookings"]))


@api_bp.route("/status")
def api_status():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: status_breakdown(ds["bookings"]))


@api_bp.route("/payment-types")
def api_payment_types():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: payment_type_analysis(ds["bookings"]))


@api_bp.route("/discounts")
def api_discounts():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: discount_analysis(ds["bookings"]))


@api_bp.route("/quarterly")
def api_quarterly():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: quarterly_summary(ds["bookings"]))


@api_bp.route("/duration")
def api_duration():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: duration_distribution(ds["bookings"]))


@api_bp.route("/host-types")
def api_host_types():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err
    return _cached_json(ds, lambda: host_type_detail(ds["bookings"], ds["host_summary"]))


# ─────────────────────────────────────────────────────────────────────────────
# Bookings table (paginated)
# ─────────────────────────────────────────────────────────────────────────────

@api_bp.route("/bookings")
def api_bookings():
    ds, err = _require_dataset(_resolve_ds())
    if err: return err

    page     = request.args.get("page",     1,    type=int)
    per_page = request.args.get("per_page", 50,   type=int)
    search   = request.args.get("q",        "",   type=str).lower()
    segment  = request.args.get("segment",  "",   type=str)
    status   = request.args.get("status",   "",   type=str)
    sort_by  = request.args.get("sort",     "start")
    order    = request.args.get("order",    "asc")

    def _compute():
        df = ds["bookings"].copy()

        # Filters
        if search:
            mask = (
                df["host"].str.lower().str.contains(search, na=False) |
                df["event_name"].str.lower().str.contains(search, na=False) |
                df["room"].str.lower().str.contains(search, na=False)
            )
            df = df[mask]

        if segment:
            df = df[df["segment"].str.lower() == segment.lower()]

        if status:
            df = df[df["status"].str.lower().str.contains(status.lower(), na=False)]

        # Sort
        if sort_by in df.columns:
            df = df.sort_values(sort_by, ascending=(order == "asc"))

        total = len(df)
        start = (page - 1) * per_page
        page_df = df.iloc[start : start + per_page]

        # Serialise datetimes
        cols = ["start", "end", "host", "event_name", "room", "status",
                "payment_type", "res_id", "Gross Sales", "Net Sales",
                "discount", "discount_pct", "segment", "duration_hrs"]
        cols = [c for c in cols if c in page_df.columns]
        rows = page_df[cols].copy()
        rows["start"] = rows["start"].dt.strftime("%Y-%m-%d %H:%M")
        rows["end"]   = rows["end"].dt.strftime("%Y-%m-%d %H:%M")

        return {
            "total":      total,
            "page":       page,
            "per_page":   per_page,
            "pages":      math.ceil(total / per_page),
            "rows":       rows.to_dict(orient="records"),
        }

    return _cached_json(ds, _compute)


# ─────────────────────────────────────────────────────────────────────────────
# Health / meta
# ─────────────────────────────────────────────────────────────────────────────

@api_bp.route("/health")
def health():
    datasets = list_datasets()
    return jsonify({
        "loaded":   bool(datasets),
        "datasets": _clean(datasets),
    })
