"""Sync sales analytics to `sku_analytics_daily` table.

Uses analytics_data_full() which makes 2 API requests:
  Batch 1: 14 metrics (visibility, sessions, cart conversions)
  Batch 2: 4 metrics (returns, cancellations, delivered_units, position_category)
"""

from __future__ import annotations

import sqlite3
from datetime import date

from shared.api.seller import SellerClient

# All columns written to sku_analytics_daily
_ALL_COLS = [
    "revenue",
    "ordered_units",
    "hits_view",
    "hits_view_search",
    "hits_view_pdp",
    "session_view",
    "session_view_search",
    "session_view_pdp",
    "hits_tocart",
    "hits_tocart_search",
    "hits_tocart_pdp",
    "conv_tocart",
    "conv_tocart_search",
    "conv_tocart_pdp",
    "delivered_units",
    "returns",
    "cancellations",
    "position_category",
]


def sync_sales(
    conn: sqlite3.Connection,
    client: SellerClient,
    date_from: date,
    date_to: date,
) -> int:
    """Fetch all analytics metrics by SKU+day and upsert. Returns row count."""
    print(f"[sales] Fetching analytics {date_from} to {date_to} ...")
    rows = client.analytics_data_full(date_from, date_to)

    # Ensure all expected columns present in each row
    for r in rows:
        for col in _ALL_COLS:
            r.setdefault(col, None)

    cols_sql = ", ".join(_ALL_COLS)
    placeholders = ", ".join(f":{c}" for c in _ALL_COLS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _ALL_COLS)

    with conn:
        conn.executemany(
            f"""
            INSERT INTO sku_analytics_daily (sku, date, {cols_sql})
            VALUES (:sku, :date, {placeholders})
            ON CONFLICT(sku, date) DO UPDATE SET {updates}
            """,
            rows,
        )

    print(f"[sales] Upserted {len(rows)} rows")
    return len(rows)
