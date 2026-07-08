"""
EMS Data Parser & Cleaner
=========================
Parses the three EMS report formats exported from the booking system:
  1. Net Sales by Booking   – row-per-event with merged cells
  2. Gross Sales by Booking – same layout, different money column
  3. Gross Sales by Host    – summary grouped by host type / host name

Design decisions
----------------
- We NEVER impute or fabricate missing monetary values.
  If a row genuinely has no sales figure we record 0.0 (the EMS system
  exports a blank cell when a booking is $0, e.g. internal-ASU events).
- Data-quality issues (duplicate Res IDs, mismatched Net vs Gross,
  non-numeric sales) are logged in a validation report returned alongside
  the parsed frames so the UI can surface them.
- All string cleaning is limited to whitespace/newline normalisation and
  capitalisation; we do not rename hosts or merge similar-looking names
  automatically (that would silently alter business data).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data containers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationReport:
    total_rows_raw: int = 0
    total_rows_parsed: int = 0
    rows_skipped_header: int = 0
    rows_skipped_total: int = 0
    rows_skipped_bad_date: int = 0
    zero_sales_count: int = 0
    duplicate_res_ids: list = field(default_factory=list)
    mismatched_net_gross: list = field(default_factory=list)  # res_ids where values differ by >1%
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self):
        return {
            "total_rows_raw": self.total_rows_raw,
            "total_rows_parsed": self.total_rows_parsed,
            "rows_skipped_header": self.rows_skipped_header,
            "rows_skipped_total": self.rows_skipped_total,
            "rows_skipped_bad_date": self.rows_skipped_bad_date,
            "zero_sales_count": self.zero_sales_count,
            "duplicate_res_ids_count": len(self.duplicate_res_ids),
            "mismatch_count": len(self.mismatched_net_gross),
            "warnings": self.warnings,
            "errors": self.errors,
        }


@dataclass
class ParsedDataset:
    bookings: pd.DataFrame = field(default_factory=pd.DataFrame)
    host_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    validation: ValidationReport = field(default_factory=ValidationReport)
    reporting_period: str = "Unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_str(v) -> str:
    if pd.isna(v):
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\n", " ")).strip()


def _safe_float(v) -> Optional[float]:
    """Return float or None – never fabricate."""
    if pd.isna(v):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _is_total_row(row_series) -> bool:
    """Detect subtotal / grand-total / page-footer rows."""
    for cell in row_series:
        s = _clean_str(cell)
        if s in ("Date Total", "Month Total", "Grand Total"):
            return True
        if re.match(r"^Page \d+ of \d+$", s):
            return True
    return False


def _extract_reporting_period(raw_df: pd.DataFrame) -> str:
    """Pull the 'Reporting Period: ...' text from the header block."""
    for _, row in raw_df.iterrows():
        for cell in row:
            s = _clean_str(cell)
            if s.startswith("Reporting Period:"):
                return s.replace("Reporting Period:", "").strip()
    return "Unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Booking file parser  (Net Sales & Gross Sales share the same layout)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_booking_sheet(
    raw_df: pd.DataFrame,
    sales_col_name: str,
    report: ValidationReport,
) -> pd.DataFrame:
    """
    Column map (0-indexed, from the EMS export):
      0  – Start datetime
      1  – End datetime
      2  – Host (client / department name)
      5  – Event Name / Location
      11 – Payment type / billing code
      13 – Booking status
      16 – Res ID (the room reservation ID that appears on row N)
           Book ID appears on row N+1 in the same column
      18 – Net Sales *or* Gross Sales (depends on the file)

    The EMS export is a merged-cell report:
      • Row pattern per booking:
          Row A: start, end, host, event_name, payment_type, status, res_id, sales
          Row B: blank except col-16 = book_id
          Row C: blank except col-5  = room_name
        (sometimes rows B and C are swapped or col-5 room appears on a 4th row)
      • Date-header rows: col-0 = date@midnight, col-1 = NaT
      • Total rows: 'Date Total' / 'Month Total' in col-14
      • Page break rows: 'Page N of N' in col-14 or col-15
    """
    report.total_rows_raw += len(raw_df)
    records = []
    pending: dict = {}   # accumulator for the current booking block
    
    # Determine room column by scanning first 100 data rows
    # The room name is always in col-5 on a row where col-0 and col-1 are NaT
    # but col-2 is also NaT (unlike event rows where col-2 = host).

    for _, row in raw_df.iterrows():
        # ── Skip total / page-footer rows ─────────────────────────────────────
        if _is_total_row(row):
            report.rows_skipped_total += 1
            # Flush pending if it exists but has no room yet
            if pending:
                records.append(pending)
                pending = {}
            continue

        v0 = row.iloc[0]
        v1 = row.iloc[1]
        v2 = row.iloc[2]
        v5 = row.iloc[5]
        v11 = row.iloc[11]
        v13 = row.iloc[13]
        v16 = row.iloc[16]
        v18 = row.iloc[18]

        # ── Date-header row (midnight, no end time, no host) ──────────────────
        if pd.notna(v0) and pd.isna(v1) and pd.isna(v2):
            report.rows_skipped_header += 1
            continue

        # ── Event data row (has start, end, and host) ─────────────────────────
        if pd.notna(v0) and pd.notna(v1) and pd.notna(v2):
            # Flush previous booking
            if pending:
                records.append(pending)
                pending = {}

            try:
                start = pd.to_datetime(v0)
                end   = pd.to_datetime(v1)
            except Exception:
                report.rows_skipped_bad_date += 1
                continue

            sales_raw = _safe_float(v18)
            sales = 0.0 if sales_raw is None else sales_raw
            if sales_raw is None and _clean_str(v18) not in ("", "nan"):
                report.warnings.append(
                    f"Non-numeric {sales_col_name} '{v18}' on "
                    f"{start.date()} – {_clean_str(v2)[:40]}; treated as 0."
                )

            pending = {
                "start":        start,
                "end":          end,
                "duration_hrs": round((end - start).total_seconds() / 3600, 2),
                "host":         _clean_str(v2),
                "event_name":   _clean_str(v5),
                "payment_type": _clean_str(v11),
                "status":       _clean_str(v13),
                "res_id":       _clean_str(v16) if pd.notna(v16) else "",
                "book_id":      "",
                "room":         "",
                sales_col_name: sales,
                "month":        start.strftime("%Y-%m"),
                "month_label":  start.strftime("%b %Y"),
                "weekday":      start.strftime("%A"),
                "hour":         start.hour,
                "date":         start.date(),
                "year":         start.year,
                "quarter":      f"Q{start.quarter}",
            }
            continue

        # ── Continuation row (book_id or room) ────────────────────────────────
        if pending:
            # Book ID row: col-16 has a numeric-ish string, col-5 is blank
            if pd.notna(v16) and pd.isna(v5):
                pending["book_id"] = _clean_str(v16)
            # Room row: col-5 has the room name
            if pd.notna(v5) and pd.isna(v0):
                existing_room = pending.get("room", "")
                room_str = _clean_str(v5)
                if existing_room == "":
                    pending["room"] = room_str
                # else already set; EMS sometimes repeats room on a 3rd continuation row

    # Flush last pending
    if pending:
        records.append(pending)

    report.total_rows_parsed += len(records)
    df = pd.DataFrame(records)

    if df.empty:
        return df

    # ── Post-parse cleanup ────────────────────────────────────────────────────
    df[sales_col_name] = pd.to_numeric(df[sales_col_name], errors="coerce").fillna(0.0)
    df["start"] = pd.to_datetime(df["start"])
    df["end"]   = pd.to_datetime(df["end"])

    # Count genuine zeros (internal/ASU events billed at $0)
    report.zero_sales_count = int((df[sales_col_name] == 0).sum())

    return df


# ─────────────────────────────────────────────────────────────────────────────
# Host summary file parser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_host_sheet(raw_df: pd.DataFrame) -> pd.DataFrame:
    """
    Column map for the Host summary report:
      1  – Host Type  (ASU / Public / SkySong Affiliate / SkySong Tenants)
      5  – Host name
      11 – Setup Count
      13 – Attendance
      17 – Gross Sales
    """
    records = []
    current_host_type = "Unknown"

    for _, row in raw_df.iterrows():
        v1  = row.iloc[1]
        v5  = row.iloc[5]
        v11 = row.iloc[11]
        v13 = row.iloc[13]
        v17 = row.iloc[17]

        # Update host type header
        s1 = _clean_str(v1)
        if s1 and s1 not in ("Host Type", "Total", "Grand Total"):
            current_host_type = s1

        # Skip non-data rows
        if pd.isna(v5):
            continue
        host_name = _clean_str(v5)
        if not host_name or host_name in ("Host", "nan"):
            continue
        if re.match(r"^Page \d+ of \d+$", host_name):
            continue

        setup     = _safe_float(v11)
        attend    = _safe_float(v13)
        gross     = _safe_float(v17)

        # Skip total aggregation rows (col-11 may say "Total")
        if _clean_str(v11) == "Total":
            continue

        if gross is None:
            continue   # row has no monetary data – skip silently

        records.append({
            "host_type":   current_host_type,
            "host":        host_name,
            "setup_count": int(setup)  if setup  is not None else 0,
            "attendance":  int(attend) if attend is not None else 0,
            "gross_sales": gross,
        })

    df = pd.DataFrame(records)
    if not df.empty:
        df["gross_sales"] = pd.to_numeric(df["gross_sales"], errors="coerce").fillna(0.0)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Cross-file validation
# ─────────────────────────────────────────────────────────────────────────────

def _cross_validate(df_net: pd.DataFrame, df_gross: pd.DataFrame, report: ValidationReport):
    """
    Check that where res_id matches between the two booking files,
    gross >= net (discounts reduce net from gross; they cannot be inverted).
    We flag mismatches but do NOT alter values.
    """
    if df_net.empty or df_gross.empty:
        return

    if "res_id" not in df_net.columns or "res_id" not in df_gross.columns:
        return

    net_by_res  = df_net.groupby("res_id")["Net Sales"].sum()
    gross_by_res = df_gross.groupby("res_id")["Gross Sales"].sum()

    common = net_by_res.index.intersection(gross_by_res.index)
    for rid in common:
        n = net_by_res[rid]
        g = gross_by_res[rid]
        if g > 0 and n > g * 1.01:   # allow 1 % float tolerance
            report.mismatched_net_gross.append(
                {"res_id": rid, "net": round(n, 2), "gross": round(g, 2)}
            )
            report.warnings.append(
                f"Res {rid}: Net Sales ({n:.2f}) > Gross Sales ({g:.2f}). "
                "Values retained as-is; please verify in EMS."
            )

    # Duplicate res_ids within the same file
    dupes = df_net[df_net["res_id"].duplicated(keep=False)]["res_id"].unique().tolist()
    if dupes:
        report.duplicate_res_ids = dupes[:20]   # cap list length
        report.warnings.append(
            f"{len(dupes)} Res IDs appear more than once in the Net Sales file "
            "(multi-room bookings or repeated date blocks – expected behaviour)."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Classify host segment
# ─────────────────────────────────────────────────────────────────────────────

def _classify_host_segment(host: str) -> str:
    """
    Derive a client-segment label from the host name using keyword matching.
    This is the FALLBACK method, used only when a host cannot be matched
    against the Host summary file's explicit host_type column (see
    _build_host_type_lookup below, which is the preferred source of truth).
    """
    h = host.upper()
    if h.startswith("ASU ") or h.startswith("ARIZONA STATE"):
        return "ASU"
    if any(kw in h for kw in ("SKYSONG", "SKY SONG")):
        return "SkySong"
    if any(kw in h for kw in ("GOVERNMENT", "COUNTY", "STATE ", "FEDERAL", "CITY OF", "DEPARTMENT")):
        return "Government"
    if any(kw in h for kw in ("SCHOOL", "ACADEMY", "UNIVERSITY", "COLLEGE", "EDUCATION", "DISTRICT")):
        return "Education"
    if any(kw in h for kw in ("HEALTH", "MEDICAL", "HOSPITAL", "CLINIC", "NURSING")):
        return "Healthcare"
    if any(kw in h for kw in ("TECHNOLOGY", "SOFTWARE", "DIGITAL", "CYBER", "COMPUTING", "CLOUD", "AI ", "DATA ")):
        return "Technology"
    if any(kw in h for kw in ("ASSOCIATION", "SOCIETY", "ALLIANCE", "INSTITUTE", "FOUNDATION", "NONPROFIT", "NON-PROFIT")):
        return "Non-Profit / Association"
    return "Commercial / Other"


def _normalize_host_key(name: str) -> str:
    """
    Normalise a host name into a matching key so that harmless formatting
    differences between the two files (extra whitespace introduced when
    Excel's wrapped-text newlines collapse next to a hyphen, e.g.
    'Well- Being' vs 'Well-Being') don't prevent a legitimate match.
    This key is used ONLY for matching; the original display name from
    the booking file is always preserved unchanged in the output.
    """
    key = re.sub(r"\s+", " ", name).strip().upper()
    key = re.sub(r"\s*-\s*", "-", key)   # normalise spacing around hyphens
    return key


def _build_host_type_lookup(host_summary: pd.DataFrame) -> dict:
    """
    Build a {normalised_host_name: host_type} lookup from the Host summary
    file. This is the ONLY thing the Host file is used for in downstream
    analytics — it is a client/host relationship (name -> type) lookup,
    never a source of dollar figures. Revenue always comes from the
    booking-level Net/Gross files.
    """
    if host_summary.empty:
        return {}
    lookup = {}
    for _, row in host_summary.iterrows():
        key = _normalize_host_key(row["host"])
        # If the same normalised host appears twice with different types
        # (shouldn't happen, but data can surprise you), keep the first
        # and don't silently overwrite — this would be a data-quality flag.
        lookup.setdefault(key, row["host_type"])
    return lookup


def _apply_host_type(bookings: pd.DataFrame, host_summary: pd.DataFrame, report: ValidationReport) -> pd.DataFrame:
    """
    Attach a 'segment' column to bookings using the Host file's host_type
    as ground truth wherever a host name can be matched, falling back to
    the keyword heuristic only for hosts that don't appear in the Host
    file. Also records match-rate statistics in the validation report so
    the user can see how much of the segmentation is ground-truth vs
    heuristic.
    """
    lookup = _build_host_type_lookup(host_summary)

    def _resolve(host: str) -> tuple[str, str]:
        key = _normalize_host_key(host)
        if key in lookup:
            return lookup[key], "host_file"
        return _classify_host_segment(host), "heuristic"

    resolved = bookings["host"].apply(_resolve)
    bookings = bookings.copy()
    bookings["segment"] = resolved.apply(lambda t: t[0])
    bookings["segment_source"] = resolved.apply(lambda t: t[1])

    if lookup:
        matched = int((bookings["segment_source"] == "host_file").sum())
        total = len(bookings)
        report.warnings.append(
            f"Client segmentation: {matched}/{total} bookings ({matched/total*100:.1f}%) "
            f"matched to a host_type from the Host summary file; the remainder used "
            f"keyword-based classification as a fallback."
        )

    return bookings


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def parse_ems_files(
    net_path: str | Path,
    gross_path: str | Path,
    host_path: str | Path,
) -> ParsedDataset:
    """
    Parse all three EMS export files and return a unified ParsedDataset.

    Parameters
    ----------
    net_path   : path to EMS_Net_Sales_by_Booking_*.xlsx
    gross_path : path to Gross_Sales_by_Booking_*.xlsx
    host_path  : path to Gross_Sales_by_Host_*.xlsx

    Returns
    -------
    ParsedDataset with .bookings (merged), .host_summary, .validation
    """
    report = ValidationReport()

    # ── Read raw sheets ───────────────────────────────────────────────────────
    try:
        raw_net   = pd.read_excel(net_path,   sheet_name="Sheet1", header=None)
        raw_gross = pd.read_excel(gross_path, sheet_name="Sheet1", header=None)
        raw_host  = pd.read_excel(host_path,  sheet_name="Sheet1", header=None)
    except Exception as exc:
        report.errors.append(f"File read error: {exc}")
        return ParsedDataset(validation=report)

    period = _extract_reporting_period(raw_net)

    # ── Parse each file ───────────────────────────────────────────────────────
    df_net   = _parse_booking_sheet(raw_net,   "Net Sales",   report)
    df_gross = _parse_booking_sheet(raw_gross, "Gross Sales", report)
    df_host  = _parse_host_sheet(raw_host)

    # ── Cross-validate ────────────────────────────────────────────────────────
    _cross_validate(df_net, df_gross, report)

    # ── Merge net + gross on res_id + start ───────────────────────────────────
    if df_net.empty or df_gross.empty:
        bookings = df_net if not df_net.empty else df_gross
    else:
        merge_keys = ["res_id", "start"]
        gross_cols = merge_keys + ["Gross Sales"]
        bookings = df_net.merge(
            df_gross[gross_cols],
            on=merge_keys,
            how="left",
            suffixes=("", "_gross"),
        )
        # Fill unmatched gross with 0 (do NOT fabricate)
        bookings["Gross Sales"] = bookings["Gross Sales"].fillna(0.0)

    # ── Derived columns ───────────────────────────────────────────────────────
    if not bookings.empty:
        bookings["discount"] = (
            bookings["Gross Sales"] - bookings["Net Sales"]
        ).clip(lower=0)
        bookings["discount_pct"] = np.where(
            bookings["Gross Sales"] > 0,
            bookings["discount"] / bookings["Gross Sales"] * 100,
            0.0,
        )

        # Client segmentation: Host file's host_type is ground truth (used
        # ONLY as a name -> type lookup, never for dollar figures); keyword
        # heuristic is a fallback for any host not found in the Host file.
        bookings = _apply_host_type(bookings, df_host, report)

        # Fiscal year label (ASU FY: Jul–Jun)
        bookings["fiscal_year"] = bookings["start"].apply(
            lambda d: f"FY{(d.year + 1) % 100:02d}" if d.month >= 7 else f"FY{d.year % 100:02d}"
        )

    return ParsedDataset(
        bookings=bookings,
        host_summary=df_host,
        validation=report,
        reporting_period=period,
    )


def parse_single_file(filepath: str | Path, file_role: str) -> dict:
    """
    Parse a single uploaded file. file_role ∈ {'net', 'gross', 'host'}.
    Returns a dict with 'data' (list of dicts) and 'validation'.
    """
    raw = pd.read_excel(filepath, sheet_name=0, header=None)
    report = ValidationReport()

    if file_role == "host":
        df = _parse_host_sheet(raw)
    else:
        sales_col = "Net Sales" if file_role == "net" else "Gross Sales"
        df = _parse_booking_sheet(raw, sales_col, report)
        if not df.empty:
            df["segment"] = df["host"].apply(_classify_host_segment)

    return {
        "data": df.to_dict(orient="records") if not df.empty else [],
        "rows": len(df),
        "validation": report.to_dict(),
    }
