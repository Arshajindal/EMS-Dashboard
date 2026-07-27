"""
Tests for the upload preview/confirm flow (POST /upload/files ->
POST /upload/confirm), which replaced the old single-request
parse-and-persist in /upload/files. Everything here goes through the real
HTTP endpoints with real /data fixtures, not direct store.save_dataset()
calls — this is what actually exercises _locate_batch_files(), the batch
folder lifecycle, and the caller-supplied dataset_id path added in this
phase.
"""
from io import BytesIO

from app.models import store


def _post_files(client, paths):
    data = {"files": [(BytesIO(p.read_bytes()), p.name) for p in paths]}
    return client.post("/upload/files", data=data, content_type="multipart/form-data")


def test_confirm_new_dataset_id_persists_lists_and_matches_direct_parse(
    client, fy26_file_paths, real_fy26_dataset
):
    preview = _post_files(client, fy26_file_paths).get_json()
    assert preview["key_exists"] is False  # nothing persisted at preview stage
    dataset_id = preview["detected_dataset_id"]

    confirm = client.post(
        "/upload/confirm",
        json={"batch_id": preview["batch_id"], "dataset_id": dataset_id},
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
    dataset_id = preview1["detected_dataset_id"]
    client.post("/upload/confirm", json={"batch_id": preview1["batch_id"], "dataset_id": dataset_id})

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
    assert preview2["key_exists"] is True
    assert preview2["detected_dataset_id"] == dataset_id
    assert preview2["existing"]["row_count"] == rows1

    confirm2 = client.post(
        "/upload/confirm",
        json={"batch_id": preview2["batch_id"], "dataset_id": dataset_id},
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
    exactly the "edit the detected label before saving" path this phase
    added for real users.
    """
    preview_a = _post_files(client, fy26_file_paths).get_json()
    id_a = preview_a["detected_dataset_id"]
    client.post("/upload/confirm", json={"batch_id": preview_a["batch_id"], "dataset_id": id_a})

    preview_b = _post_files(client, trimmed_fy26_file_paths).get_json()
    id_b = "FY99-TEST-UPLOAD-FLOW"
    assert preview_b["detected_dataset_id"] != id_b  # confirms the override actually diverges
    client.post("/upload/confirm", json={"batch_id": preview_b["batch_id"], "dataset_id": id_b})

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
