"""
Dataset-keyed store, backed by SQLite (see app/models/db.py).
Replaces the old process-global DataStore singleton so multiple fiscal
years can be retained and served concurrently. DataFrames are persisted as
Parquet blobs (pyarrow) inside a single `datasets` row per dataset_id.
"""
from __future__ import annotations
import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

import pandas as pd

from app.models.db import get_connection

_cache_lock = threading.Lock()
_dashboard_cache: dict[str, dict] = {}
_CACHE_MAX_KEYS = 8

_db_path: Optional[str] = None


def configure(path: str) -> None:
    """Set the SQLite file path this module reads/writes. Call once at app-factory time."""
    global _db_path
    _db_path = path


def _conn():
    if _db_path is None:
        raise RuntimeError("store.configure(path) must be called before use.")
    return get_connection(_db_path)


def _df_to_blob(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    df.to_parquet(buf)
    return buf.getvalue()


def _blob_to_df(blob: bytes) -> pd.DataFrame:
    return pd.read_parquet(BytesIO(blob))


def derive_dataset_id(reporting_period: str) -> str:
    years = sorted(set(re.findall(r"\b(20\d{2})\b", reporting_period or "")))
    if len(years) >= 2:
        return f"FY{years[0]}-{years[-1]}"
    if len(years) == 1:
        return f"FY{years[0]}"
    digest = hashlib.sha1((reporting_period or "").encode("utf-8")).hexdigest()[:10]
    return f"FY-{digest}"


def save_dataset(
    bookings: pd.DataFrame,
    host_summary: pd.DataFrame,
    reporting_period: str,
    validation,
    source_files: Optional[list] = None,
    dataset_id: Optional[str] = None,
) -> str:
    dataset_id = dataset_id or derive_dataset_id(reporting_period)
    validation_dict = validation.to_dict() if hasattr(validation, "to_dict") else validation
    now = datetime.now(timezone.utc).isoformat()

    conn = _conn()
    try:
        conn.execute(
            """
            INSERT INTO datasets
                (id, reporting_period, source_files_json, validation_json,
                 row_count, bookings_blob, host_summary_blob, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                reporting_period  = excluded.reporting_period,
                source_files_json = excluded.source_files_json,
                validation_json   = excluded.validation_json,
                row_count         = excluded.row_count,
                bookings_blob     = excluded.bookings_blob,
                host_summary_blob = excluded.host_summary_blob,
                updated_at        = excluded.updated_at
            """,
            (
                dataset_id,
                reporting_period,
                json.dumps(source_files or []),
                json.dumps(validation_dict),
                len(bookings),
                _df_to_blob(bookings),
                _df_to_blob(host_summary),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    invalidate_cache(dataset_id)
    return dataset_id


def get_dataset(dataset_id: str) -> Optional[dict]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    cols = [
        "id", "reporting_period", "source_files_json", "validation_json",
        "row_count", "bookings_blob", "host_summary_blob", "created_at", "updated_at",
    ]
    r = dict(zip(cols, row))
    return {
        "id": r["id"],
        "bookings": _blob_to_df(r["bookings_blob"]),
        "host_summary": _blob_to_df(r["host_summary_blob"]),
        "reporting_period": r["reporting_period"],
        "validation": json.loads(r["validation_json"]),
        "source_files": json.loads(r["source_files_json"]),
        "row_count": r["row_count"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def get_dataset_meta(dataset_id: str) -> Optional[dict]:
    conn = _conn()
    try:
        row = conn.execute(
            """
            SELECT id, reporting_period, source_files_json, validation_json,
                   row_count, created_at, updated_at
            FROM datasets WHERE id = ?
            """,
            (dataset_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    cols = ["id", "reporting_period", "source_files_json", "validation_json",
            "row_count", "created_at", "updated_at"]
    r = dict(zip(cols, row))
    return {
        "id": r["id"],
        "reporting_period": r["reporting_period"],
        "source_files": json.loads(r["source_files_json"]),
        "validation": json.loads(r["validation_json"]),
        "row_count": r["row_count"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def list_datasets() -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT id, reporting_period, source_files_json, validation_json,
                   row_count, created_at, updated_at
            FROM datasets ORDER BY updated_at DESC
            """
        ).fetchall()
    finally:
        conn.close()
    cols = ["id", "reporting_period", "source_files_json", "validation_json",
            "row_count", "created_at", "updated_at"]
    out = []
    for row in rows:
        r = dict(zip(cols, row))
        out.append({
            "id": r["id"],
            "reporting_period": r["reporting_period"],
            "source_files": json.loads(r["source_files_json"]),
            "validation": json.loads(r["validation_json"]),
            "row_count": r["row_count"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        })
    return out


def dataset_exists(dataset_id: str) -> bool:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM datasets WHERE id = ?", (dataset_id,)
        ).fetchone()
    finally:
        conn.close()
    return row is not None


def most_recent_dataset_id() -> Optional[str]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id FROM datasets ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def delete_dataset(dataset_id: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
        conn.commit()
    finally:
        conn.close()
    invalidate_cache(dataset_id)
    return cur.rowcount > 0


def get_dashboard_cache(dataset_id: str) -> Optional[dict]:
    with _cache_lock:
        return _dashboard_cache.get(dataset_id)


def set_dashboard_cache(dataset_id: str, payload: dict) -> None:
    with _cache_lock:
        _dashboard_cache[dataset_id] = payload
        if len(_dashboard_cache) > _CACHE_MAX_KEYS:
            oldest_key = next(iter(_dashboard_cache))
            del _dashboard_cache[oldest_key]


def invalidate_cache(dataset_id: str) -> None:
    with _cache_lock:
        _dashboard_cache.pop(dataset_id, None)
