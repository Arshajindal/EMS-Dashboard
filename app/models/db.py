"""
Raw SQLite plumbing for the dataset-keyed store.
WAL mode + busy_timeout are what make concurrent gunicorn workers safe to
share one file — see docs/Multi-Year_Comparison_Architecture_Guide.md §3.
"""
from __future__ import annotations
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    id                 TEXT PRIMARY KEY,
    reporting_period   TEXT NOT NULL,
    source_files_json  TEXT NOT NULL DEFAULT '[]',
    validation_json    TEXT NOT NULL DEFAULT '{}',
    row_count          INTEGER NOT NULL DEFAULT 0,
    bookings_blob      BLOB NOT NULL,
    host_summary_blob  BLOB NOT NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
"""


def get_connection(path: str) -> sqlite3.Connection:
    """Short-lived connection per call — no long-lived global connection."""
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str) -> None:
    """Create the datasets table and enable WAL mode if not already set up."""
    conn = get_connection(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
