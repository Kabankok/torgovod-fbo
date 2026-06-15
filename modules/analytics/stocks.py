"""Sync stock levels to `sku_stocks_daily` and `sku_stocks_by_warehouse` tables.

Two API calls:
  1. /v4/product/info/stocks  -> sku_stocks_daily (FBO + FBS aggregates)
  2. /v2/analytics/stock_on_warehouses (FBO then FBS) -> sku_stocks_by_warehouse
     Real warehouse names: "Студенческий", "Офис", Ozon FBO warehouses, etc.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from shared.api.seller import SellerClient


def sync_stocks(
    conn: sqlite3.Connection,
    client: SellerClient,
    today: date,
) -> int:
    """Snapshot today's stock levels. Returns count of SKUs processed."""
    date_str = today.isoformat()

    # ── Step 1: aggregates from /v4/product/info/stocks ──────────────────────
    # Архивные (local_archived=1) исключаем — они в ручном архиве пользователя,
    # API-запросы по ним не делаем, остатки не обновляем.
    rows_map = conn.execute(
        "SELECT sku, offer_id FROM products "
        "WHERE offer_id IS NOT NULL AND offer_id != '' "
        "AND COALESCE(local_archived, 0) = 0"
    ).fetchall()

    if not rows_map:
        print("[stocks] No products in catalog -- run catalog sync first")
        return 0

    offer_ids = [r["offer_id"] for r in rows_map]
    offer_to_sku = {r["offer_id"]: r["sku"] for r in rows_map}

    print(f"[stocks] Fetching FBO/FBS aggregates for {len(offer_ids)} products...")
    detailed = client.get_stocks_detailed(offer_ids)
    print(f"[stocks] Ozon returned {len(detailed)} of {len(offer_ids)} offer_ids")

    daily_rows = []
    seen_skus: set[str] = set()
    for offer_id, entries in detailed.items():
        sku = offer_to_sku.get(offer_id)
        if not sku:
            continue
        fbo = sum(e["present"] for e in entries if e["warehouse_type"] == "fbo")
        fbs = sum(e["present"] for e in entries if e["warehouse_type"] in ("fbs", "rfbs"))
        daily_rows.append({"sku": sku, "date": date_str, "stock_fbo": fbo, "stock_fbs": fbs})
        seen_skus.add(str(sku))

    # Не вернувшиеся SKU = остаток 0 (товар не виден на Ozon с visibility=ALL).
    # Без этого UI показывает stale-запись месячной давности и логика «закончился»
    # не срабатывает. Защита: если Ozon вернул < 90% запрошенного (явный сбой
    # вместо реально архивных товаров) — нули не пишем, ждём следующего синка.
    missing = [str(r["sku"]) for r in rows_map if str(r["sku"]) not in seen_skus]
    coverage = len(detailed) / max(1, len(offer_ids))
    if missing and coverage >= 0.9:
        for sku in missing:
            daily_rows.append({"sku": sku, "date": date_str, "stock_fbo": 0, "stock_fbs": 0})
        print(f"[stocks] Zero-filled {len(missing)} missing SKUs (Ozon coverage {coverage:.0%})")
    elif missing:
        print(
            f"[stocks] SKIP zero-fill for {len(missing)} missing SKUs — "
            f"low coverage {coverage:.0%}, suspect API failure"
        )

    with conn:
        conn.executemany(
            """
            INSERT INTO sku_stocks_daily (sku, date, stock_fbo, stock_fbs)
            VALUES (:sku, :date, :stock_fbo, :stock_fbs)
            ON CONFLICT(sku, date) DO UPDATE SET
                stock_fbo = excluded.stock_fbo,
                stock_fbs = excluded.stock_fbs
            """,
            daily_rows,
        )
    print(f"[stocks] Upserted {len(daily_rows)} rows to sku_stocks_daily")

    # ── Step 2: named warehouses from /v2/analytics/stock_on_warehouses ──────
    # Note: this endpoint shows Ozon's own fulfillment warehouses (FBO network).
    # Seller's own FBS warehouses ("Студенческий", "Офис") are NOT returned here —
    # Ozon's analytics API has no endpoint for seller warehouse names.
    # FBS totals are captured in sku_stocks_daily above.
    print("[stocks] Fetching per-warehouse breakdown (ALL)...")
    all_data = client.get_stock_on_warehouses("ALL")
    print(f"[stocks]   {len(all_data)} SKUs across Ozon network")

    merged = all_data

    warehouse_rows = []
    for sku, entries in merged.items():
        for e in entries:
            warehouse_rows.append(
                {
                    "sku": sku,
                    "date": date_str,
                    "warehouse_name": e["warehouse_name"],
                    "warehouse_type": e["warehouse_type"],
                    "present": e["present"],
                    "promised": e["promised"],
                    "reserved": e["reserved"],
                }
            )

    with conn:
        conn.executemany(
            """
            INSERT INTO sku_stocks_by_warehouse
                (sku, date, warehouse_name, warehouse_type, present, promised, reserved)
            VALUES (:sku, :date, :warehouse_name, :warehouse_type, :present, :promised, :reserved)
            ON CONFLICT(sku, date, warehouse_name) DO UPDATE SET
                warehouse_type = excluded.warehouse_type,
                present        = excluded.present,
                promised       = excluded.promised,
                reserved       = excluded.reserved
            """,
            warehouse_rows,
        )

    wnames = sorted({r["warehouse_name"] for r in warehouse_rows})
    print(f"[stocks] Upserted {len(warehouse_rows)} warehouse entries for {date_str}")
    print(f"[stocks] Warehouses: {wnames}")

    return len(daily_rows)
