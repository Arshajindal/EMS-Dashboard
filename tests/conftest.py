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
