import time

import pandas as pd

from app.models import store


def _df(n=3):
    return pd.DataFrame({"a": range(n), "b": [f"row{i}" for i in range(n)]})


class _FakeValidation:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def to_dict(self):
        return {"warnings": self.warnings, "errors": self.errors}


def test_derive_dataset_id_two_years(app):
    assert store.derive_dataset_id("7/1/2025 thru 6/30/2026") == "FY2025-2026"


def test_derive_dataset_id_one_year(app):
    assert store.derive_dataset_id("Fiscal Year 2025 Summary") == "FY2025"


def test_derive_dataset_id_fallback_hash(app):
    dataset_id = store.derive_dataset_id("no years mentioned here")
    assert dataset_id.startswith("FY-")


def test_save_get_list_delete_round_trip(app):
    bookings = _df(5)
    host_summary = _df(2)
    dataset_id = store.save_dataset(
        bookings, host_summary, "7/1/2020 thru 6/30/2021", _FakeValidation(),
        source_files=["a.xlsx"],
    )
    assert dataset_id == "FY2020-2021"

    fetched = store.get_dataset(dataset_id)
    assert fetched is not None
    pd.testing.assert_frame_equal(fetched["bookings"], bookings)
    pd.testing.assert_frame_equal(fetched["host_summary"], host_summary)
    assert fetched["source_files"] == ["a.xlsx"]

    all_ids = [d["id"] for d in store.list_datasets()]
    assert dataset_id in all_ids

    assert store.delete_dataset(dataset_id) is True
    assert store.get_dataset(dataset_id) is None
    assert store.dataset_exists(dataset_id) is False


def test_reupload_upserts_in_place(app):
    period = "7/1/2022 thru 6/30/2023"
    id1 = store.save_dataset(_df(3), _df(1), period, _FakeValidation())
    meta1 = store.get_dataset_meta(id1)

    time.sleep(0.01)
    id2 = store.save_dataset(_df(9), _df(1), period, _FakeValidation())
    meta2 = store.get_dataset_meta(id2)

    assert id1 == id2
    assert meta2["row_count"] == 9
    assert meta2["updated_at"] >= meta1["updated_at"]
    assert meta2["created_at"] == meta1["created_at"]

    ids = [d["id"] for d in store.list_datasets()]
    assert ids.count(id1) == 1


def test_cache_invalidation_scoped_to_reuploaded_key(app):
    id_a = store.save_dataset(_df(2), _df(1), "7/1/2018 thru 6/30/2019", _FakeValidation())
    id_b = store.save_dataset(_df(2), _df(1), "7/1/2019 thru 6/30/2020", _FakeValidation())

    store.set_dashboard_cache(id_a, {"payload": "a"})
    store.set_dashboard_cache(id_b, {"payload": "b"})

    # Re-saving dataset A must only invalidate A's cache entry.
    store.save_dataset(_df(4), _df(1), "7/1/2018 thru 6/30/2019", _FakeValidation())

    assert store.get_dashboard_cache(id_a) is None
    assert store.get_dashboard_cache(id_b) == {"payload": "b"}


def test_most_recent_dataset_id(app):
    id_a = store.save_dataset(_df(1), _df(1), "7/1/2015 thru 6/30/2016", _FakeValidation())
    time.sleep(0.01)
    id_b = store.save_dataset(_df(1), _df(1), "7/1/2016 thru 6/30/2017", _FakeValidation())

    assert store.most_recent_dataset_id() == id_b
    assert id_a != id_b
