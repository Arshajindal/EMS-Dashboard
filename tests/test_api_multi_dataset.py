import pandas as pd

from app.models import store


class _FakeValidation:
    def __init__(self):
        self.warnings = []
        self.errors = []

    def to_dict(self):
        return {"warnings": self.warnings, "errors": self.errors}


def _bookings_df(gross_values):
    n = len(gross_values)
    return pd.DataFrame({
        "Gross Sales":  gross_values,
        "Net Sales":    [g * 0.8 for g in gross_values],
        "discount":     [g * 0.1 for g in gross_values],
        "host":         [f"Host {i % 2}" for i in range(n)],
        "duration_hrs": [2.0] * n,
        "month":        ["2021-01"] * n,
    })


def _host_summary_df(n=2):
    return pd.DataFrame({
        "host_type":    ["Internal"] * n,
        "setup_count":  [1] * n,
        "attendance":   [10] * n,
    })


def _seed_two_datasets():
    id_a = store.save_dataset(
        _bookings_df([100.0, 200.0]), _host_summary_df(),
        "7/1/2020 thru 6/30/2021", _FakeValidation(),
    )
    id_b = store.save_dataset(
        _bookings_df([1000.0, 2000.0, 3000.0]), _host_summary_df(),
        "7/1/2021 thru 6/30/2022", _FakeValidation(),
    )
    return id_a, id_b


def test_datasets_are_independent(app, client):
    with app.app_context():
        id_a, id_b = _seed_two_datasets()

    resp_a = client.get(f"/api/kpis?ds={id_a}")
    resp_b = client.get(f"/api/kpis?ds={id_b}")

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200

    kpis_a = resp_a.get_json()
    kpis_b = resp_b.get_json()

    assert kpis_a["total_gross_sales"] == 300.0
    assert kpis_b["total_gross_sales"] == 6000.0
    assert kpis_a["total_events"] == 2
    assert kpis_b["total_events"] == 3


def test_omitting_ds_resolves_to_most_recent(app, client):
    with app.app_context():
        id_a, id_b = _seed_two_datasets()

    resp = client.get("/api/kpis")
    assert resp.status_code == 200
    assert resp.get_json()["total_gross_sales"] == 6000.0  # id_b, saved last


def test_unknown_dataset_id_returns_404(app, client):
    resp = client.get("/api/kpis?ds=FY-does-not-exist")
    assert resp.status_code == 404


def test_datasets_endpoint_lists_both(app, client):
    with app.app_context():
        id_a, id_b = _seed_two_datasets()

    resp = client.get("/api/datasets")
    assert resp.status_code == 200
    ids = [d["id"] for d in resp.get_json()]
    assert id_a in ids and id_b in ids
