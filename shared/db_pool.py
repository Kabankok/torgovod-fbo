"""Per-company SQLite connection factory with LRU initialization tracking.

Design:
    - One file per company: data/company_{id}.db
    - Company context propagated via contextvars (safe in asyncio/FastAPI)
    - Schema applied once per company per process lifetime (LRU, max 50)
    - Each call returns a NEW connection — caller is responsible for closing it

Usage:
    # Middleware sets context once per request:
    set_company_context(user["company_id"])

    # Storage functions pick it up automatically:
    from shared.db_pool import get_current_company_id, get_company_db
    cid = get_current_company_id()
    if cid:
        conn = get_company_db(cid)
"""

from __future__ import annotations

import contextvars
import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

_company_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "company_id", default=None
)

_lock = threading.Lock()
_initialized: OrderedDict[str, bool] = OrderedDict()
_MAX_CACHED = 50

_DATA_DIR = Path("data")


def set_company_context(company_id: str | None) -> None:
    """Set current company ID for this async task / thread."""
    _company_ctx.set(company_id)


def get_current_company_id() -> str | None:
    """Return company ID set for this context, or None (background tasks)."""
    return _company_ctx.get()


def get_company_db(company_id: str) -> sqlite3.Connection:
    """Open WAL connection to data/company_{company_id}.db.

    Schema is applied on first access per company (tracked in LRU cache of 50).
    Returns a new connection each call — caller must close it.
    """
    db_path = _DATA_DIR / f"company_{company_id}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA foreign_keys=ON")

    with _lock:
        if company_id not in _initialized:
            from shared.company_schema import apply_company_schema

            apply_company_schema(conn)
            if len(_initialized) >= _MAX_CACHED:
                _initialized.popitem(last=False)
            _initialized[company_id] = True

    return conn


def mark_uninitialized(company_id: str) -> None:
    """Force schema re-application on next access (call after migration)."""
    with _lock:
        _initialized.pop(company_id, None)
