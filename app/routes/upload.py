"""Upload Blueprint – handles multi-file Excel ingestion."""
import os
from pathlib import Path

from flask import Blueprint, request, jsonify, current_app

from app.models.store import set_store, clear_store
from app.utils.parser import parse_ems_files

upload_bp = Blueprint("upload", __name__)

ALLOWED = {"xlsx", "xls"}


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED


def _save(file, folder: str) -> str:
    name = file.filename.replace(" ", "_")
    path = os.path.join(folder, name)
    file.save(path)
    return path


def _detect_role(filename: str) -> str:
    """
    Guess file role from name so users don't need to label them.
    Returns 'net' | 'gross_booking' | 'host' | 'unknown'.
    """
    fn = filename.lower()
    if "net" in fn:
        return "net"
    if "host" in fn:
        return "host"
    if "gross" in fn:
        return "gross_booking"
    return "unknown"


@upload_bp.route("/files", methods=["POST"])
def upload_files():
    """
    Accepts 1–3 files via multipart/form-data key 'files'.
    Auto-detects each file's role from its filename.
    Returns JSON with status, validation summary, and redirect URL.
    """
    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify({"error": "No files received."}), 400

    folder = current_app.config["UPLOAD_FOLDER"]
    saved: dict[str, str] = {}
    errors = []

    for f in uploaded:
        if not f.filename:
            continue
        if not _allowed(f.filename):
            errors.append(f"'{f.filename}' is not an Excel file – skipped.")
            continue
        role = _detect_role(f.filename)
        path = _save(f, folder)
        saved[role] = path

    if errors and not saved:
        return jsonify({"error": "; ".join(errors)}), 400

    # ── Require all three roles ───────────────────────────────────────────────
    missing = [r for r in ("net", "gross_booking", "host") if r not in saved]
    if missing:
        # Try positional fallback if user uploaded exactly 3 files without
        # recognisable names
        all_files = request.files.getlist("files")
        valid_paths = [p for p in saved.values()]
        if len(valid_paths) == 3 and missing:
            roles = ["net", "gross_booking", "host"]
            saved = {roles[i]: list(saved.values())[i] for i in range(3)}
            missing = []

    if missing:
        return jsonify({
            "error": (
                f"Could not identify file role(s): {missing}. "
                "Please ensure filenames contain 'Net', 'Gross', and 'Host'."
            ),
            "saved": list(saved.keys()),
        }), 422

    # ── Parse ─────────────────────────────────────────────────────────────────
    try:
        dataset = parse_ems_files(
            net_path=saved["net"],
            gross_path=saved["gross_booking"],
            host_path=saved["host"],
        )
    except Exception as exc:
        return jsonify({"error": f"Parse failed: {exc}"}), 500

    if dataset.validation.errors:
        return jsonify({
            "error": "Critical parse errors.",
            "details": dataset.validation.errors,
        }), 500

    set_store(
        bookings=dataset.bookings,
        host_summary=dataset.host_summary,
        reporting_period=dataset.reporting_period,
        validation=dataset.validation,
        source_files=[os.path.basename(p) for p in saved.values()],
    )

    return jsonify({
        "status":           "ok",
        "rows_parsed":      dataset.validation.total_rows_parsed,
        "reporting_period": dataset.reporting_period,
        "warnings":         dataset.validation.warnings,
        "redirect":         "/dashboard",
    })


@upload_bp.route("/clear", methods=["POST"])
def clear_data():
    clear_store()
    return jsonify({"status": "cleared"})


@upload_bp.route("/demo", methods=["POST"])
def load_demo():
    """Load the pre-shipped EMS files that ship with the app."""
    data_dir = Path(current_app.config["DATA_FOLDER"])

    net_candidates   = list(data_dir.glob("*Net*Sales*Booking*.xlsx")) + \
                       list(data_dir.glob("*net*.xlsx"))
    gross_candidates = list(data_dir.glob("*Gross*Sales*Booking*.xlsx")) + \
                       list(data_dir.glob("*gross*booking*.xlsx"))
    host_candidates  = list(data_dir.glob("*Host*.xlsx")) + \
                       list(data_dir.glob("*host*.xlsx"))

    if not (net_candidates and gross_candidates and host_candidates):
        return jsonify({"error": "Demo files not found in /data folder."}), 404

    try:
        dataset = parse_ems_files(
            net_path=net_candidates[0],
            gross_path=gross_candidates[0],
            host_path=host_candidates[0],
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    set_store(
        bookings=dataset.bookings,
        host_summary=dataset.host_summary,
        reporting_period=dataset.reporting_period,
        validation=dataset.validation,
        source_files=["demo_net.xlsx", "demo_gross.xlsx", "demo_host.xlsx"],
    )

    return jsonify({
        "status":           "ok",
        "rows_parsed":      dataset.validation.total_rows_parsed,
        "reporting_period": dataset.reporting_period,
        "redirect":         "/dashboard",
    })
