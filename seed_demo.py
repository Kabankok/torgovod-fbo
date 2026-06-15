"""Демо-данные для «Торговод · Отгрузки FBO».

Заполняет локальные БД реалистичными примерами (товары, продажи за 30 дней,
остатки по кластерам, оборачиваемость) и прогоняет настоящий расчёт compute_all —
чтобы экраны «засветились» без подключения к Ozon. Удобно для ознакомления,
скриншотов и записи видео.

Запуск:
    python seed_demo.py

Сбросить демо: удалите файлы в папке data/ и перезапустите приложение.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

from modules.fbo.analytics import compute_all
from modules.fbo.storage import get_fbo_connection, init_fbo_db
from shared.db.models import get_connection, init_db

random.seed(42)

CLUSTERS = ["Москва, МО и Дальние регионы", "Санкт-Петербург и СЗО", "Екатеринбург", "Краснодар"]

# (offer_id, name, price, профиль спроса, профиль остатка)
#   профиль спроса:  daily ~ среднее заказов в день
#   профиль остатка: множитель к дневному спросу (мало → дефицит, много → избыток)
PRODUCTS = [
    ("ORG-CAB-05",  "Органайзер для кабелей, 5 шт",            390,  11.0, 0.3),   # дефицит
    ("YOGA-MAT-6",  "Коврик для йоги 6 мм, нескользящий",      1290, 6.0,  0.6),   # риск
    ("SOAP-AUTO",   "Дозатор для мыла автоматический",         1490, 4.5,  4.0),   # норма
    ("NB-STAND-AL", "Подставка для ноутбука алюминиевая",      1990, 3.2,  20.0),  # избыток
    ("THERMO-450",  "Термокружка 450 мл, сталь",               890,  8.0,  0.5),   # дефицит/риск
    ("LED-GARL-10", "Гирлянда LED 10 м, тёплый свет",          650,  9.5,  1.2),   # риск
    ("FOOD-BOX-3",  "Контейнеры для еды, набор 3 шт",          540,  5.5,  2.5),   # норма
    ("LED-TAPE-5",  "Лента светодиодная 5 м, RGB",             790,  7.0,  0.4),   # дефицит
    ("NECK-MASS",   "Массажёр для шеи аккумуляторный",         2490, 2.1,  18.0),  # избыток
    ("HAIR-BRUSH",  "Щётка массажная для волос",               320,  6.5,  1.8),   # норма/риск
    ("HUMID-300",   "Увлажнитель воздуха 300 мл",              1190, 4.0,  0.7),   # риск
    ("CAR-HOLD-3",  "Держатель для телефона в авто",           450,  10.0, 3.5),   # норма
]


def seed() -> None:
    today = date.today()
    today_s = today.isoformat()

    analytics = get_connection()
    init_db(analytics)
    fbo = get_fbo_connection()
    init_fbo_db(fbo)

    print("Очищаю прошлые демо-данные…")
    with analytics:
        for t in ("products", "sku_analytics_daily", "sku_stocks_by_warehouse"):
            analytics.execute(f"DELETE FROM {t}")
    with fbo:
        for t in (
            "fbo_stock_cluster", "fbo_sales_cluster", "fbo_turnover",
            "fbo_sku_summary", "fbo_cluster_recommendations", "fbo_warehouse_cluster_map",
        ):
            fbo.execute(f"DELETE FROM {t}")

    now_iso = today_s + "T00:00:00"

    for i, (offer_id, name, price, daily, stock_mult) in enumerate(PRODUCTS):
        sku = str(900100 + i)

        # ── каталог ──
        with analytics:
            analytics.execute(
                "INSERT INTO products (sku, product_id, offer_id, name, price, old_price, image_url, is_archived, updated_at)"
                " VALUES (?,?,?,?,?,?,?,0,?)",
                (sku, sku, offer_id, name, price, round(price * 1.4), "", now_iso),
            )

        # ── продажи за 30 дней ──
        rows = []
        for d in range(30):
            day = (today - timedelta(days=d)).isoformat()
            units = max(0, int(round(random.gauss(daily, daily * 0.35))))
            rows.append((sku, day, units * price, units))
        with analytics:
            analytics.executemany(
                "INSERT INTO sku_analytics_daily (sku, date, revenue, ordered_units) VALUES (?,?,?,?)",
                rows,
            )

        # ── итоги продаж по окнам (для разбивки по кластерам) ──
        d10 = (today - timedelta(days=10)).isoformat()
        d28 = (today - timedelta(days=28)).isoformat()
        qty_30d = sum(u for _, _, _, u in rows)
        qty_28d = sum(u for _, dd, _, u in rows if dd >= d28)
        qty_10d = sum(u for _, dd, _, u in rows if dd >= d10)
        rev_30d = sum(r for _, _, r, _ in rows)

        # ── остатки + продажи по кластерам (раскидываем по 2–4 кластерам) ──
        total_stock = int(daily * 30 * stock_mult)
        n_clusters = random.randint(2, 4)
        weights = [random.random() for _ in range(n_clusters)]
        wsum = sum(weights) or 1
        with fbo:
            for ci in range(n_clusters):
                cluster = CLUSTERS[ci]
                share = weights[ci] / wsum
                fact = int(total_stock * share)
                fbo.execute(
                    "INSERT INTO fbo_stock_cluster (sku, cluster_name, snapshot_date, fact_stock, in_transit, reserved)"
                    " VALUES (?,?,?,?,?,?)",
                    (sku, cluster, today_s, fact, random.randint(0, max(1, fact // 5)), random.randint(0, 3)),
                )
                c_qty30 = int(qty_30d * share)
                fbo.execute(
                    "INSERT INTO fbo_sales_cluster (sku, cluster_name, snapshot_date, qty_10d, qty_28d, qty_30d, revenue_30d, avg_daily_qty)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (sku, cluster, today_s, int(qty_10d * share), int(qty_28d * share),
                     c_qty30, rev_30d * share, round(c_qty30 / 30.0, 4)),
                )

        # ── оборачиваемость (грейд Ozon) ──
        idc_days = round((total_stock / daily) if daily else 0, 1)
        grade = "RED" if idc_days < 20 else ("YELLOW" if idc_days < 45 else "GREEN")
        with fbo:
            fbo.execute(
                "INSERT INTO fbo_turnover (sku, current_stock, ads_daily, idc_days, idc_grade,"
                " turnover_days, turnover_grade, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (sku, total_stock, round(daily, 2), idc_days, grade, idc_days, grade, now_iso),
            )

    print("Считаю ABC, рекомендации и статусы (compute_all)…")
    compute_all(fbo, analytics, today_s)

    n = fbo.execute("SELECT COUNT(*) FROM fbo_sku_summary").fetchone()[0]
    by_status = fbo.execute(
        "SELECT status, COUNT(*) FROM fbo_sku_summary GROUP BY status"
    ).fetchall()
    analytics.close()
    fbo.close()

    print(f"Готово: {n} SKU в сводке.")
    for st, c in by_status:
        print(f"  {st}: {c}")
    print("\nЗапусти приложение (start.bat) и открой http://localhost:4000/fbo")


if __name__ == "__main__":
    seed()
