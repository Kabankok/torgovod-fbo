"""Полный синк данных для «Торговод · Отгрузки FBO».

Пирамида данных FBO:
    1. catalog → products            (каталог товаров из Ozon)
    2. sales   → sku_analytics_daily (продажи по дням — основа ABC и рекомендаций)
    3. run_full_sync                 (остатки по складам, кластеры, FBO-постинги,
                                      оборачиваемость и расчёт рекомендаций)

Эту же функцию дёргает кнопка «Обновить» в интерфейсе, поэтому один клик
наполняет все три слоя. Каждый шаг пишет статус в fbo_sync_status, чтобы
интерфейс показывал живой прогресс (каталог → продажи → остатки → расчёт),
а не молчаливую крутилку. Шаг, упавший с ошибкой, не обрывает остальные.

FBO-расчёту из продаж нужны только revenue и ordered_units, поэтому тянем
лишь базовые метрики (basic_only) — втрое меньше запросов и без зависимости
от подписки Premium.

CLI:
    python pipeline.py            # последние 35 дней
    python pipeline.py --days 30
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta


def run_pipeline(days: int = 35) -> None:
    from modules.analytics.catalog import sync_catalog
    from modules.analytics.sales import sync_sales
    from modules.fbo.storage import get_fbo_connection, init_fbo_db, upsert_sync_status
    from modules.fbo.sync import run_full_sync
    from shared.api.seller import SellerClient
    from shared.db.models import get_connection, init_db

    today = date.today()
    date_from = today - timedelta(days=days - 1)

    logger = logging.getLogger("pipeline")
    logger.info("=== Полный синк FBO: %s … %s (%d дн.) ===", date_from, today, days)

    # Отдельное соединение только для записи статуса шагов каталога/продаж.
    status_conn = get_fbo_connection()
    init_fbo_db(status_conn)
    # Чистим статусы прошлого прогона, чтобы плашка прогресса показывала только
    # текущий синк (а не «running» от прерванного предыдущего).
    try:
        status_conn.execute("DELETE FROM fbo_sync_status")
        status_conn.commit()
    except Exception:
        pass

    @contextmanager
    def _step(name: str):
        started = datetime.utcnow().isoformat()
        t0 = time.monotonic()
        st: dict = {"rows": None}
        upsert_sync_status(status_conn, name, "running", started_at=started)
        try:
            yield st
        except Exception as exc:
            upsert_sync_status(
                status_conn, name, "error",
                started_at=started, finished_at=datetime.utcnow().isoformat(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                rows_affected=st["rows"], error_message=f"{type(exc).__name__}: {exc}"[:500],
            )
            logger.exception("[pipeline] шаг '%s' упал", name)
        else:
            upsert_sync_status(
                status_conn, name, "ok",
                started_at=started, finished_at=datetime.utcnow().isoformat(),
                duration_ms=int((time.monotonic() - t0) * 1000),
                rows_affected=st["rows"], error_message=None,
            )

    seller = SellerClient()  # ключи из .env; кинет ValueError, если не заданы

    conn = get_connection()
    init_db(conn)
    try:
        with _step("catalog") as st:
            logger.info("[1/3] Каталог товаров…")
            st["rows"] = sync_catalog(conn, seller)
        with _step("sales") as st:
            logger.info("[2/3] Продажи по дням…")
            st["rows"] = sync_sales(conn, seller, date_from, today, basic_only=True)
    finally:
        conn.close()

    # run_full_sync сам пишет свои шаги (stocks_refresh, cluster_map, …, compute)
    # в fbo_sync_status и индивидуально оборачивает каждый в try/except.
    status_conn.close()
    logger.info("[3/3] Остатки, кластеры, FBO-постинги, рекомендации…")
    run_full_sync(company_id=None, sales_window_days=min(days, 35))
    logger.info("=== Готово ===")


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Полный синк данных FBO из Ozon")
    parser.add_argument("--days", type=int, default=35, help="Сколько дней истории тянуть (по умолчанию 35)")
    args = parser.parse_args()
    run_pipeline(days=args.days)
