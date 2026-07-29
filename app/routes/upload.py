"""Upload Blueprint – handles multi-file, multi-year Excel ingestion."""
import re
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
from app.utils.parser import parse_ems_files, group_files_by_period, FileGroup

upload_bp = Blueprint("upload", __name__)

ALLOWED = {"xlsx", "xls"}

_BATCH_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DATASET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED


def _batch_dir(batch_id: str) -> Path:
    return Path(current_app.config["UPLOAD_FOLDER"]) / batch_id


def _save_uploaded_file(file, batch_dir: Path, index: int) -> Path:
    """
    Saves under an index-prefixed name (0000__<original>, 0001__<original>,
    ...) purely to guarantee on-disk uniqueness even if two uploaded files
    share a filename — role is no longer encoded in the filename at all,
    since grouping is 100% content-based (probe_file()) both now and on
    every later re-group, so nothing ever needs to trust a filename.
    """
    batch_dir.mkdir(parents=True, exist_ok=True)
    name = file.filename.replace(" ", "_")
    path = batch_dir / f"{index:04d}__{name}"
    file.save(str(path))
    return path


def _assign_group_ids(groups: dict) -> dict:
    """
    Deterministic group_id per VALID group, derived from its reporting
    period via derive_dataset_id() — the same slug logic already used for
    dataset_id derivation elsewhere. If two groups' periods happen to derive
    the same candidate id (distinct normalized periods, same extracted
    year), the later one (in stable sorted-key order) gets a numeric
    suffix. Sorted order + an unshrinking input set (see confirm_upload's
    docstring) is what makes this produce the same mapping on every call
    for the same batch, which /upload/confirm depends on.
    """
    ids = {}
    seen_counts = {}
    for key in sorted(groups.keys()):
        g = groups[key]
        if g.status != "valid":
            continue
        candidate = derive_dataset_id(g.period)
        n = seen_counts.get(candidate, 0)
        seen_counts[candidate] = n + 1
        ids[key] = candidate if n == 0 else f"{candidate}-{n}"
    return ids


def _ambiguous_detail(group: FileGroup) -> str:
    parts = []
    for role in group.collision_roles:
        count = group.role_counts.get(role, 0)
        label = f"{role}_sales" if role in ("net", "gross") else role
        parts.append(f"{count} files detected as {label}")
    if group.unrecognized_files:
        n = len(group.unrecognized_files)
        parts.append(f"{n} file{'s' if n != 1 else ''} could not be identified")
    return "; ".join(parts) if parts else "Ambiguous file grouping."


@upload_bp.route("/files", methods=["POST"])
def upload_files():
    """
    Accepts any number of files via multipart/form-data key 'files' —
    spanning any number of fiscal years, not just a fixed trio. Saves them
    all under a new batch folder, content-probes and groups them by fiscal
    year (parser.group_files_by_period), and returns a preview per group.
    Does NOT persist anything — call POST /upload/confirm with the
    batch_id and a specific group_id to save that group.
    """
    uploaded = request.files.getlist("files")
    if not uploaded:
        return jsonify({"error": "No files received."}), 400

    batch_id = uuid.uuid4().hex
    batch_dir = _batch_dir(batch_id)

    saved_paths = []
    skipped_extension = []
    index = 0
    for f in uploaded:
        if not f.filename:
            continue
        if not _allowed(f.filename):
            skipped_extension.append(f.filename)
            continue
        saved_paths.append(_save_uploaded_file(f, batch_dir, index))
        index += 1

    if not saved_paths:
        detail = "No valid Excel files received."
        if skipped_extension:
            detail = "; ".join(f"'{n}' is not an Excel file – skipped." for n in skipped_extension)
        return jsonify({"error": detail}), 400

    groups = group_files_by_period(saved_paths)
    group_ids = _assign_group_ids(groups)

    response_groups = []
    for key, g in groups.items():
        if g.status == "incomplete":
            response_groups.append({
                "status": "incomplete",
                "reporting_period": g.period,
                "missing_roles": g.missing_roles,
            })
            continue

        if g.status == "ambiguous":
            response_groups.append({
                "status": "ambiguous",
                "reporting_period": g.period,
                "detail": _ambiguous_detail(g),
            })
            continue

        # status == "valid": preview-parse it (same as today's single-year
        # flow) — this is not persisted, /upload/confirm re-parses.
        try:
            dataset = parse_ems_files(net_path=g.net_path, gross_path=g.gross_path, host_path=g.host_path)
        except Exception as exc:
            response_groups.append({
                "status": "error",
                "reporting_period": g.period,
                "detail": f"Parse failed: {exc}",
            })
            continue

        if dataset.validation.errors:
            # The file grouping itself was unambiguous (exactly one file per
            # role) — this is a data-content failure (e.g. the
            # reconciliation guard), not a file-selection ambiguity, so it
            # gets its own status rather than being folded into "ambiguous".
            response_groups.append({
                "status": "error",
                "reporting_period": g.period,
                "detail": "; ".join(dataset.validation.errors),
            })
            continue

        detected_dataset_id = derive_dataset_id(dataset.reporting_period)
        key_exists = dataset_exists(detected_dataset_id)
        entry = {
            "group_id":            group_ids[key],
            "status":              "ready",
            "detected_dataset_id": detected_dataset_id,
            "reporting_period":    dataset.reporting_period,
            "row_count":           len(dataset.bookings),
            "key_exists":          key_exists,
            "warnings":            dataset.validation.warnings,
        }
        if key_exists:
            existing = get_dataset_meta(detected_dataset_id)
            entry["existing"] = {
                "uploaded_at": existing["updated_at"],
                "row_count":   existing["row_count"],
            }
        response_groups.append(entry)

    for name in skipped_extension:
        response_groups.append({
            "status": "ambiguous",
            "reporting_period": "Unknown",
            "detail": f"'{name}' is not an Excel file – skipped.",
        })

    return jsonify({"batch_id": batch_id, "groups": response_groups})


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
    Re-groups the batch saved by /upload/files, re-parses the one group
    matching group_id, and persists it under dataset_id (the possibly
    user-edited label — kept distinct from group_id, which just identifies
    which file-trio in the batch to use), upserting if it already exists.

    Confirming one group must not require or affect any other group in the
    same batch: nothing is deleted from the batch folder here. If it were,
    and a later confirm() call for a *different* group in the same batch
    re-grouped over a now-smaller file set, _assign_group_ids()'s collision
    disambiguation could shift and produce a different group_id than what
    the original /upload/files preview showed — batch folder cleanup is
    intentionally out of scope for this pass (same accepted gap as
    already-abandoned, never-confirmed batches).
    """
    body = request.get_json(silent=True) or {}
    batch_id = body.get("batch_id", "")
    group_id = body.get("group_id", "")
    dataset_id = body.get("dataset_id", "")

    if not _BATCH_ID_RE.match(batch_id):
        return jsonify({"error": "Invalid batch_id."}), 400
    if not isinstance(group_id, str) or not group_id:
        return jsonify({"error": "group_id is required."}), 400
    if not _DATASET_ID_RE.match(dataset_id):
        return jsonify({"error": "Invalid dataset_id. Use letters, numbers, '_' or '-' only."}), 400

    batch_dir = _batch_dir(batch_id)
    if not batch_dir.is_dir():
        return jsonify({"error": "Unknown or expired batch_id. Please re-upload your files."}), 404

    all_paths = [p for p in batch_dir.iterdir() if p.is_file()]
    groups = group_files_by_period(all_paths)
    group_ids = _assign_group_ids(groups)

    matching_key = next((k for k, gid in group_ids.items() if gid == group_id), None)
    if matching_key is None:
        return jsonify({"error": f"Unknown group_id '{group_id}' for this batch."}), 404

    group = groups[matching_key]
    if group.status != "valid":
        return jsonify({"error": f"Group '{group_id}' is not ready to confirm."}), 400

    try:
        dataset = parse_ems_files(
            net_path=group.net_path,
            gross_path=group.gross_path,
            host_path=group.host_path,
        )
    except Exception as exc:
        return jsonify({"error": f"Parse failed: {exc}"}), 500

    if dataset.validation.errors:
        return jsonify({
            "error": "Critical parse errors.",
            "details": dataset.validation.errors,
        }), 500

    source_files = [p.name.split("__", 1)[1] for p in (group.net_path, group.gross_path, group.host_path)]
    _persist_parsed(dataset_id, dataset, source_files)

    return jsonify({
        "status":           "ok",
        "dataset_id":       dataset_id,
        "rows_parsed":      dataset.validation.total_rows_parsed,
        "reporting_period": dataset.reporting_period,
        "warnings":         dataset.validation.warnings,
        "redirect":         f"/dashboard?ds={dataset_id}",
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


def _pick_latest_group(groups: dict):
    """Prefers the valid group with the highest parseable year in its
    reporting period text; falls back to the first valid group found if
    none parse. Used only by /upload/demo, which has no user present to
    pick a group from a preview — it just loads the most recent year."""
    valid = [g for g in groups.values() if g.status == "valid"]
    if not valid:
        return None

    def year_key(g):
        years = re.findall(r"(20\d{2})", g.period)
        return max((int(y) for y in years), default=-1)

    return max(valid, key=year_key)


@upload_bp.route("/demo", methods=["POST"])
def load_demo():
    """
    Load the pre-shipped EMS files that ship with the app — the most recent
    fiscal year found anywhere under /data (which may hold several years,
    each in its own subfolder), detected the same content-based way a real
    upload is. Routed through the same group_files_by_period() +
    _persist_parsed() path /upload/confirm uses, just without the
    batch-folder/two-step preview dance, since there's no user choice to
    make — one click loads the latest year.
    """
    data_dir = Path(current_app.config["DATA_FOLDER"])
    all_files = [p for p in data_dir.rglob("*") if p.is_file() and _allowed(p.name)]
    if not all_files:
        return jsonify({"error": "Demo files not found in /data folder."}), 404

    groups = group_files_by_period(all_files)
    group = _pick_latest_group(groups)
    if group is None:
        return jsonify({"error": "Demo files not found in /data folder."}), 404

    try:
        dataset = parse_ems_files(
            net_path=group.net_path,
            gross_path=group.gross_path,
            host_path=group.host_path,
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if dataset.validation.errors:
        return jsonify({
            "error": "Critical parse errors.",
            "details": dataset.validation.errors,
        }), 500

    dataset_id = derive_dataset_id(dataset.reporting_period)
    _persist_parsed(
        dataset_id, dataset,
        [group.net_path.name, group.gross_path.name, group.host_path.name],
    )

    return jsonify({
        "status":           "ok",
        "dataset_id":       dataset_id,
        "rows_parsed":      dataset.validation.total_rows_parsed,
        "reporting_period": dataset.reporting_period,
        "redirect":         f"/dashboard?ds={dataset_id}",
    })
