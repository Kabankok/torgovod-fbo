"""Sync product catalog to `products` table.

Calls /v3/product/list + /v3/product/info/list.
Upserts: sku, product_id, offer_id, name, price, old_price.

Race-window с wizard'ом модерации: wizard заводит строку с временным
`sku='pending:<offer_id>'`, ждёт когда Ozon снимет модерацию и
moderation_watcher заменит sku на реальный. Если catalog_sync отработает
между этими событиями — он INSERT'нёт ВТОРУЮ строку (тот же offer_id,
но другой sku=реальный), и в таблице образуется дубль. Поэтому перед
батч-апсертом мы удаляем pending-строки по списку offer_id, которые
пришли от Ozon — реальная строка от catalog их заменяет (см. фикс B3
аудита plans/2026-06-03-product-creation-wizard.md).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from shared.api.seller import SellerClient

logger = logging.getLogger(__name__)


def sync_catalog(conn: sqlite3.Connection, client: SellerClient) -> int:
    """Fetch all products and upsert to products table. Returns count of upserted rows."""
    logger.info("[catalog] Fetching product catalog...")
    products = client.list_all_products()
    now = datetime.now(UTC).isoformat()

    # Снимаем pending-двойников: Ozon вернул реальный sku по offer_id,
    # значит wizard'овская строка-заглушка sku='pending:<offer_id>' больше не нужна.
    # Делаем это ПЕРЕД INSERT'ом — иначе будет два ряда на один offer_id.
    offer_ids = [p.get("offer_id") for p in products if p.get("offer_id")]

    with conn:
        if offer_ids:
            placeholders = ",".join("?" for _ in offer_ids)
            conn.execute(
                f"DELETE FROM products WHERE sku LIKE 'pending:%' AND offer_id IN ({placeholders})",
                offer_ids,
            )

        conn.executemany(
            """
            INSERT INTO products (sku, product_id, offer_id, name, price, old_price, image_url, is_archived, updated_at)
            VALUES (:sku, :product_id, :offer_id, :name, :price, :old_price, :image_url, :is_archived, :updated_at)
            ON CONFLICT(sku) DO UPDATE SET
                product_id  = excluded.product_id,
                offer_id    = excluded.offer_id,
                name        = excluded.name,
                price       = excluded.price,
                old_price   = excluded.old_price,
                image_url   = excluded.image_url,
                is_archived = excluded.is_archived,
                updated_at  = excluded.updated_at
            """,
            [{**p, "is_archived": p.get("is_archived", 0), "updated_at": now} for p in products],
        )

    logger.info("[catalog] Upserted %d products", len(products))
    return len(products)
