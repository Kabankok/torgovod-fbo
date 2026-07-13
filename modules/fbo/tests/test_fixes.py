"""Регресс-тесты на дефекты, найденные аудитом 2026-07-13.

Каждый тест — про то, что пользователь видел неправильно, а не про внутренности:
спрос отключённого кластера пропадал, незнакомый кластер Ozon вливался в Москву,
слот-хантер сравнивал UTC с местным временем, итоги перезаписывались нулями.

Запуск:  python -m pytest modules/fbo/tests -q
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from modules.fbo.analytics import compute_all
from modules.fbo.slot_hunter import _parse_dt, find_matching_slot
from modules.fbo.storage import _apply_redistribution, init_fbo_db
from modules.fbo.sync import normalize_cluster_name


# ── Перераспределение спроса с отключённых кластеров ──────────────────────────
def test_demand_of_disabled_cluster_is_not_lost_when_manual_fallback_is_disabled() -> None:
    """Ручной резерв, который сам отключён, раньше молча съедал спрос.

    `fallback or _find_nearest_active(...)` короткозамыкается на непустом ручном резерве,
    а у проверки `if fallback in active_map` не было else — units исчезали из отчёта.
    """
    clusters = [
        {"cluster_name": "Беларусь", "is_active": 0, "recommendation": 180, "qty_30d": 75,
         "fallback_cluster": "Калининград"},  # резерв указан вручную, но он сам выключен
        {"cluster_name": "Калининград", "is_active": 0, "recommendation": 0, "qty_30d": 8},
        {"cluster_name": "Санкт-Петербург и СЗО", "is_active": 1, "recommendation": 20,
         "qty_30d": 400},
        {"cluster_name": "Москва, МО и Дальние регионы", "is_active": 1, "recommendation": 10,
         "qty_30d": 900},
    ]
    result = _apply_redistribution(clusters)

    total = sum(c["recommendation"] for c in result)
    assert total == 210, "спрос отключённого кластера обязан куда-то переехать, а не исчезнуть"
    spb = next(c for c in result if c["cluster_name"] == "Санкт-Петербург и СЗО")
    assert spb["recommendation"] == 200  # 20 своих + 180 из Беларуси (ближайший активный)
    assert spb["merged_from"] == [{"cluster": "Беларусь", "units": 180}]


def test_redistribution_is_deterministic_for_unknown_cluster() -> None:
    """У незнакомого кластера нет географических соседей: раньше спрос уезжал в случайный
    активный кластер (`next(iter(set))`), теперь — в крупнейший по продажам."""
    clusters = [
        {"cluster_name": "Ижевск", "is_active": 0, "recommendation": 50, "qty_30d": 30},
        {"cluster_name": "Казань", "is_active": 1, "recommendation": 0, "qty_30d": 100},
        {"cluster_name": "Москва, МО и Дальние регионы", "is_active": 1, "recommendation": 0,
         "qty_30d": 900},
    ]
    for _ in range(5):
        result = _apply_redistribution(clusters)
        biggest = next(c for c in result if c["cluster_name"] == "Москва, МО и Дальние регионы")
        assert biggest["recommendation"] == 50


# ── Незнакомый кластер Ozon ───────────────────────────────────────────────────
def test_unknown_cluster_is_kept_not_merged_into_moscow() -> None:
    """Новый/переименованный кластер Ozon не должен молча вливаться в Москву."""
    assert normalize_cluster_name("Izhevsk") == "Izhevsk"
    # А известные — по-прежнему приводятся к каноническому русскому имени,
    # в том числе без бэктика, который Ozon ставит не всегда.
    assert normalize_cluster_name("Kazan`") == "Казань"
    assert normalize_cluster_name("Kazan") == "Казань"
    assert normalize_cluster_name("Belarus`") == "Беларусь"


# ── Слот-хантер: часовые пояса ────────────────────────────────────────────────
def test_slot_time_window_is_compared_in_local_time() -> None:
    """Ozon отдаёт слоты в UTC, пользователь вводит окно по-местному.

    Раньше «Z» отрезался, и 08:00 UTC сравнивалось с 08:00 местного — в Екатеринбурге
    (UTC+5) охотник ловил слот на 13:00 вместо запрошенного утреннего.
    """
    slot_utc = datetime.now(UTC) + timedelta(days=2)
    slot_utc = slot_utc.replace(minute=0, second=0, microsecond=0)
    local = slot_utc.astimezone()

    slots = [
        {
            "from": slot_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": (slot_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    ]
    day = local.date().isoformat()
    window_from = f"{max(local.hour - 1, 0):02d}:00"
    window_to = f"{min(local.hour + 2, 23):02d}:00"

    # Окно вокруг ЛОКАЛЬНОГО времени слота — слот обязан найтись.
    assert find_matching_slot(slots, day, day, window_from, window_to) is not None

    # Разобранное время осознаёт часовой пояс (раньше «Z» отрезался и время было наивным).
    parsed = _parse_dt(slots[0]["from"])
    assert parsed is not None and parsed.tzinfo is not None
    assert parsed == slot_utc


# ── Итоги не перезаписываются нулями ──────────────────────────────────────────
def test_compute_all_falls_back_to_last_stock_snapshot() -> None:
    """Если снимок остатков за сегодня не записался (шаг синка упал), итоги должны
    считаться по последнему реальному снимку, а не по нулям («вези всё»)."""
    fbo = sqlite3.connect(":memory:")
    fbo.row_factory = sqlite3.Row
    init_fbo_db(fbo)
    analytics = sqlite3.connect(":memory:")
    analytics.row_factory = sqlite3.Row
    analytics.executescript("""
        CREATE TABLE products (sku TEXT PRIMARY KEY, offer_id TEXT, name TEXT);
        CREATE TABLE sku_analytics_daily (sku TEXT, date TEXT, ordered_units INT, revenue REAL);
        INSERT INTO products VALUES ('1', 'ART-1', 'Хомут');
    """)
    analytics.commit()

    yesterday = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    today = datetime.now(UTC).date().isoformat()
    with fbo:
        fbo.execute(
            "INSERT INTO fbo_stock_cluster (sku, cluster_name, snapshot_date, fact_stock,"
            " in_transit, reserved) VALUES ('1', 'Беларусь', ?, 121, 10, 0)",
            (yesterday,),
        )

    compute_all(fbo, analytics, today)  # снимка за сегодня НЕТ

    row = fbo.execute("SELECT fact_stock FROM fbo_sku_summary WHERE sku = '1'").fetchone()
    assert row is not None
    assert row["fact_stock"] == 121, "остаток обязан браться из последнего снимка, а не обнуляться"
    fbo.close()
    analytics.close()


def test_summary_is_not_pruned_when_sales_window_is_empty() -> None:
    """Чистка зомби-строк не должна выкашивать каталог у вернувшегося пользователя.

    summary покрывает продажи ∪ остатки. Если sku_analytics_daily пуста (первый запуск или
    пользователь не заходил больше 30 дней — продажи синкаются ПОЗЖЕ этого шага), summary
    схлопывается до пары товаров с остатком, и чистка по snapshot_date снесла бы весь
    остальной каталог: на реальной БД это 6783 товара из 7197.
    """
    fbo = sqlite3.connect(":memory:")
    fbo.row_factory = sqlite3.Row
    init_fbo_db(fbo)
    analytics = sqlite3.connect(":memory:")
    analytics.row_factory = sqlite3.Row
    analytics.executescript("""
        CREATE TABLE products (sku TEXT PRIMARY KEY, offer_id TEXT, name TEXT);
        CREATE TABLE sku_analytics_daily (sku TEXT, date TEXT, ordered_units INT, revenue REAL);
        INSERT INTO products VALUES ('1', 'ART-1', 'С остатком'), ('2', 'ART-2', 'Без остатка');
    """)  # sku_analytics_daily намеренно пуста — окна продаж нет
    analytics.commit()

    today = datetime.now(UTC).date().isoformat()
    old = (datetime.now(UTC) - timedelta(days=40)).date().isoformat()
    with fbo:
        # Прошлый визит: в отчёте были оба товара.
        fbo.executemany(
            "INSERT INTO fbo_sku_summary (sku, offer_id, name, snapshot_date, fact_stock)"
            " VALUES (?, ?, ?, ?, ?)",
            [("1", "ART-1", "С остатком", old, 5), ("2", "ART-2", "Без остатка", old, 7)],
        )
        # Сегодня остаток есть только у первого.
        fbo.execute(
            "INSERT INTO fbo_stock_cluster (sku, cluster_name, snapshot_date, fact_stock,"
            " in_transit, reserved) VALUES ('1', 'Беларусь', ?, 5, 0, 0)",
            (today,),
        )

    compute_all(fbo, analytics, today)

    skus = {r["sku"] for r in fbo.execute("SELECT sku FROM fbo_sku_summary")}
    assert skus == {"1", "2"}, "без окна продаж чистка обязана воздержаться, а не срезать каталог"
    fbo.close()
    analytics.close()


def test_failed_postings_do_not_wipe_the_good_cluster_sales_snapshot() -> None:
    """Повторный синк при упавшем postings-API не должен стирать утренний срез продаж.

    Снимок продаж пересобирается только когда цифры пришли из постингов. Если Ozon ответил
    429, на руках лишь грубый резерв (продажи, размазанные по доле остатка) — затирать им
    хороший срез нельзя.
    """
    from modules.fbo.sync import sync_sales_by_cluster

    fbo = sqlite3.connect(":memory:")
    fbo.row_factory = sqlite3.Row
    init_fbo_db(fbo)
    analytics = sqlite3.connect(":memory:")
    analytics.row_factory = sqlite3.Row
    analytics.executescript("""
        CREATE TABLE sku_analytics_daily (sku TEXT, date TEXT, ordered_units INT, revenue REAL);
        INSERT INTO sku_analytics_daily VALUES ('1', date('now'), 4, 400.0);
    """)
    analytics.commit()

    today = datetime.now(UTC).date().isoformat()
    with fbo:
        # Утренний синк: реальный срез из постингов.
        fbo.execute(
            "INSERT INTO fbo_sales_cluster (sku, cluster_name, snapshot_date, qty_10d, qty_28d,"
            " qty_30d, revenue_30d, avg_daily_qty) VALUES ('1', 'Беларусь', ?, 3, 8, 9, 900.0, 0.3)",
            (today,),
        )
        fbo.execute(
            "INSERT INTO fbo_stock_cluster (sku, cluster_name, snapshot_date, fact_stock,"
            " in_transit, reserved) VALUES ('1', 'Москва, МО и Дальние регионы', ?, 10, 0, 0)",
            (today,),
        )

    class DeadClient:
        def get_fbo_postings(self, *_args, **_kwargs):
            raise RuntimeError("429 Too Many Requests")

    sync_sales_by_cluster(fbo, analytics, DeadClient(), today)

    bel = fbo.execute(
        "SELECT qty_30d FROM fbo_sales_cluster WHERE sku='1' AND cluster_name='Беларусь'"
        " AND snapshot_date=?",
        (today,),
    ).fetchone()
    assert bel is not None, "срез из постингов стёрт резервным расчётом"
    assert bel["qty_30d"] == 9
    fbo.close()
    analytics.close()


def test_pipeline_passes_date_as_string_to_compute_all() -> None:
    """compute_all принимает дату строкой (внутри date.fromisoformat).

    pipeline передавал объект date — шаг «recompute» падал в TypeError при каждом синке,
    а _step гасил исключение, так что пересчёт «проходил», ничего не пересчитав.
    """
    import inspect

    import pipeline

    src = inspect.getsource(pipeline.run_pipeline)
    assert "compute_all(status_conn, analytics_conn, today.isoformat())" in src
