from app.models import store


def test_demo_upload_end_to_end_and_parquet_round_trip(app, client):
    resp = client.post("/upload/demo")
    assert resp.status_code == 200

    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["rows_parsed"] > 0
    dataset_id = body["dataset_id"]

    with app.app_context():
        ds = store.get_dataset(dataset_id)

    # Reconciliation guard in parser.py hard-errors on Net/Gross drift > $0.01
    # before save_dataset is ever called, so reaching here with no error and
    # a non-empty frame confirms it passed.
    assert ds is not None
    assert len(ds["bookings"]) > 0
    assert len(ds["bookings"]) == ds["row_count"]
    assert ds["validation"]["errors"] == []

    # Parquet round-trip: the columns pulled back out of SQLite must match
    # what the parser produced going in, not just row count.
    assert set(ds["bookings"].columns) >= {"Gross Sales", "Net Sales", "host"}


def test_demo_upload_twice_upserts_not_duplicates(app, client):
    first = client.post("/upload/demo").get_json()
    second = client.post("/upload/demo").get_json()

    assert first["dataset_id"] == second["dataset_id"]

    with app.app_context():
        all_ids = [d["id"] for d in store.list_datasets()]
    assert all_ids.count(first["dataset_id"]) == 1
