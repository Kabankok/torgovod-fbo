"""FBO sync: pull data from Ozon API and analytics.db, store in fbo.db.

Steps:
1. Sync cluster map (warehouse → cluster)
2. Sync stock by cluster (from analytics.db sku_stocks_by_warehouse)
3. Sync sales by cluster (from analytics.db sku_analytics_daily + FBO postings)
4. Sync turnover from Ozon API
5. Run analytics to compute recommendations and summaries

Run: python -m modules.fbo.sync
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

from modules.fbo.storage import get_fbo_connection, init_fbo_db, upsert_sync_status
from shared.api.seller import SellerClient
from shared.db.models import get_connection as get_analytics_connection


@contextmanager
def _track_step(fbo: sqlite3.Connection, step: str) -> Iterator[dict[str, Any]]:
    """Record per-step status of the FBO sync into fbo_sync_status.

    Why: previously each step (esp. sync_turnover) silently swallowed exceptions,
    so the user saw "sync ok" while the data behind one step stayed stale.
    Yielded dict lets the step report rows_affected via state['rows'] = N.
    """
    started = datetime.utcnow().isoformat()
    t0 = time.monotonic()
    state: dict[str, Any] = {"rows": None}
    upsert_sync_status(fbo, step, "running", started_at=started)
    try:
        yield state
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        upsert_sync_status(
            fbo,
            step,
            "error",
            started_at=started,
            finished_at=datetime.utcnow().isoformat(),
            duration_ms=duration_ms,
            rows_affected=state.get("rows"),
            error_message=f"{type(exc).__name__}: {exc}"[:500],
        )
        raise
    else:
        duration_ms = int((time.monotonic() - t0) * 1000)
        upsert_sync_status(
            fbo,
            step,
            "ok",
            started_at=started,
            finished_at=datetime.utcnow().isoformat(),
            duration_ms=duration_ms,
            rows_affected=state.get("rows"),
            error_message=None,
        )


# Fallback cluster inference from warehouse name (when /v1/cluster/list is missing warehouse).
# Cluster names here MUST match Ozon's own taxonomy (the values of _LATIN_TO_RU below) — a name
# that exists only here would never line up with the clusters that postings and cluster/list
# report, and the same SKU would land in two different clusters for stock and for sales.
_CLUSTER_KEYWORDS: list[tuple[list[str], str]] = [
    (
        [
            "москва",
            "мо ",
            "хоругвино",
            "домодедово",
            "подольск",
            "котельники",
            "быково",
            "электросталь",
            "ногинск",
            "пушкино",
            "жуковский",
            "раменское",
            "софьино",
            "чехов",
            "внуково",
            "коледино",
        ],
        "Москва, МО и Дальние регионы",
    ),
    (
        [
            "санкт-петербург",
            "санкт петербург",
            "спб",
            "шушары",
            "колпино",
            "бугры",
            "волхонское",  # Волхонское шоссе, Ленобласть
            "московское",  # Московское шоссе — это Петербург, а не Москва
            "лиговский",
            "красное село",
            "троицкий",
            "уткина заводь",
            "мурманск",
            "архангельск",
            "сыктывкар",
            "петрозаводск",
        ],
        "Санкт-Петербург и СЗО",
    ),
    (
        [
            "екатеринбург",
            "екб",
            "уральск",  # «…_УРАЛЬСКАЯ»; проигрывает более длинному ключу другого кластера
            "первоуральск",
            "свердловск",
            "челябинск",
            "березовский",
        ],
        "Екатеринбург",
    ),
    (["новосибирск", "нск", "нсб", "сибирь", "толмачево", "кемерово", "томск"], "Новосибирск"),
    (["ростов", "аксай", "батайск", "новочеркасск"], "Ростов"),
    (
        [
            "краснодар",
            "кубань",
            "новороссийск",
            "тимашевск",
            "адыгейск",
            "старобжегокай",
            "южный обход",
            "медиа плаза",
            "крд",
        ],
        "Краснодар",
    ),
    (["уфа", "башкир", "стерлитамак", "октябрьский", "нефтекамск"], "Уфа"),
    (["оренбург"], "Оренбург"),
    (["тюмень", "тюменск"], "Тюмень"),
    (["тверь", "тверск", "боровлево"], "Тверь"),
    (["ярославль", "ярославск", "кострома"], "Ярославль"),
    (["самар", "самарск", "тольятти", "чапаевск"], "Самара"),
    (["пермь", "пермск"], "Пермь"),
    (
        ["саратов", "саратовск", "энгельс", "волгоград", "волжский", "средняя ахтуба"],
        "Саратов",
    ),
    (
        [
            "казань",
            "кзн",
            "татарст",
            "зеленодольск",
            "столбище",
            "нижний новгород",
            "нижний-новгород",
            "нино",  # так Ozon сокращает Нижний Новгород в именах складов
            "иннополис",
            "дзержинск",  # Дзержинск Нижегородской обл. — у Ozon это кластер Казань
        ],
        "Казань",
    ),
    (["воронеж"], "Воронеж"),
    (["красноярск", "красноярский"], "Красноярск"),
    (["омск"], "Омск"),
    (
        ["дальн", "владивосток", "хабаровск", "иркутск", "якутск", "сахалин", "чита", "улан-удэ"],
        "Дальний Восток",
    ),
    (["беларус", "минск", "беларуск"], "Беларусь"),
    (["невинномысск", "ставрополь"], "Невинномысск"),
    (["махачкал", "дагест", "грозный", "владикавказ"], "Махачкала"),
    (["калининград"], "Калининград"),
    (["астана", "нур-султан"], "Астана"),
    (["алматы", "алма-аты"], "Алматы"),
    (["кыргыз", "бишкек"], "Кыргызстан"),
    (["узбекист", "ташкент"], "Узбекистан"),
    (["армени", "ереван"], "Армения"),
    (["грузи", "тбилис"], "Грузия"),
]

_DEFAULT_CLUSTER = "Москва, МО и Дальние регионы"

# Ozon FBO postings API returns cluster names in Latin transliteration.
# This maps them to the Russian canonical names used throughout the system.
_LATIN_TO_RU: dict[str, str] = {
    "Moskva, MO i Dal`nie regiony`": "Москва, МО и Дальние регионы",
    "Sankt-Peterburg i SZO": "Санкт-Петербург и СЗО",
    "Ekaterinburg": "Екатеринбург",
    "Novosibirsk": "Новосибирск",
    "Rostov": "Ростов",
    "Ufa": "Уфа",
    "Krasnodar": "Краснодар",
    "Saratov": "Саратов",
    "Samara": "Самара",
    "Perm`": "Пермь",
    "Belarus`": "Беларусь",
    "Dal`nij Vostok": "Дальний Восток",
    "Nevinnomy`ssk": "Невинномысск",
    "Maxachkala": "Махачкала",
    "Tver`": "Тверь",
    "Omsk": "Омск",
    "Orenburg": "Оренбург",
    "Astana": "Астана",
    "Kaliningrad": "Калининград",
    "Almaty`": "Алматы",
    "Kazan`": "Казань",
    "Voronezh": "Воронеж",
    "Tyumen`": "Тюмень",
    "Krasnoyarsk": "Красноярск",
    "Ky`rgy`zstan": "Кыргызстан",
    "Uzbekistan": "Узбекистан",
    "Armeniya": "Армения",
    "Gruziya": "Грузия",
    "Yaroslavl`": "Ярославль",
    # Common abbreviations / alternate spellings
    "Moskva": "Москва, МО и Дальние регионы",
    "Sankt-Peterburg": "Санкт-Петербург и СЗО",
    "SPb": "Санкт-Петербург и СЗО",
    "Ekb": "Екатеринбург",
    "NSK": "Новосибирск",
}


def _cluster_key(name: str) -> str:
    """Key for tolerant comparison: Ozon writes "Kazan`" / "Perm`" with a trailing backtick,
    and a rename or a missing backtick must not turn a known cluster into an unknown one."""
    return name.lower().replace("`", "").replace("'", "").strip()


def normalize_cluster_name(raw: str) -> str:
    """Map any cluster name (Latin or Cyrillic) to our canonical Russian name."""
    if not raw:
        return _DEFAULT_CLUSTER
    # Direct match in Latin→RU table
    if raw in _LATIN_TO_RU:
        return _LATIN_TO_RU[raw]
    # Already Cyrillic and matches a known Russian cluster — return as-is
    # (handles cases where API returns Cyrillic directly)
    known_ru = set(_LATIN_TO_RU.values())
    if raw in known_ru:
        return raw
    # Tolerant match: backtick/case differences ("Kazan" vs "Kazan`") must not fall through
    # to inference, whose keywords are Cyrillic-only and would silently answer «Москва».
    key = _cluster_key(raw)
    for latin, ru in _LATIN_TO_RU.items():
        if _cluster_key(latin) == key:
            return ru
    for ru in known_ru:
        if _cluster_key(ru) == key:
            return ru
    # Fuzzy: try keyword inference on the raw value
    lower = raw.lower().replace("_", " ")
    for keywords, cluster in _CLUSTER_KEYWORDS:
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw), lower):
                return cluster
    # Unknown cluster (Ozon added or renamed one). Returning _DEFAULT_CLUSTER here would
    # silently pour its sales into Москва and erase the region from the report entirely —
    # the same class of silent misattribution as the МИНСК bug. Keep it as its own row.
    logger.warning("[fbo/sync] unknown cluster name from Ozon: %r — kept as-is", raw)
    return raw


def infer_cluster(warehouse_name: str) -> str:
    """Map warehouse name to business cluster by keyword matching.

    Warehouse names from Ozon may use underscores as word separators
    (e.g. "Санкт_Петербург_РФЦ"). Normalize to spaces so keyword matching
    works correctly with hyphenated and space-separated keywords alike.

    Matching is anchored to a word boundary (\b) at the start of each keyword,
    so a short abbreviation never matches inside an unrelated word. Without this,
    "нск" (Новосибирск) matched as a substring of "минск" and the whole
    Belarus (Минск) FBO stock was silently misassigned to the Новосибирск cluster.

    Among all matching keywords the LONGEST one wins, so correctness does not depend
    on the order of the rules: "НОВОСИБИРСК_ОМСКИЙ_ТРАКТ" resolves by "новосибирск"
    rather than "омск", and "КРАСНОДАР_СППЗ_УРАЛЬСКАЯ" by "краснодар" over "уральск".
    """
    # Normalise: underscores → space, keep hyphens for keyword matching
    lower = warehouse_name.lower().replace("_", " ")
    best_kw = ""
    best_cluster = ""
    for keywords, cluster in _CLUSTER_KEYWORDS:
        for kw in keywords:
            if len(kw) > len(best_kw) and re.search(r"\b" + re.escape(kw), lower):
                best_kw, best_cluster = kw, cluster
    if not best_cluster:
        # Silent fallback to Москва is how МИНСК-class errors hide: log it so an unknown
        # warehouse is visible in the log instead of quietly inflating the default cluster.
        logger.warning(
            "[fbo/sync] cluster not inferred for warehouse %r → %s", warehouse_name, _DEFAULT_CLUSTER
        )
        return _DEFAULT_CLUSTER
    return best_cluster


def sync_cluster_map(
    fbo: sqlite3.Connection,
    client: SellerClient,
) -> dict[str, str]:
    """Fetch /v1/cluster/list and build warehouse → cluster mapping.

    Returns {warehouse_name: cluster_name}.
    """
    logger.info("[fbo/sync] Fetching cluster list from Ozon API...")
    mapping: dict[str, str] = {}
    try:
        clusters = client.get_cluster_list()
        for c in clusters:
            cluster_name = c["cluster_name"]
            for w in c.get("warehouses") or []:
                wname = w["warehouse_name"]
                if wname:
                    mapping[wname] = cluster_name
        logger.info("[fbo/sync] Got %d warehouses from cluster API", len(mapping))
    except Exception as e:
        logger.warning("[fbo/sync] cluster/list failed (%s), will use inference only", e)

    # Save to DB
    rows = [(wname, cname, "api") for wname, cname in mapping.items()]
    with fbo:
        fbo.executemany(
            """
            INSERT INTO fbo_warehouse_cluster_map (warehouse_name, cluster_name, source)
            VALUES (?, ?, ?)
            ON CONFLICT(warehouse_name) DO UPDATE SET cluster_name=excluded.cluster_name, source=excluded.source
        """,
            rows,
        )

    heal_inferred_clusters(fbo)

    return mapping


def heal_inferred_clusters(fbo: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Re-run inference over cached 'inferred' rows and fix the ones that changed.

    Rows are cached in fbo_warehouse_cluster_map, and _get_cluster prefers the cache
    over a fresh inference — so a cluster mis-inferred by an older version of
    infer_cluster would stay wrong forever, even after the inference logic is fixed.
    Re-inferring on every sync makes the fix reach existing installs without asking
    the user to run a repair script. Rows with source='api' come from Ozon and are
    authoritative — never touched here.

    Returns [(warehouse_name, old_cluster, new_cluster)] for the rows that changed.
    """
    rows = fbo.execute(
        "SELECT warehouse_name, cluster_name FROM fbo_warehouse_cluster_map WHERE source = 'inferred'"
    ).fetchall()

    changed = [
        (r["warehouse_name"], r["cluster_name"], infer_cluster(r["warehouse_name"]))
        for r in rows
    ]
    changed = [(w, old, new) for (w, old, new) in changed if old != new]
    if not changed:
        return []

    with fbo:
        fbo.executemany(
            "UPDATE fbo_warehouse_cluster_map SET cluster_name = ? WHERE warehouse_name = ?",
            [(new, w) for (w, _old, new) in changed],
        )
    for w, old, new in changed:
        logger.info("[fbo/sync] healed warehouse cluster: %s: %s -> %s", w, old, new)
    return changed


def _get_cluster(
    warehouse_name: str,
    mapping: dict[str, str],
    fbo: sqlite3.Connection,
) -> str:
    """Look up cluster for warehouse, falling back to DB then inference."""
    if warehouse_name in mapping:
        return mapping[warehouse_name]
    row = fbo.execute(
        "SELECT cluster_name FROM fbo_warehouse_cluster_map WHERE warehouse_name = ?",
        (warehouse_name,),
    ).fetchone()
    if row:
        return row[0]
    cluster = infer_cluster(warehouse_name)
    # Save inferred mapping for future use
    with fbo:
        fbo.execute(
            """
            INSERT OR IGNORE INTO fbo_warehouse_cluster_map (warehouse_name, cluster_name, source)
            VALUES (?, ?, 'inferred')
        """,
            (warehouse_name, cluster),
        )
    mapping[warehouse_name] = cluster
    return cluster


def sync_stock_by_cluster(
    fbo: sqlite3.Connection,
    analytics: sqlite3.Connection,
    mapping: dict[str, str],
    today: str,
) -> None:
    """Aggregate sku_stocks_by_warehouse → fbo_stock_cluster, grouping by cluster.

    Uses the latest available date in analytics.db if today has no data yet.
    """
    logger.info("[fbo/sync] Aggregating stocks by cluster...")

    # Get latest available date
    row = analytics.execute(
        "SELECT MAX(date) AS d FROM sku_stocks_by_warehouse WHERE warehouse_type = 'fbo'"
    ).fetchone()
    latest_date = row[0] if row and row[0] else today

    rows = analytics.execute(
        """
        SELECT sku, warehouse_name, present, promised, reserved
        FROM sku_stocks_by_warehouse
        WHERE date = ? AND warehouse_type = 'fbo'
    """,
        (latest_date,),
    ).fetchall()

    # Aggregate by (sku, cluster)
    agg: dict[tuple[str, str], dict[str, int]] = {}
    for r in rows:
        cluster = _get_cluster(r["warehouse_name"], mapping, fbo)
        key = (r["sku"], cluster)
        if key not in agg:
            agg[key] = {"fact": 0, "transit": 0, "reserved": 0}
        agg[key]["fact"] += r["present"] or 0
        agg[key]["transit"] += r["promised"] or 0
        agg[key]["reserved"] += r["reserved"] or 0

    cluster_rows = [
        (sku, cluster, today, v["fact"], v["transit"], v["reserved"])
        for (sku, cluster), v in agg.items()
    ]

    with fbo:
        # Rebuild today's snapshot from scratch. An upsert alone would leave behind rows
        # written under a warehouse's previous cluster (e.g. МИНСК_МПСЦ moving from
        # Новосибирск to Беларусь), double-counting the same stock in both clusters.
        # Guarded by `if cluster_rows` so an empty source never wipes a good snapshot.
        if cluster_rows:
            fbo.execute("DELETE FROM fbo_stock_cluster WHERE snapshot_date = ?", (today,))
        fbo.executemany(
            """
            INSERT INTO fbo_stock_cluster (sku, cluster_name, snapshot_date, fact_stock, in_transit, reserved)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku, cluster_name, snapshot_date) DO UPDATE SET
                fact_stock  = excluded.fact_stock,
                in_transit  = excluded.in_transit,
                reserved    = excluded.reserved
        """,
            cluster_rows,
        )

    logger.info("[fbo/sync] Upserted %d stock-cluster rows for %s", len(cluster_rows), today)


def sync_sales_by_cluster(
    fbo: sqlite3.Connection,
    analytics: sqlite3.Connection,
    client: SellerClient,
    today: str,
    sales_window_days: int = 35,
) -> None:
    """Build cluster-level sales aggregates.

    Primary: use FBO postings API (has cluster_to field).
    Fallback: analytics.db ordered_units allocated proportionally to stock clusters.
    """
    logger.info("[fbo/sync] Syncing FBO postings for cluster sales...")
    date_to = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    date_from = (datetime.now(UTC) - timedelta(days=sales_window_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    postings: list[dict[str, Any]] = []
    try:
        postings = client.get_fbo_postings(date_from, date_to)
        logger.info("[fbo/sync] Got %d FBO posting lines", len(postings))
    except Exception as e:
        logger.warning("[fbo/sync] FBO postings API failed: %s. Falling back to analytics.db", e)

    now_dt = datetime.fromisoformat(today)
    d10 = (now_dt - timedelta(days=10)).date().isoformat()
    d28 = (now_dt - timedelta(days=28)).date().isoformat()
    d30 = (now_dt - timedelta(days=30)).date().isoformat()

    agg: dict[tuple[str, str], dict[str, Any]] = {}  # (sku, cluster) → metrics

    if postings:
        for p in postings:
            sku = p["sku"]
            raw_cluster = p.get("cluster_to") or p.get("cluster_from") or ""
            cluster = normalize_cluster_name(raw_cluster)
            key = (sku, cluster)
            if key not in agg:
                agg[key] = {"qty_10d": 0, "qty_28d": 0, "qty_30d": 0, "revenue_30d": 0.0}
            created = p["created_at"][:10] if p["created_at"] else ""
            qty = p["quantity"]
            rev = p["price"] * qty
            if created >= d30:
                agg[key]["qty_30d"] += qty
                agg[key]["revenue_30d"] += rev
            if created >= d28:
                agg[key]["qty_28d"] += qty
            if created >= d10:
                agg[key]["qty_10d"] += qty
    else:
        # Fallback: use analytics.db ordered_units and distribute to clusters by stock share
        sales_rows = analytics.execute(
            """
            SELECT sku, SUM(ordered_units) AS qty_30d,
                   SUM(CASE WHEN date >= ? THEN ordered_units ELSE 0 END) AS qty_28d,
                   SUM(CASE WHEN date >= ? THEN ordered_units ELSE 0 END) AS qty_10d,
                   SUM(revenue) AS revenue_30d
            FROM sku_analytics_daily
            WHERE date >= ?
            GROUP BY sku
        """,
            (d28, d10, d30),
        ).fetchall()

        stock_rows = fbo.execute(
            """
            SELECT sku, cluster_name, fact_stock
            FROM fbo_stock_cluster
            WHERE snapshot_date = ?
        """,
            (today,),
        ).fetchall()

        # Build stock share per cluster
        sku_total_stock: dict[str, int] = {}
        sku_cluster_stock: dict[str, dict[str, int]] = {}
        for r in stock_rows:
            sku_total_stock[r["sku"]] = sku_total_stock.get(r["sku"], 0) + r["fact_stock"]
            sku_cluster_stock.setdefault(r["sku"], {})[r["cluster_name"]] = r["fact_stock"]

        for s in sales_rows:
            sku = s["sku"]
            clusters = sku_cluster_stock.get(sku, {_DEFAULT_CLUSTER: 1})
            total = sku_total_stock.get(sku, 1) or 1
            for cluster, stock in clusters.items():
                share = stock / total
                key = (sku, cluster)
                agg[key] = {
                    "qty_10d": int((s["qty_10d"] or 0) * share),
                    "qty_28d": int((s["qty_28d"] or 0) * share),
                    "qty_30d": int((s["qty_30d"] or 0) * share),
                    "revenue_30d": float((s["revenue_30d"] or 0.0) * share),
                }

    sales_rows_db = [
        (
            sku,
            cluster,
            today,
            v["qty_10d"],
            v["qty_28d"],
            v["qty_30d"],
            v["revenue_30d"],
            round(v["qty_30d"] / 30.0, 4),
        )
        for (sku, cluster), v in agg.items()
    ]

    with fbo:
        # Same reason as fbo_stock_cluster: rebuild today's rows so a cluster that a SKU no longer
        # belongs to cannot linger from a previous run. But ONLY when the numbers came from the
        # postings API — that is the real per-cluster demand. If postings failed (Ozon 429) we are
        # holding the crude fallback (sales split by stock share); wiping the snapshot would throw
        # away a good morning sync and replace it with the guess. Then upsert only, as before.
        if sales_rows_db and postings:
            fbo.execute("DELETE FROM fbo_sales_cluster WHERE snapshot_date = ?", (today,))
        fbo.executemany(
            """
            INSERT INTO fbo_sales_cluster
                (sku, cluster_name, snapshot_date, qty_10d, qty_28d, qty_30d, revenue_30d, avg_daily_qty)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku, cluster_name, snapshot_date) DO UPDATE SET
                qty_10d      = excluded.qty_10d,
                qty_28d      = excluded.qty_28d,
                qty_30d      = excluded.qty_30d,
                revenue_30d  = excluded.revenue_30d,
                avg_daily_qty= excluded.avg_daily_qty
        """,
            sales_rows_db,
        )

    logger.info("[fbo/sync] Upserted %d sales-cluster rows for %s", len(sales_rows_db), today)


def sync_turnover(
    fbo: sqlite3.Connection,
    client: SellerClient,
) -> None:
    """Fetch /v1/analytics/turnover/stocks and save to fbo_turnover."""
    logger.info("[fbo/sync] Fetching turnover data from Ozon API...")
    try:
        rows = client.get_turnover_stocks()
    except Exception as e:
        logger.warning("[fbo/sync] Turnover API failed: %s", e)
        return

    now = datetime.utcnow().isoformat()
    with fbo:
        fbo.executemany(
            """
            INSERT INTO fbo_turnover (sku, current_stock, ads_daily, idc_days, idc_grade,
                                      turnover_days, turnover_grade, updated_at)
            VALUES (:sku, :current_stock, :ads_daily, :idc_days, :idc_grade,
                    :turnover_days, :turnover_grade, :updated_at)
            ON CONFLICT(sku) DO UPDATE SET
                current_stock  = excluded.current_stock,
                ads_daily      = excluded.ads_daily,
                idc_days       = excluded.idc_days,
                idc_grade      = excluded.idc_grade,
                turnover_days  = excluded.turnover_days,
                turnover_grade = excluded.turnover_grade,
                updated_at     = excluded.updated_at
        """,
            [{**r, "updated_at": now} for r in rows],
        )

    logger.info("[fbo/sync] Upserted %d turnover rows", len(rows))


def run_full_sync(
    sales_window_days: int = 35,
    company_id: str | None = None,
) -> None:
    """Run the complete FBO data sync pipeline for a given company.

    company_id is REQUIRED in multi-tenant context — without it both fbo and analytics
    connections fall back to legacy global DBs (data/fbo.db, data/analytics.db) and the
    Ozon client uses .env keys instead of per-company encrypted credentials.
    Background threads lose the ContextVar set by middleware, so the caller MUST pass
    company_id explicitly.
    """
    logger.info("[fbo/sync] === Starting FBO full sync (company_id=%s) ===", company_id)
    today = datetime.now().date().isoformat()

    fbo = get_fbo_connection(company_id=company_id)
    init_fbo_db(fbo)
    analytics = get_analytics_connection(company_id=company_id)
    client = SellerClient(company_id=company_id)

    try:
        # Refresh analytics.db stocks first — FBO sync reads from sku_stocks_by_warehouse,
        # so stale analytics data would produce stale FBO numbers even after sync.
        from datetime import date as _date

        from modules.analytics.stocks import sync_stocks

        try:
            with _track_step(fbo, "stocks_refresh") as st:
                logger.info("[fbo/sync] Refreshing analytics stocks from Ozon API...")
                st["rows"] = sync_stocks(analytics, client, _date.today())
                logger.info("[fbo/sync] Analytics stocks refreshed.")
        except Exception as _e:
            logger.warning("[fbo/sync] Analytics stocks refresh failed (%s), using cached data", _e)

        try:
            with _track_step(fbo, "cluster_map") as st:
                mapping = sync_cluster_map(fbo, client)
                st["rows"] = len(mapping)
        except Exception as _e:
            logger.warning(
                "[fbo/sync] cluster_map step failed (%s), continuing with cached map", _e
            )
            mapping = {}

        try:
            with _track_step(fbo, "stock_by_cluster"):
                sync_stock_by_cluster(fbo, analytics, mapping, today)
        except Exception as _e:
            logger.error("[fbo/sync] stock_by_cluster step failed: %s", _e)

        try:
            with _track_step(fbo, "sales_by_cluster"):
                sync_sales_by_cluster(fbo, analytics, client, today, sales_window_days)
        except Exception as _e:
            logger.error("[fbo/sync] sales_by_cluster step failed: %s", _e)

        # Turnover: previously swallowed errors silently — now surfaces them via fbo_sync_status.
        try:
            with _track_step(fbo, "turnover"):
                sync_turnover(fbo, client)
        except Exception as _e:
            logger.warning("[fbo/sync] turnover step failed: %s", _e)

        try:
            with _track_step(fbo, "compute"):
                from modules.fbo.analytics import compute_all

                compute_all(fbo, analytics, today)
        except Exception as _e:
            logger.error("[fbo/sync] compute step failed: %s", _e)

        logger.info("[fbo/sync] === FBO sync complete (company_id=%s) ===", company_id)
    finally:
        fbo.close()
        analytics.close()


def _get_default_company_id() -> str | None:
    """For CLI / one-shot scripts: pick the first company in auth.db."""
    try:
        from shared.auth.models import get_all_companies

        companies = get_all_companies()
        if companies:
            return str(companies[0]["id"])
    except Exception:
        pass
    return None


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Run full FBO sync for one company")
    parser.add_argument("--company-id", default=None, help="Company UUID (default: first in DB)")
    args = parser.parse_args()
    cid = args.company_id or _get_default_company_id()
    if not cid:
        print("ERROR: no companies found and --company-id not given")
        raise SystemExit(1)
    print(f"Company: {cid}")
    run_full_sync(company_id=cid)
