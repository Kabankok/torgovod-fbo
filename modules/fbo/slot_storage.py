"""FBO Slot Hunter: DB schema and CRUD helpers.

Separate DB: data/slot_hunter.db (override with SLOT_HUNTER_DB_PATH env var).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

SLOT_HUNTER_DB_PATH = Path(os.getenv("SLOT_HUNTER_DB_PATH", "data/slot_hunter.db"))

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS slot_hunter_jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    supply_order_id  INTEGER NOT NULL,
    supply_order_num TEXT DEFAULT '',
    target_date_from TEXT NOT NULL,
    target_date_to   TEXT NOT NULL,
    target_time_from TEXT NOT NULL,
    target_time_to   TEXT NOT NULL,
    interval_sec     INTEGER DEFAULT 60,
    status           TEXT DEFAULT 'active',
    found_slot_from  TEXT,
    found_slot_to    TEXT,
    checks_count     INTEGER DEFAULT 0,
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS slot_hunter_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES slot_hunter_jobs(id),
    event_type  TEXT NOT NULL,
    slots_count INTEGER DEFAULT 0,
    message     TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_slot_events_job ON slot_hunter_events(job_id, created_at DESC);
"""


def get_slot_connection(company_id: str | None = None) -> sqlite3.Connection:
    from shared.db_pool import get_company_db, get_current_company_id

    cid = company_id or get_current_company_id()
    if cid:
        return get_company_db(cid)
    SLOT_HUNTER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SLOT_HUNTER_DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_slot_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def create_job(
    conn: sqlite3.Connection,
    supply_order_id: int,
    supply_order_num: str,
    target_date_from: str,
    target_date_to: str,
    target_time_from: str,
    target_time_to: str,
    interval_sec: int = 60,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO slot_hunter_jobs
            (supply_order_id, supply_order_num,
             target_date_from, target_date_to,
             target_time_from, target_time_to,
             interval_sec, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (
            supply_order_id,
            supply_order_num,
            target_date_from,
            target_date_to,
            target_time_from,
            target_time_to,
            interval_sec,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def list_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM slot_hunter_jobs ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM slot_hunter_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def get_active_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM slot_hunter_jobs WHERE status = 'active'").fetchall()
    return [dict(r) for r in rows]


def update_job_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    found_slot_from: str | None = None,
    found_slot_to: str | None = None,
) -> None:
    now = datetime.utcnow().isoformat()
    # COALESCE, not a plain assignment: callers that only change the status (pause, resume,
    # stop) pass no slot and would otherwise erase the slot the hunter had already found.
    conn.execute(
        """
        UPDATE slot_hunter_jobs
           SET status = ?,
               found_slot_from = COALESCE(?, found_slot_from),
               found_slot_to   = COALESCE(?, found_slot_to),
               updated_at = ?
         WHERE id = ?
        """,
        (status, found_slot_from, found_slot_to, now, job_id),
    )
    conn.commit()


def increment_checks(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE slot_hunter_jobs SET checks_count = checks_count + 1, updated_at = ? WHERE id = ?",
        (datetime.utcnow().isoformat(), job_id),
    )
    conn.commit()


def add_event(
    conn: sqlite3.Connection,
    job_id: int,
    event_type: str,
    slots_count: int = 0,
    message: str = "",
) -> None:
    conn.execute(
        "INSERT INTO slot_hunter_events (job_id, event_type, slots_count, message) VALUES (?, ?, ?, ?)",
        (job_id, event_type, slots_count, message),
    )
    conn.commit()


def get_events(conn: sqlite3.Connection, job_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM slot_hunter_events WHERE job_id = ? ORDER BY created_at DESC LIMIT ?",
        (job_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_job(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("DELETE FROM slot_hunter_events WHERE job_id = ?", (job_id,))
    conn.execute("DELETE FROM slot_hunter_jobs WHERE id = ?", (job_id,))
    conn.commit()
