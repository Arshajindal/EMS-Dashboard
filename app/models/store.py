"""
In-memory data store.
Keeps the last-parsed dataset available across requests without a database.
For multi-worker deployments, swap this out for Redis or SQLite.
"""
from __future__ import annotations
import threading
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional

_lock = threading.Lock()


@dataclass
class DataStore:
    bookings: pd.DataFrame = field(default_factory=pd.DataFrame)
    host_summary: pd.DataFrame = field(default_factory=pd.DataFrame)
    reporting_period: str = ""
    validation: dict = field(default_factory=dict)
    loaded: bool = False
    source_files: list = field(default_factory=list)


_store = DataStore()


def get_store() -> DataStore:
    return _store


def set_store(
    bookings: pd.DataFrame,
    host_summary: pd.DataFrame,
    reporting_period: str,
    validation,
    source_files: Optional[list] = None,
):
    global _store
    with _lock:
        _store = DataStore(
            bookings=bookings,
            host_summary=host_summary,
            reporting_period=reporting_period,
            validation=validation.to_dict() if hasattr(validation, "to_dict") else validation,
            loaded=True,
            source_files=source_files or [],
        )


def is_loaded() -> bool:
    return _store.loaded


def clear_store():
    global _store
    with _lock:
        _store = DataStore()
