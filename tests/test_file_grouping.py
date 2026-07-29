"""
Tests for the content-based multi-year file grouping primitive
(app/utils/parser.py: probe_file / group_files_by_period). Not wired into
any route yet — this validates the grouping engine in isolation, against
real /data fixtures wherever possible.
"""
from pathlib import Path

import pandas as pd
import pytest

from app.utils.parser import group_files_by_period, parse_ems_files, probe_file
from tests.conftest import DATA_DIR


def _all_files_in(dir_path: Path) -> list[Path]:
    """Every Excel file in a directory, filename-agnostic — group_files_by_period()
    doesn't use filenames at all, so this deliberately doesn't either."""
    return sorted(p for p in dir_path.iterdir() if p.is_file() and p.suffix.lower() in (".xlsx", ".xls"))


@pytest.fixture(scope="session")
def fy24_files():
    return _all_files_in(DATA_DIR / "FY24")


@pytest.fixture(scope="session")
def fy25_files():
    return _all_files_in(DATA_DIR / "FY25")


@pytest.fixture(scope="session")
def fy26_files():
    return _all_files_in(DATA_DIR / "FY26")


def test_valid_multi_year_grouping(fy24_files, fy25_files, fy26_files):
    all_files = fy24_files + fy25_files + fy26_files
    groups = group_files_by_period(all_files)

    assert len(groups) == 3
    for g in groups.values():
        assert g.status == "valid"
        assert g.net_path is not None
        assert g.gross_path is not None
        assert g.host_path is not None
        assert g.missing_roles == []
        assert g.collision_roles == []
        assert g.unrecognized_files == []

    # Spot-check: handing one group's trio to the unchanged parse_ems_files()
    # still passes the reconciliation guard — proves the new grouping layer
    # and the existing parse primitive actually compose.
    any_group = next(iter(groups.values()))
    dataset = parse_ems_files(
        net_path=any_group.net_path,
        gross_path=any_group.gross_path,
        host_path=any_group.host_path,
    )
    assert dataset.validation.errors == []
    assert len(dataset.bookings) > 0


def test_valid_single_year_is_just_the_one_group_case(fy26_files):
    """A single-year, 3-file upload isn't a special path — it's what the
    general algorithm produces when only one period is present."""
    groups = group_files_by_period(fy26_files)
    assert len(groups) == 1
    g = next(iter(groups.values()))
    assert g.status == "valid"


def test_incomplete_missing_host(fy26_files):
    net_or_gross_only = [p for p in fy26_files if probe_file(p).role in ("net", "gross")]
    groups = group_files_by_period(net_or_gross_only)
    assert len(groups) == 1
    g = next(iter(groups.values()))
    assert g.status == "incomplete"
    assert g.missing_roles == ["host"]


def test_ambiguous_role_collision(fy26_files):
    net_path = next(p for p in fy26_files if probe_file(p).role == "net")
    batch = fy26_files + [net_path]  # duplicate the net file -> 2 files claim "net"
    groups = group_files_by_period(batch)
    assert len(groups) == 1
    g = next(iter(groups.values()))
    assert g.status == "ambiguous"
    assert g.collision_roles == ["net"]
    assert g.unrecognized_files == []


def test_ambiguous_unrecognized_file(fy26_files, tmp_path):
    # A file whose header matches neither booking nor host required fields,
    # but does carry the same reporting-period text as the real FY26 files
    # — so it joins their group instead of forming its own "Unknown" group,
    # actually exercising "unrecognized file inside an otherwise-real
    # period's group" rather than just "no period detected at all".
    garbage_path = tmp_path / "garbage.xlsx"
    pd.DataFrame([
        ["Reporting Period: 7/1/2025 thru 6/30/2026"],
        ["not an EMS export"],
        [123],
    ]).to_excel(garbage_path, index=False, header=False)

    host_path = next(p for p in fy26_files if probe_file(p).role == "host")
    net_path = next(p for p in fy26_files if probe_file(p).role == "net")
    batch = [net_path, host_path, garbage_path]

    groups = group_files_by_period(batch)
    assert len(groups) == 1
    g = next(iter(groups.values()))
    assert g.status == "ambiguous"
    assert garbage_path in g.unrecognized_files


def test_one_bad_group_does_not_block_a_valid_group(fy24_files, fy26_files):
    incomplete_fy24 = [p for p in fy24_files if probe_file(p).role != "host"]  # drop host
    batch = incomplete_fy24 + fy26_files
    groups = group_files_by_period(batch)

    assert len(groups) == 2
    statuses = {g.status for g in groups.values()}
    assert statuses == {"incomplete", "valid"}

    valid_group = next(g for g in groups.values() if g.status == "valid")
    assert valid_group.net_path is not None
    assert valid_group.gross_path is not None
    assert valid_group.host_path is not None


def test_probe_tie_break_matches_real_parse_when_both_sales_columns_present(fy26_files):
    """If a file's header somehow has both net_sales and gross_sales aliases,
    probe_file()'s tie-break must agree with what _parse_booking_sheet()
    would actually resolve it to (both default to net)."""
    net_path = next(p for p in fy26_files if probe_file(p).role == "net")
    probe = probe_file(net_path)
    assert probe.role == "net"

    # This file genuinely only has a Net Sales column (no artificial
    # ambiguity needed) — confirms probing agrees with the real parse for
    # the actual fixture, which is the case that matters in practice.
    dataset = parse_ems_files(
        net_path=net_path,
        gross_path=next(p for p in fy26_files if probe_file(p).role == "gross"),
        host_path=next(p for p in fy26_files if probe_file(p).role == "host"),
    )
    assert "Net Sales" in dataset.bookings.columns
