"""
Excel export – combines the merged bookings dataset back into the two
sheets ("Net" and "Gross") the source EMS files originally shipped as,
each carrying a "Client Type" column.

Why this doesn't re-read the original Net/Gross/Host files: Phase 1 of the
multi-year migration (see docs/Multi-Year_Comparison_Architecture_Guide.md)
already persists the reconciled, merged `bookings` DataFrame per dataset —
Net Sales, Gross Sales, and the Host file's host_type (as `segment`, i.e.
"Client Type") all live on the same row, produced by parser.py's
_merge_net_and_gross / _apply_host_type with the reconciliation guard from
CLAUDE.md §4.3/§5.3 already applied. Re-deriving two raw sheets from that
one merged table, rather than re-joining the three source files a second
time, means this module can stay a light view over already-validated data
instead of a second implementation of the merge/lookup logic that could
drift from parser.py's.

Kept out of analytics.py deliberately: analytics.py's contract is "DataFrames
in, plain dict/list out, no I/O" (CLAUDE.md); building an .xlsx workbook is
an I/O-shaped concern and belongs in its own module.
"""
from __future__ import annotations

from io import BytesIO

import pandas as pd

# Columns common to both exported sheets, in display order, mapped from the
# internal bookings column name to the header written to Excel.
_COMMON_COLUMNS: list[tuple[str, str]] = [
    ("start", "Start"),
    ("end", "End"),
    ("duration_hrs", "Duration (hrs)"),
    ("host", "Host"),
    ("event_name", "Event Name"),
    ("room", "Room"),
    ("payment_type", "Payment Type"),
    ("status", "Status"),
    ("res_id", "Res ID"),
    ("book_id", "Book ID"),
    ("segment", "Client Type"),
    ("fiscal_year", "Fiscal Year"),
    ("month_label", "Month"),
    ("quarter", "Quarter"),
]


def _build_sheet(bookings: pd.DataFrame, sales_col: str, sales_header: str) -> pd.DataFrame:
    """Project `bookings` down to one export sheet's columns, in order."""
    out = pd.DataFrame()
    for src_col, header in _COMMON_COLUMNS:
        if src_col in bookings.columns:
            out[header] = bookings[src_col]
    if sales_col in bookings.columns:
        out[sales_header] = bookings[sales_col]

    for dt_col in ("Start", "End"):
        if dt_col in out.columns:
            out[dt_col] = pd.to_datetime(out[dt_col]).dt.strftime("%Y-%m-%d %H:%M")

    return out


def build_export_workbook(bookings: pd.DataFrame) -> bytes:
    """
    Build a two-sheet .xlsx workbook ("Net", "Gross") from the merged
    bookings DataFrame and return it as raw bytes ready to stream back to
    the client. Each sheet carries its own sales column (Net Sales /
    Gross Sales — always from `bookings`, per CLAUDE.md §5.4, never from
    host_summary) plus the Client Type column merged in from the Host file.
    """
    net_df = _build_sheet(bookings, "Net Sales", "Net Sales")
    gross_df = _build_sheet(bookings, "Gross Sales", "Gross Sales")

    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        net_df.to_excel(writer, sheet_name="Net", index=False)
        gross_df.to_excel(writer, sheet_name="Gross", index=False)
    return buf.getvalue()
