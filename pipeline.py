"""Полный синк данных для «Торговод · Отгрузки FBO».

Пирамида данных FBO:
    1. catalog → products            (каталог товаров из Ozon)
    2. sales   → sku_analytics_daily (продажи по дням — основа ABC и рекомендаций)
    3. run_full_sync                 (остатки по складам, кластеры, FBO-постинги,
                                      оборачиваемость и расчёт рекомендаций)

Эту же функцию дёргает кнопка «Обновить» в интерфейсе, поэтому один клик
наполняет все три слоя.

CLI:
    python pipeline.py            # последние 60 дней
    python pipeline.py --days 30
"""

from __future__ import annotations

import logging
from datetime import date, timedelta


def run_pipeline(days: int = 60) -> None:
    from modules.analytics.catalog import sync_catalog
    from modules.analytics.sales import sync_sales
    from modules.fbo.sync import run_full_sync
    from shared.api.seller import SellerClient
    from shared.db.models import get_connection, init_db

    today = date.today()
    date_from = today - timedelta(days=days - 1)

    logger = logging.getLogger("pipeline")
    logger.info("=== Полный синк FBO: %s … %s (%d дн.) ===", date_from, today, days)

    conn = get_connection()
    init_db(conn)
    seller = SellerClient()
    try:
        logger.info("[1/3] Каталог товаров…")
        sync_catalog(conn, seller)
        logger.info("[2/3] Продажи по дням…")
        sync_sales(conn, seller, date_from, today)
    finally:
        conn.close()

    logger.info("[3/3] Остатки, кластеры, FBO-постинги, рекомендации…")
    run_full_sync(company_id=None)
    logger.info("=== Готово ===")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Полный синк данных FBO из Ozon")
    parser.add_argument("--days", type=int, default=60, help="Сколько дней истории тянуть (по умолчанию 60)")
    args = parser.parse_args()
    run_pipeline(days=args.days)
