import os
from pathlib import Path

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")

from app import create_app
from app.utils.parser import parse_ems_files

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture
def app(tmp_path):
    db_path = str(tmp_path / "test_datasets.db")
    application = create_app(database_path=db_path)
    application.config["TESTING"] = True
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(scope="session")
def fy26_file_paths():
    """Locates the real /data FY26 export files (same globs upload.py's load_demo() uses)."""
    net = next(iter(
        list(DATA_DIR.glob("*Net*Sales*Booking*.xlsx")) + list(DATA_DIR.glob("*net*.xlsx"))
    ))
    gross = next(iter(
        list(DATA_DIR.glob("*Gross*Sales*Booking*.xlsx")) + list(DATA_DIR.glob("*gross*booking*.xlsx"))
    ))
    host = next(iter(
        list(DATA_DIR.glob("*Host*.xlsx")) + list(DATA_DIR.glob("*host*.xlsx"))
    ))
    return net, gross, host


@pytest.fixture(scope="session")
def real_fy26_dataset(fy26_file_paths):
    """
    Parses the actual /data FY26 export exactly once for the whole test
    session. Real fixtures, not synthetic data — this is what exercises the
    real net/gross reconciliation guard in parser.py.
    """
    net, gross, host = fy26_file_paths
    return parse_ems_files(net_path=net, gross_path=gross, host_path=host)


@pytest.fixture(scope="session")
def trimmed_fy26_file_paths(tmp_path_factory, fy26_file_paths):
    """
    A second, genuinely-different-but-still-valid real file set: the actual
    Net/Gross workbooks with a trailing chunk of data rows deleted (headers
    and merged-cell blocks untouched — deletion only touches rows well past
    them). Used by upload-flow tests that need two uploads with provably
    different totals without a second real fiscal year on disk. Row deletion
    (not cell-value editing) keeps this safe: same schema, same reporting
    period text, real reconciliation guard still exercised on real data —
    just fewer bookings.
    """
    import openpyxl

    net, gross, host = fy26_file_paths
    out_dir = tmp_path_factory.mktemp("trimmed_fy26")
    trim = 2000

    trimmed = {}
    for label, path in [("net", net), ("gross", gross)]:
        wb = openpyxl.load_workbook(path)
        ws = wb.active
        start_row = ws.max_row - trim + 1
        ws.delete_rows(start_row, trim)
        out_path = out_dir / f"{label}_trimmed.xlsx"
        wb.save(out_path)
        trimmed[label] = out_path

    return trimmed["net"], trimmed["gross"], host
