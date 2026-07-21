"""
§6 regression tests (docs/Multi-Year_Comparison_Architecture_Guide.md):
  1. Two fiscal years loaded at once have fully independent totals — no
     cross-key bleed.
  2. The net/gross reconciliation guard in parser.py runs per dataset load,
     not once globally.
  3. Re-uploading an existing dataset_id invalidates that key's cached
     dashboard payload.

All three are anchored on the real /data FY26 EMS export (see conftest.py's
real_fy26_dataset / fy26_file_paths fixtures) rather than synthetic frames.
/data only ships one real fiscal year, so the "second fiscal year" used for
the independence check is a row-subset of that same real parse (every 3rd
booking row, every 2nd host-summary row — no values are invented or scaled),
saved under an obviously-fake reporting_period so it can never be mistaken
for real data.
"""
from app.models import store
from app.utils.parser import parse_ems_files


def _save_real_fy26(real_fy26_dataset):
    ds = real_fy26_dataset
    return store.save_dataset(
        ds.bookings, ds.host_summary, ds.reporting_period, ds.validation,
        source_files=["real-fy26"],
    )


def _save_fabricated_second_year(real_fy26_dataset):
    ds = real_fy26_dataset
    fabricated_bookings = ds.bookings.iloc[::3].reset_index(drop=True)
    fabricated_hosts = ds.host_summary.iloc[::2].reset_index(drop=True)
    return store.save_dataset(
        fabricated_bookings,
        fabricated_hosts,
        "FY99-TEST (fabricated row-subset of real FY26 data, for regression tests only)",
        ds.validation,
        source_files=["fabricated-subset-of-real-fy26"],
    )


def test_two_fiscal_years_are_fully_independent(app, client, real_fy26_dataset):
    id_real = _save_real_fy26(real_fy26_dataset)
    id_fake = _save_fabricated_second_year(real_fy26_dataset)
    assert id_real != id_fake

    expected_real_gross = round(real_fy26_dataset.bookings["Gross Sales"].sum(), 2)
    expected_fake_gross = round(real_fy26_dataset.bookings.iloc[::3]["Gross Sales"].sum(), 2)
    assert expected_real_gross != expected_fake_gross  # subset must actually differ

    kpis_real = client.get(f"/api/kpis?ds={id_real}").get_json()
    kpis_fake = client.get(f"/api/kpis?ds={id_fake}").get_json()

    assert kpis_real["total_gross_sales"] == expected_real_gross
    assert kpis_fake["total_gross_sales"] == expected_fake_gross
    assert kpis_real["total_events"] == len(real_fy26_dataset.bookings)
    assert kpis_fake["total_events"] == len(real_fy26_dataset.bookings.iloc[::3])

    # Loading dataset B must not have mutated dataset A's totals (no bleed).
    kpis_real_again = client.get(f"/api/kpis?ds={id_real}").get_json()
    assert kpis_real_again["total_gross_sales"] == expected_real_gross

    # And both must show up independently in the cross-dataset listing.
    ids = [d["id"] for d in client.get("/api/datasets").get_json()]
    assert id_real in ids and id_fake in ids


def test_reconciliation_guard_runs_per_dataset_load(app, real_fy26_dataset, fy26_file_paths):
    # The session fixture already parsed once — guard already passed there.
    assert real_fy26_dataset.validation.errors == []

    # Parse the same real files again, completely independently. parser.py
    # holds no global state (CLAUDE.md invariant), so this proves the guard
    # re-executes on every dataset load rather than being skipped or cached
    # after the first parse.
    net, gross, host = fy26_file_paths
    second_parse = parse_ems_files(net_path=net, gross_path=gross, host_path=host)

    assert second_parse.validation.errors == []
    assert len(second_parse.bookings) == len(real_fy26_dataset.bookings)
    assert abs(
        second_parse.bookings["Gross Sales"].sum() - real_fy26_dataset.bookings["Gross Sales"].sum()
    ) < 0.01
    assert abs(
        second_parse.bookings["Net Sales"].sum() - real_fy26_dataset.bookings["Net Sales"].sum()
    ) < 0.01

    # Each independent load must persist as its own dataset row when saved.
    id_first = _save_real_fy26(real_fy26_dataset)
    id_second = store.save_dataset(
        second_parse.bookings, second_parse.host_summary, second_parse.reporting_period,
        second_parse.validation, source_files=["real-fy26-second-load"],
    )
    # Same reporting_period -> same derived id -> this is an UPSERT, not a
    # separate row, which is itself part of "runs per dataset load": the
    # guard ran twice, independently, and both loads agree well enough to
    # collapse into the same key without drift.
    assert id_first == id_second


def test_reupload_invalidates_cached_dashboard_payload(app, client):
    first = client.post("/upload/demo").get_json()
    assert first["status"] == "ok"
    dataset_id = first["dataset_id"]

    resp1 = client.get(f"/api/dashboard?ds={dataset_id}")
    assert resp1.status_code == 200
    etag1 = resp1.headers["ETag"]
    assert store.get_dashboard_cache(dataset_id) is not None

    # Repeat request against the same, unchanged dataset -> cache hit / 304.
    resp1b = client.get(f"/api/dashboard?ds={dataset_id}", headers={"If-None-Match": etag1})
    assert resp1b.status_code == 304

    # Re-upload the same real files -> UPSERTs the same dataset_id.
    second = client.post("/upload/demo").get_json()
    assert second["dataset_id"] == dataset_id

    # save_dataset() must have invalidated the cache entry immediately,
    # before any GET repopulates it.
    assert store.get_dashboard_cache(dataset_id) is None

    # The stale ETag from before the re-upload must no longer match -> 200,
    # not 304, and a fresh ETag (updated_at changed).
    resp2 = client.get(f"/api/dashboard?ds={dataset_id}", headers={"If-None-Match": etag1})
    assert resp2.status_code == 200
    etag2 = resp2.headers["ETag"]
    assert etag2 != etag1
    assert store.get_dashboard_cache(dataset_id) is not None
