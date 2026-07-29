"""
Tests for the upload preview/confirm flow (POST /upload/files ->
POST /upload/confirm), now content-grouped and multi-year-capable.
Everything here goes through the real HTTP endpoints with real /data
fixtures, not direct store.save_dataset() calls — this is what actually
exercises group_files_by_period(), the batch folder, and the
group_id/dataset_id-distinct confirm contract.
"""
from io import BytesIO
from pathlib import Path

import pytest

from app.models import store
from app.utils.parser import probe_file
from tests.conftest import DATA_DIR


def _post_files(client, paths):
    data = {"files": [(BytesIO(p.read_bytes()), p.name) for p in paths]}
    return client.post("/upload/files", data=data, content_type="multipart/form-data")


def _only_ready_group(preview_json):
    """These tests upload exactly one fiscal year's trio at a time, so the
    preview should always contain exactly one "ready" group."""
    ready = [g for g in preview_json["groups"] if g["status"] == "ready"]
    assert len(ready) == 1, preview_json["groups"]
    return ready[0]


def _all_files_in(dir_path: Path) -> list[Path]:
    """Every Excel file in a directory, filename-agnostic -- role/period
    detection is 100% content-based, so fixture selection deliberately
    doesn't lean on filenames either (mirrors test_file_grouping.py)."""
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


def test_confirm_new_dataset_id_persists_lists_and_matches_direct_parse(
    client, fy26_file_paths, real_fy26_dataset
):
    preview = _post_files(client, fy26_file_paths).get_json()
    group = _only_ready_group(preview)
    assert group["key_exists"] is False  # nothing persisted at preview stage
    dataset_id = group["detected_dataset_id"]

    confirm = client.post(
        "/upload/confirm",
        json={"batch_id": preview["batch_id"], "group_id": group["group_id"], "dataset_id": dataset_id},
    )
    assert confirm.status_code == 200
    assert confirm.get_json()["dataset_id"] == dataset_id

    # Listed
    ids = [d["id"] for d in client.get("/api/datasets").get_json()]
    assert dataset_id in ids

    # Matches a direct parse_ems_files() call on the same real files.
    dash = client.get(f"/api/dashboard?ds={dataset_id}").get_json()
    assert dash["reporting_period"] == real_fy26_dataset.reporting_period
    assert dash["kpis"]["total_gross_sales"] == round(real_fy26_dataset.bookings["Gross Sales"].sum(), 2)
    assert dash["kpis"]["total_net_sales"] == round(real_fy26_dataset.bookings["Net Sales"].sum(), 2)
    assert dash["kpis"]["total_events"] == len(real_fy26_dataset.bookings)


def test_confirm_existing_dataset_id_overwrites_invalidates_cache_and_changes_totals(
    client, fy26_file_paths, trimmed_fy26_file_paths
):
    # First upload: full real data.
    preview1 = _post_files(client, fy26_file_paths).get_json()
    group1 = _only_ready_group(preview1)
    dataset_id = group1["detected_dataset_id"]
    client.post(
        "/upload/confirm",
        json={"batch_id": preview1["batch_id"], "group_id": group1["group_id"], "dataset_id": dataset_id},
    )

    resp1 = client.get(f"/api/dashboard?ds={dataset_id}")
    assert resp1.status_code == 200
    etag1 = resp1.headers["ETag"]
    rows1 = resp1.get_json()["kpis"]["total_events"]
    assert store.get_dashboard_cache(dataset_id) is not None

    # Phase 1 cache pattern still holds: unchanged dataset -> 304 on repeat.
    resp1b = client.get(f"/api/dashboard?ds={dataset_id}", headers={"If-None-Match": etag1})
    assert resp1b.status_code == 304

    # Second upload: SAME auto-detected dataset_id (same real reporting
    # period), but genuinely fewer rows/smaller totals (trimmed file).
    preview2 = _post_files(client, trimmed_fy26_file_paths).get_json()
    group2 = _only_ready_group(preview2)
    assert group2["key_exists"] is True
    assert group2["detected_dataset_id"] == dataset_id
    assert group2["existing"]["row_count"] == rows1

    confirm2 = client.post(
        "/upload/confirm",
        json={"batch_id": preview2["batch_id"], "group_id": group2["group_id"], "dataset_id": dataset_id},
    )
    assert confirm2.status_code == 200
    assert confirm2.get_json()["dataset_id"] == dataset_id

    # save_dataset()'s UPSERT must invalidate the cache entry immediately,
    # before any GET repopulates it.
    assert store.get_dashboard_cache(dataset_id) is None

    # The pre-overwrite ETag must no longer match -> 200, not 304 (a stale
    # cache would incorrectly serve 304 here).
    resp2 = client.get(f"/api/dashboard?ds={dataset_id}", headers={"If-None-Match": etag1})
    assert resp2.status_code == 200
    etag2 = resp2.headers["ETag"]
    assert etag2 != etag1

    # And the payload itself must reflect the NEW upload's numbers, not a
    # stale cached copy of the old ones.
    body2 = resp2.get_json()
    rows2 = body2["kpis"]["total_events"]
    assert rows2 != rows1
    assert rows2 < rows1  # trimmed file strictly has fewer bookings

    # UPSERT, not a duplicate row.
    ids = [d["id"] for d in client.get("/api/datasets").get_json()]
    assert ids.count(dataset_id) == 1


def test_two_fiscal_years_independent_via_upload_confirm_flow(
    client, fy26_file_paths, trimmed_fy26_file_paths, real_fy26_dataset
):
    """
    Re-runs the §6 two-fiscal-year independence check, but through the real
    /upload/files -> /upload/confirm HTTP path instead of calling
    parse_ems_files()/store.save_dataset() directly. Both real file sets
    derive the same real reporting_period, so getting two independent keys
    means using the confirm step's caller-supplied dataset_id override —
    exactly the "edit the detected label before saving" path, group_id kept
    distinct from dataset_id throughout.
    """
    preview_a = _post_files(client, fy26_file_paths).get_json()
    group_a = _only_ready_group(preview_a)
    id_a = group_a["detected_dataset_id"]
    client.post(
        "/upload/confirm",
        json={"batch_id": preview_a["batch_id"], "group_id": group_a["group_id"], "dataset_id": id_a},
    )

    preview_b = _post_files(client, trimmed_fy26_file_paths).get_json()
    group_b = _only_ready_group(preview_b)
    id_b = "FY99-TEST-UPLOAD-FLOW"
    assert group_b["detected_dataset_id"] != id_b  # confirms the override actually diverges
    client.post(
        "/upload/confirm",
        json={"batch_id": preview_b["batch_id"], "group_id": group_b["group_id"], "dataset_id": id_b},
    )

    assert id_a != id_b

    kpis_a = client.get(f"/api/kpis?ds={id_a}").get_json()
    kpis_b = client.get(f"/api/kpis?ds={id_b}").get_json()

    expected_a_gross = round(real_fy26_dataset.bookings["Gross Sales"].sum(), 2)
    assert kpis_a["total_gross_sales"] == expected_a_gross
    assert kpis_a["total_events"] == len(real_fy26_dataset.bookings)
    assert kpis_b["total_gross_sales"] != expected_a_gross
    assert kpis_b["total_events"] < kpis_a["total_events"]

    # Loading/confirming B must not have mutated A (no cross-key bleed).
    kpis_a_again = client.get(f"/api/kpis?ds={id_a}").get_json()
    assert kpis_a_again["total_gross_sales"] == expected_a_gross

    ids = [d["id"] for d in client.get("/api/datasets").get_json()]
    assert id_a in ids and id_b in ids


def test_three_year_batch_all_ready_and_independently_confirmable(
    client, fy24_files, fy25_files, fy26_files, real_fy26_dataset
):
    """One upload spanning 3 distinct fiscal years -> 3 correctly-detected
    "ready" groups, each confirmable on its own."""
    batch = fy24_files + fy25_files + fy26_files
    preview = _post_files(client, batch).get_json()

    ready = [g for g in preview["groups"] if g["status"] == "ready"]
    assert len(ready) == 3, preview["groups"]
    assert len({g["reporting_period"] for g in ready}) == 3  # 3 genuinely distinct years
    assert len({g["detected_dataset_id"] for g in ready}) == 3

    for g in ready:
        confirm = client.post(
            "/upload/confirm",
            json={"batch_id": preview["batch_id"], "group_id": g["group_id"], "dataset_id": g["detected_dataset_id"]},
        )
        assert confirm.status_code == 200, confirm.get_json()
        assert confirm.get_json()["dataset_id"] == g["detected_dataset_id"]

    ids = [d["id"] for d in client.get("/api/datasets").get_json()]
    for g in ready:
        assert g["detected_dataset_id"] in ids

    # Spot-check the FY26 group specifically matches a known-good direct parse.
    fy26_group = next(g for g in ready if g["reporting_period"] == real_fy26_dataset.reporting_period)
    dash = client.get(f"/api/dashboard?ds={fy26_group['detected_dataset_id']}").get_json()
    assert dash["kpis"]["total_gross_sales"] == round(real_fy26_dataset.bookings["Gross Sales"].sum(), 2)
    assert dash["kpis"]["total_events"] == len(real_fy26_dataset.bookings)


def test_incomplete_year_does_not_block_other_complete_years(client, fy24_files, fy25_files, fy26_files):
    """One year missing its Host file, alongside 2 complete years in the
    same batch -> only that year is flagged; the other 2 are unaffected."""
    fy24_missing_host = [p for p in fy24_files if probe_file(p).role != "host"]
    batch = fy24_missing_host + fy25_files + fy26_files

    preview = _post_files(client, batch).get_json()
    groups = preview["groups"]

    incomplete = [g for g in groups if g["status"] == "incomplete"]
    ready = [g for g in groups if g["status"] == "ready"]
    assert len(incomplete) == 1, groups
    assert incomplete[0]["missing_roles"] == ["host"]
    assert len(ready) == 2, groups

    for g in ready:
        confirm = client.post(
            "/upload/confirm",
            json={"batch_id": preview["batch_id"], "group_id": g["group_id"], "dataset_id": g["detected_dataset_id"]},
        )
        assert confirm.status_code == 200

    ids = [d["id"] for d in client.get("/api/datasets").get_json()]
    for g in ready:
        assert g["detected_dataset_id"] in ids
    # Nothing was silently persisted for the incomplete year under any plausible id.
    assert store.derive_dataset_id(incomplete[0]["reporting_period"]) not in ids


def test_ambiguous_duplicate_net_file_not_parsed_or_guessed(client, fy26_files):
    """Two files both detected as Net for the same period -> the whole
    period is flagged ambiguous, not parsed, and never silently guessed
    from one of the two candidates."""
    net_path = next(p for p in fy26_files if probe_file(p).role == "net")
    batch = fy26_files + [net_path]  # a second file also claims "net"

    preview = _post_files(client, batch).get_json()
    groups = preview["groups"]

    ambiguous = [g for g in groups if g["status"] == "ambiguous"]
    ready = [g for g in groups if g["status"] == "ready"]
    assert len(ambiguous) == 1, groups
    assert ready == []  # the collision blocks the whole period, no fallback guess
    assert "net_sales" in ambiguous[0]["detail"]
    assert "group_id" not in ambiguous[0]  # nothing to confirm

    # Confirming an ambiguous group is impossible even if you guess the id
    # that would have been assigned had it been valid.
    guessed_id = store.derive_dataset_id(ambiguous[0]["reporting_period"])
    confirm = client.post(
        "/upload/confirm",
        json={"batch_id": preview["batch_id"], "group_id": guessed_id, "dataset_id": guessed_id},
    )
    assert confirm.status_code == 404

    ids = [d["id"] for d in client.get("/api/datasets").get_json()]
    assert guessed_id not in ids


def test_confirm_one_group_leaves_other_overwrite_flagged_group_untouched(client, fy24_files, fy25_files):
    """Confirming one "ready" group must not touch a second, overwrite-
    flagged group left unconfirmed in the same batch."""
    # Baseline: FY24 already persisted from an earlier, separate upload.
    preview0 = _post_files(client, fy24_files).get_json()
    group0 = _only_ready_group(preview0)
    fy24_id = group0["detected_dataset_id"]
    client.post(
        "/upload/confirm",
        json={"batch_id": preview0["batch_id"], "group_id": group0["group_id"], "dataset_id": fy24_id},
    )
    baseline_rows = client.get(f"/api/dashboard?ds={fy24_id}").get_json()["kpis"]["total_events"]

    # New batch: FY24 again (now overwrite-flagged) plus brand-new FY25.
    preview = _post_files(client, fy24_files + fy25_files).get_json()
    ready = [g for g in preview["groups"] if g["status"] == "ready"]
    assert len(ready) == 2

    fy24_group = next(g for g in ready if g["detected_dataset_id"] == fy24_id)
    fy25_group = next(g for g in ready if g["detected_dataset_id"] != fy24_id)
    assert fy24_group["key_exists"] is True
    assert fy25_group["key_exists"] is False

    # Confirm only FY25 -- FY24's group_id in this batch is deliberately never posted.
    confirm = client.post(
        "/upload/confirm",
        json={"batch_id": preview["batch_id"], "group_id": fy25_group["group_id"], "dataset_id": fy25_group["detected_dataset_id"]},
    )
    assert confirm.status_code == 200

    ids = [d["id"] for d in client.get("/api/datasets").get_json()]
    assert fy25_group["detected_dataset_id"] in ids
    assert fy24_id in ids  # still present from the baseline confirm

    # FY24's persisted data is exactly what it was before this batch existed.
    rows_after = client.get(f"/api/dashboard?ds={fy24_id}").get_json()["kpis"]["total_events"]
    assert rows_after == baseline_rows


def test_confirm_all_ready_bulk_loop_skips_existing_key_group(client, fy24_files, fy25_files, fy26_files):
    """Simulates the frontend's "Confirm all ready" bulk action, which loops
    /upload/confirm only over groups with status=="ready" and
    key_exists==False. A mix of new and existing keys in the same batch ->
    only the new-key groups get swept up; the existing-key group is
    untouched until confirmed individually afterward."""
    # Baseline: FY24 already persisted, so it'll come back overwrite-flagged.
    preview0 = _post_files(client, fy24_files).get_json()
    group0 = _only_ready_group(preview0)
    fy24_id = group0["detected_dataset_id"]
    client.post(
        "/upload/confirm",
        json={"batch_id": preview0["batch_id"], "group_id": group0["group_id"], "dataset_id": fy24_id},
    )

    # One batch: FY24 (existing) + FY25 + FY26 (both brand new).
    preview = _post_files(client, fy24_files + fy25_files + fy26_files).get_json()
    ready = [g for g in preview["groups"] if g["status"] == "ready"]
    assert len(ready) == 3

    existing_group = next(g for g in ready if g["key_exists"] is True)
    new_groups = [g for g in ready if g["key_exists"] is False]
    assert existing_group["detected_dataset_id"] == fy24_id
    assert len(new_groups) == 2

    for g in new_groups:  # the bulk loop's own filter, applied here explicitly
        confirm = client.post(
            "/upload/confirm",
            json={"batch_id": preview["batch_id"], "group_id": g["group_id"], "dataset_id": g["detected_dataset_id"]},
        )
        assert confirm.status_code == 200

    ids = [d["id"] for d in client.get("/api/datasets").get_json()]
    for g in new_groups:
        assert g["detected_dataset_id"] in ids

    # The existing-key group was never posted to /upload/confirm by the bulk
    # loop -- its on-disk row_count still matches the original baseline, not
    # a re-parse of this batch's (possibly different) FY24 files.
    meta = store.get_dataset_meta(fy24_id)
    assert meta["row_count"] == group0["row_count"]

    # It only updates once confirmed individually, same guarantee as above.
    confirm_existing = client.post(
        "/upload/confirm",
        json={"batch_id": preview["batch_id"], "group_id": existing_group["group_id"], "dataset_id": fy24_id},
    )
    assert confirm_existing.status_code == 200


def test_plain_single_year_three_file_upload_still_works(client, fy26_file_paths, real_fy26_dataset):
    """Regression guard: the old 3-file, single-year case is just the
    1-group case of the same content-based grouping path, not a special
    case that could silently regress while multi-year batches are added."""
    net, gross, host = fy26_file_paths
    preview = _post_files(client, [net, gross, host]).get_json()
    assert len(preview["groups"]) == 1
    group = preview["groups"][0]
    assert group["status"] == "ready"

    confirm = client.post(
        "/upload/confirm",
        json={"batch_id": preview["batch_id"], "group_id": group["group_id"], "dataset_id": group["detected_dataset_id"]},
    )
    assert confirm.status_code == 200

    dash = client.get(f"/api/dashboard?ds={group['detected_dataset_id']}").get_json()
    assert dash["kpis"]["total_events"] == len(real_fy26_dataset.bookings)
