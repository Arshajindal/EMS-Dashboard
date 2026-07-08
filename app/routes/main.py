"""Main Blueprint – HTML page routes."""
from flask import Blueprint, render_template, redirect, url_for
from app.models.store import is_loaded, get_store

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    if is_loaded():
        return redirect(url_for("main.dashboard"))
    return render_template("upload.html")


@main_bp.route("/dashboard")
def dashboard():
    if not is_loaded():
        return redirect(url_for("main.index"))
    store = get_store()
    return render_template(
        "dashboard.html",
        reporting_period=store.reporting_period,
        source_files=store.source_files,
    )


@main_bp.route("/upload-page")
def upload_page():
    return render_template("upload.html")
