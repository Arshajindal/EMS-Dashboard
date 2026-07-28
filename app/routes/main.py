"""Main Blueprint – HTML page routes."""
import re
from flask import Blueprint, render_template, redirect, url_for, request
from app.models.store import list_datasets, most_recent_dataset_id, get_dataset_meta

main_bp = Blueprint("main", __name__)


def _fiscal_year_label(reporting_period: str) -> str:
    """Turn a raw 'Reporting Period: 7/1/2025 thru 6/30/2026' string into
    'Fiscal Year 2025-2026', driven entirely by whatever years appear in the
    source data. Falls back to the raw string if no year is found."""
    years = sorted(set(re.findall(r"\b(20\d{2})\b", reporting_period or "")))
    if len(years) >= 2:
        return f"Fiscal Year {years[0]}-{years[-1]}"
    if len(years) == 1:
        return f"Fiscal Year {years[0]}"
    return reporting_period


@main_bp.route("/")
def index():
    if list_datasets():
        return redirect(url_for("main.dashboard"))
    return render_template("upload.html")


@main_bp.route("/dashboard")
def dashboard():
    dataset_id = request.args.get("ds") or most_recent_dataset_id()
    meta = get_dataset_meta(dataset_id) if dataset_id else None
    if meta is None:
        return redirect(url_for("main.index"))
    return render_template(
        "dashboard.html",
        reporting_period=meta["reporting_period"],
        fiscal_year_label=_fiscal_year_label(meta["reporting_period"]),
        source_files=meta["source_files"],
    )


@main_bp.route("/upload-page")
def upload_page():
    return render_template("upload.html", has_datasets=bool(list_datasets()))
