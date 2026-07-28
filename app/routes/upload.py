"""Upload Blueprint – handles multi-file Excel ingestion."""
import re
import shutil
import uuid
from pathlib import Path

from flask import Blueprint, request, jsonify, current_app

from app.models.store import (
    save_dataset,
    delete_dataset,
    dataset_exists,
    derive_dataset_id,
    get_dataset_meta,
)
from app.utils.parser import parse_ems_files

upload_bp = Blueprint("upload", __name__)

ALLOWED = {"xlsx", "xls"}

_BATCH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DATASET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_ROLES = ("net", "gross_booking", "host")


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED


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


def _batch_dir(batch_id: str) -> Path:
    return Path(current_app.config["UPLOAD_FOLDER"]) / batch_id


def _save_batch_file(file, batch_dir: Path, role: str) -> Path:
    """
    Saves under a role-prefixed name (net__<original>, gross_booking__<original>,
    host__<original>) so /upload/confirm can re-locate each file unambiguously
    by role, regardless of whether the role was originally detected by
    filename keyword or the positional fallback below — a plain directory
    listing doesn't preserve original upload order, so re-deriving roles from
    filenames alone at confirm time could silently disagree with what was
    already previewed.
    """
    batch_dir.mkdir(parents=True, exist_ok=True)
    name = file.filename.replace(" ", "_")
    path = batch_dir / f"{role}__{name}"
    file.save(str(path))
    return path


def _locate_batch_files(batch_dir: Path) -> dict:
    """Re-locates net/gross_booking/host files saved by _save_batch_file()."""
    found = {}
    for role in _ROLES:
        matches = list(batch_dir.glob(f"{role}__*"))
        if matches:
            found[role] = matches[0]
    return found


@upload_bp.route("/files", methods=["POST"])
def upload_files():
    """
    Accepts 1–3 files via multipart/form-data key 'files'.
    Auto-detects each file's role from its filename, saves them under a new
    batch folder, and parses them for a preview — does NOT persist to the
    store. Call POST /upload/confirm with the returned batch_id to save.
    """
    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify({"error": "No files received."}), 400

    batch_id = uuid.uuid4().hex
    batch_dir = _batch_dir(batch_id)

    roles_by_filename: dict[str, str] = {}
    unknown_files = []
    skipped_extension = []
    for f in uploaded:
        if not f.filename:
            continue
        if not _allowed(f.filename):
            skipped_extension.append(f.filename)
            continue
        role = _detect_role(f.filename)
        if role == "unknown":
            unknown_files.append(f)
        else:
            roles_by_filename[f.filename] = role

    if skipped_extension and not roles_by_filename and not unknown_files:
        return jsonify({
            "error": "; ".join(f"'{n}' is not an Excel file – skipped." for n in skipped_extension),
        }), 400

    # Positional fallback: if we ended up with exactly 3 files and none (or
    # not all) were recognized by keyword, assign the remaining roles in
    # submission order.
    detected_roles = set(roles_by_filename.values())
    missing_roles = [r for r in _ROLES if r not in detected_roles]
    if unknown_files and len(roles_by_filename) + len(unknown_files) == 3 and len(missing_roles) == len(unknown_files):
        for f, role in zip(unknown_files, missing_roles):
            roles_by_filename[f.filename] = role
        unknown_files = []

    missing = [r for r in _ROLES if r not in set(roles_by_filename.values())]
    if missing or unknown_files:
        detail = (
            f"Could not identify file role(s): {missing}. "
            "Please ensure filenames contain 'Net', 'Gross', and 'Host'."
        )
        if skipped_extension:
            detail += " Skipped non-Excel file(s): " + ", ".join(skipped_extension) + "."
        return jsonify({"error": detail}), 422

    saved_paths = {}
    for f in uploaded:
        if f.filename not in roles_by_filename:
            continue
        role = roles_by_filename[f.filename]
        saved_paths[role] = _save_batch_file(f, batch_dir, role)

    # ── Parse (preview only — not persisted) ────────────────────────────────
    try:
        dataset = parse_ems_files(
            net_path=saved_paths["net"],
            gross_path=saved_paths["gross_booking"],
            host_path=saved_paths["host"],
        )
    except Exception as exc:
        shutil.rmtree(batch_dir, ignore_errors=True)
        return jsonify({"error": f"Parse failed: {exc}"}), 500

    if dataset.validation.errors:
        shutil.rmtree(batch_dir, ignore_errors=True)
        return jsonify({
            "error": "Critical parse errors.",
            "details": dataset.validation.errors,
        }), 500

    detected_dataset_id = derive_dataset_id(dataset.reporting_period)
    key_exists = dataset_exists(detected_dataset_id)

    response = {
        "batch_id":           batch_id,
        "detected_dataset_id": detected_dataset_id,
        "reporting_period":   dataset.reporting_period,
        "row_count":          len(dataset.bookings),
        "key_exists":         key_exists,
        "warnings":           dataset.validation.warnings,
    }
    if key_exists:
        existing = get_dataset_meta(detected_dataset_id)
        response["existing"] = {
            "uploaded_at": existing["updated_at"],
            "row_count":   existing["row_count"],
        }

    return jsonify(response)


def _persist_parsed(dataset_id: str, dataset, source_files: list) -> None:
    """Shared persistence path for /upload/confirm and /upload/demo."""
    save_dataset(
        bookings=dataset.bookings,
        host_summary=dataset.host_summary,
        reporting_period=dataset.reporting_period,
        validation=dataset.validation,
        source_files=source_files,
        dataset_id=dataset_id,
    )


@upload_bp.route("/confirm", methods=["POST"])
def confirm_upload():
    """
    Re-parses the batch saved by /upload/files and persists it under the
    given (possibly user-edited) dataset_id, upserting if it already exists.
    """
    body = request.get_json(silent=True) or {}
    batch_id = body.get("batch_id", "")
    dataset_id = body.get("dataset_id", "")

    if not _BATCH_ID_RE.match(batch_id):
        return jsonify({"error": "Invalid batch_id."}), 400
    if not _DATASET_ID_RE.match(dataset_id):
        return jsonify({"error": "Invalid dataset_id. Use letters, numbers, '_' or '-' only."}), 400

    batch_dir = _batch_dir(batch_id)
    if not batch_dir.is_dir():
        return jsonify({"error": "Unknown or expired batch_id. Please re-upload your files."}), 404

    paths = _locate_batch_files(batch_dir)
    missing = [r for r in _ROLES if r not in paths]
    if missing:
        return jsonify({"error": f"Batch is missing file role(s): {missing}."}), 500

    try:
        dataset = parse_ems_files(
            net_path=paths["net"],
            gross_path=paths["gross_booking"],
            host_path=paths["host"],
        )
    except Exception as exc:
        return jsonify({"error": f"Parse failed: {exc}"}), 500

    if dataset.validation.errors:
        return jsonify({
            "error": "Critical parse errors.",
            "details": dataset.validation.errors,
        }), 500

    source_files = [p.name.split("__", 1)[1] for p in paths.values()]
    _persist_parsed(dataset_id, dataset, source_files)
    shutil.rmtree(batch_dir, ignore_errors=True)

    return jsonify({
        "status":           "ok",
        "dataset_id":        dataset_id,
        "rows_parsed":       dataset.validation.total_rows_parsed,
        "reporting_period":  dataset.reporting_period,
        "warnings":          dataset.validation.warnings,
        "redirect":          f"/dashboard?ds={dataset_id}",
    })


@upload_bp.route("/clear", methods=["POST"])
def clear_data():
    dataset_id = request.form.get("dataset_id") or request.args.get("dataset_id")
    if not dataset_id:
        return jsonify({"error": "dataset_id is required."}), 400
    if not dataset_exists(dataset_id):
        return jsonify({"error": f"Unknown dataset_id '{dataset_id}'."}), 404
    delete_dataset(dataset_id)
    return jsonify({"status": "cleared", "dataset_id": dataset_id})


def _find_demo_triplet(base_dir: Path):
    """
    Locates a complete Net/Gross/Host file triplet, all three from the SAME
    directory — never mixes files from different fiscal-year subfolders,
    which would silently merge unrelated years and either fail the
    reconciliation guard or (worse) pass it on coincidentally-aligned data.
    Checks base_dir itself first (flat /data layout), then its immediate
    subdirectories in reverse alphabetical order — so with FY24/FY25/FY26
    subfolders present, the most recent (FY26) wins.
    """
    candidates = [base_dir] + sorted(
        (p for p in base_dir.iterdir() if p.is_dir()), reverse=True
    )
    for d in candidates:
        net   = list(d.glob("*Net*Sales*Booking*.xlsx")) + list(d.glob("*net*.xlsx"))
        gross = list(d.glob("*Gross*Sales*Booking*.xlsx")) + list(d.glob("*gross*booking*.xlsx"))
        host  = list(d.glob("*Host*.xlsx")) + list(d.glob("*host*.xlsx"))
        if net and gross and host:
            return net[0], gross[0], host[0]
    return None


@upload_bp.route("/demo", methods=["POST"])
def load_demo():
    """Load the pre-shipped EMS files that ship with the app."""
    data_dir = Path(current_app.config["DATA_FOLDER"])
    triplet = _find_demo_triplet(data_dir)

    if triplet is None:
        return jsonify({"error": "Demo files not found in /data folder."}), 404

    net_path, gross_path, host_path = triplet

    try:
        dataset = parse_ems_files(
            net_path=net_path,
            gross_path=gross_path,
            host_path=host_path,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if dataset.validation.errors:
        return jsonify({
            "error": "Critical parse errors.",
            "details": dataset.validation.errors,
        }), 500

    dataset_id = derive_dataset_id(dataset.reporting_period)
    _persist_parsed(dataset_id, dataset, ["demo_net.xlsx", "demo_gross.xlsx", "demo_host.xlsx"])

    return jsonify({
        "status":           "ok",
        "dataset_id":       dataset_id,
        "rows_parsed":      dataset.validation.total_rows_parsed,
        "reporting_period": dataset.reporting_period,
        "redirect":         f"/dashboard?ds={dataset_id}",
    })
