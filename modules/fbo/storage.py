"""FBO module database: schema and helpers.

Separate DB: data/fbo.db (override with FBO_DB_PATH env var).
Reads from analytics.db for product catalog and daily stock/sales data.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

FBO_DB_PATH = Path(os.getenv("FBO_DB_PATH", "data/fbo.db"))

_SCHEMA = """
PRAGMA journal_mode=WAL;

-- Mapping: Ozon warehouse name → business cluster
CREATE TABLE IF NOT EXISTS fbo_warehouse_cluster_map (
    warehouse_name  TEXT PRIMARY KEY,
    cluster_name    TEXT NOT NULL,
    source          TEXT DEFAULT 'api'  -- 'api' | 'inferred'
);

-- Stock snapshot by cluster per SKU (from /v2/analytics/stock_on_warehouses grouped by cluster)
CREATE TABLE IF NOT EXISTS fbo_stock_cluster (
    sku             TEXT NOT NULL,
    cluster_name    TEXT NOT NULL,
    snapshot_date   TEXT NOT NULL,
    fact_stock      INTEGER DEFAULT 0,
    in_transit      INTEGER DEFAULT 0,
    reserved        INTEGER DEFAULT 0,
    PRIMARY KEY (sku, cluster_name, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_fbo_stock_cluster_date ON fbo_stock_cluster(snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_fbo_stock_cluster_sku ON fbo_stock_cluster(sku, snapshot_date DESC);

-- Aggregated sales per SKU per cluster per window (built from analytics.db postings)
CREATE TABLE IF NOT EXISTS fbo_sales_cluster (
    sku             TEXT NOT NULL,
    cluster_name    TEXT NOT NULL,
    snapshot_date   TEXT NOT NULL,
    qty_10d         INTEGER DEFAULT 0,
    qty_28d         INTEGER DEFAULT 0,
    qty_30d         INTEGER DEFAULT 0,
    revenue_30d     REAL DEFAULT 0.0,
    avg_daily_qty   REAL DEFAULT 0.0,   -- based on 30d window
    PRIMARY KEY (sku, cluster_name, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_fbo_sales_cluster_sku ON fbo_sales_cluster(sku, snapshot_date DESC);

-- Turnover data from Ozon /v1/analytics/turnover/stocks
CREATE TABLE IF NOT EXISTS fbo_turnover (
    sku             TEXT PRIMARY KEY,
    current_stock   INTEGER DEFAULT 0,
    ads_daily       REAL DEFAULT 0.0,   -- avg daily sold (Ozon's 60d calc)
    idc_days        REAL DEFAULT 0.0,   -- days of coverage
    idc_grade       TEXT,               -- 'RED' | 'YELLOW' | 'GREEN'
    turnover_days   REAL DEFAULT 0.0,   -- turnover in days
    turnover_grade  TEXT,               -- 'RED' | 'YELLOW' | 'GREEN'
    updated_at      TEXT NOT NULL
);

-- Per-SKU summary row: all calculated metrics for main table
CREATE TABLE IF NOT EXISTS fbo_sku_summary (
    sku             TEXT PRIMARY KEY,
    offer_id        TEXT,
    name            TEXT,
    snapshot_date   TEXT NOT NULL,
    -- ABC
    abc_revenue     TEXT,  -- 'A' | 'B' | 'C'
    abc_qty         TEXT,
    -- Sales aggregates (FBO + FBS from analytics.db)
    qty_10d         INTEGER DEFAULT 0,
    qty_28d         INTEGER DEFAULT 0,
    qty_30d         INTEGER DEFAULT 0,
    revenue_30d     REAL DEFAULT 0.0,
    avg_daily_qty   REAL DEFAULT 0.0,
    -- Stock
    fact_stock      INTEGER DEFAULT 0,
    in_transit      INTEGER DEFAULT 0,
    -- Calculated
    days_to_zero    REAL,    -- NULL if no demand
    actual_turnover REAL,
    -- Ozon turnover API
    ozon_idc_days   REAL,
    ozon_idc_grade  TEXT,
    ozon_turnover   REAL,
    ozon_grade      TEXT,
    -- Recommendation (sum across all clusters)
    total_recommendation INTEGER DEFAULT 0,
    -- Status
    status          TEXT DEFAULT 'НОРМА'  -- 'ДЕФИЦИТ' | 'РИСК' | 'НОРМА' | 'ИЗБЫТОК'
);
CREATE INDEX IF NOT EXISTS idx_fbo_sku_summary_status ON fbo_sku_summary(status);
CREATE INDEX IF NOT EXISTS idx_fbo_sku_summary_rec ON fbo_sku_summary(total_recommendation DESC);

-- Cluster-level recommendations per SKU
CREATE TABLE IF NOT EXISTS fbo_cluster_recommendations (
    sku             TEXT NOT NULL,
    cluster_name    TEXT NOT NULL,
    snapshot_date   TEXT NOT NULL,
    fact_stock      INTEGER DEFAULT 0,
    in_transit      INTEGER DEFAULT 0,
    qty_28d         INTEGER DEFAULT 0,
    avg_daily_qty   REAL DEFAULT 0.0,
    target_days     REAL DEFAULT 60.0,  -- forward coverage target in days (was buffer_factor)
    recommendation  INTEGER DEFAULT 0,
    PRIMARY KEY (sku, cluster_name, snapshot_date)
);
CREATE INDEX IF NOT EXISTS idx_fbo_cluster_rec_sku ON fbo_cluster_recommendations(sku, snapshot_date DESC);

-- Manual settings per SKU (preserved across syncs)
CREATE TABLE IF NOT EXISTS fbo_sku_settings (
    sku             TEXT PRIMARY KEY,
    is_active       INTEGER DEFAULT 1,   -- 1=грузим, 0=не грузим
    tags            TEXT DEFAULT '',
    comment         TEXT DEFAULT '',
    to_order        INTEGER DEFAULT 0,   -- в заказ поставщику
    priority        REAL DEFAULT 1.0,    -- коэффициент приоритета
    updated_at      TEXT NOT NULL
);

-- Telegram session state for warehouse bot
CREATE TABLE IF NOT EXISTS fbo_telegram_sessions (
    chat_id         TEXT PRIMARY KEY,
    last_sku        TEXT,
    last_offer_id   TEXT,
    updated_at      TEXT NOT NULL
);

-- Supply orders cached from Ozon API
CREATE TABLE IF NOT EXISTS fbo_supply_orders (
    supply_order_id  INTEGER PRIMARY KEY,
    order_number     TEXT,
    state            TEXT,
    cluster          TEXT,
    timeslot_from    TEXT,
    timeslot_to      TEXT,
    car_number       TEXT,
    car_model        TEXT,
    driver_name      TEXT,
    driver_phone     TEXT,
    cargo_type       TEXT,
    cargo_count      INTEGER,
    raw_json         TEXT,
    last_synced      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fbo_supply_order_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    supply_order_id  INTEGER NOT NULL,
    state            TEXT NOT NULL,
    recorded_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fbo_order_history ON fbo_supply_order_history(supply_order_id, recorded_at DESC);

-- Per-step status of last full-sync run (one row per step).
-- Updated on each /api/fbo/sync — lets UI show what failed without digging logs.
CREATE TABLE IF NOT EXISTS fbo_sync_status (
    step           TEXT PRIMARY KEY,
    status         TEXT NOT NULL,         -- 'running' | 'ok' | 'error'
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    duration_ms    INTEGER,
    rows_affected  INTEGER,
    error_message  TEXT
);

-- Cluster-level settings (active/disabled, lead time, fallback)
CREATE TABLE IF NOT EXISTS fbo_cluster_settings (
    cluster_name    TEXT PRIMARY KEY,
    is_active       INTEGER DEFAULT 1,
    priority        INTEGER DEFAULT 0,   -- 0 = auto-sort by sales
    lead_time_days  INTEGER DEFAULT 8,
    fallback_cluster TEXT,               -- NULL = auto (next ranked active cluster)
    updated_at      TEXT NOT NULL
);
"""

_MIGRATIONS: list[str] = [
    "CREATE TABLE IF NOT EXISTS fbo_cluster_settings (cluster_name TEXT PRIMARY KEY, is_active INTEGER DEFAULT 1, priority INTEGER DEFAULT 0, lead_time_days INTEGER DEFAULT 8, fallback_cluster TEXT, updated_at TEXT NOT NULL)",
    "ALTER TABLE fbo_cluster_recommendations RENAME COLUMN buffer_factor TO target_days",
    "CREATE TABLE IF NOT EXISTS fbo_sync_status (step TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, duration_ms INTEGER, rows_affected INTEGER, error_message TEXT)",
]

# Geographic proximity: for each cluster, ordered list of nearest alternatives.
# Used when a cluster is disabled — demand redistributes to the first active cluster in the list.
_CLUSTER_NEAREST: dict[str, list[str]] = {
    "Москва, МО и Дальние регионы": ["Тверь", "Ярославль", "Воронеж"],
    "Тверь": ["Москва, МО и Дальние регионы", "СПБ (СЗО)", "Ярославль"],
    "Ярославль": ["Москва, МО и Дальние регионы", "Тверь", "Воронеж"],
    "Воронеж": ["Москва, МО и Дальние регионы", "Саратов", "Ростов"],
    "СПБ (СЗО)": ["Тверь", "Москва, МО и Дальние регионы", "Калининград"],
    "Калининград": ["СПБ (СЗО)", "Беларусь", "Тверь"],
    "Казань": ["Уфа", "Самара", "Ярославль"],
    "Самара": ["Саратов", "Казань", "Оренбург"],
    "Саратов": ["Самара", "Воронеж", "Казань"],
    "Уфа": ["Казань", "Оренбург", "Пермь"],
    "Оренбург": ["Уфа", "Самара", "Екатеринбург"],
    "Пермь": ["Уфа", "Екатеринбург", "Казань"],
    "Екатеринбург": ["Пермь", "Тюмень", "Уфа"],
    "Тюмень": ["Екатеринбург", "Омск", "Пермь"],
    "Омск": ["Тюмень", "Новосибирск", "Екатеринбург"],
    "Новосибирск": ["Омск", "Красноярск", "Тюмень"],
    "Красноярск": ["Новосибирск", "Дальний Восток", "Омск"],
    "Дальний Восток": ["Красноярск", "Новосибирск", "Омск"],
    "Ростов": ["Краснодар", "Воронеж", "Невинномысск"],
    "Краснодар": ["Ростов", "Кубань", "Невинномысск"],
    "Кубань": ["Краснодар", "Ростов", "Невинномысск"],
    "Невинномысск": ["Краснодар", "Ростов", "Махачкала"],
    "Махачкала": ["Невинномысск", "Астана", "Ростов"],
    "Беларусь": ["СПБ (СЗО)", "Тверь", "Москва, МО и Дальние регионы"],
    "Астана": ["Омск", "Екатеринбург", "Новосибирск"],
    "Алматы": ["Астана", "Новосибирск", "Омск"],
    "Кыргызстан": ["Алматы", "Астана", "Новосибирск"],
    "Узбекистан": ["Алматы", "Астана", "Оренбург"],
    "Армения": ["Грузия", "Невинномысск", "Махачкала"],
    "Грузия": ["Армения", "Невинномысск", "Краснодар"],
}

# Surcharge % added by Ozon for non-local FBO fulfillment (effective 06.05.2026)
_CLUSTER_SURCHARGE: dict[str, int] = {
    "Москва, МО и Дальние регионы": 8,
    "СПБ (СЗО)": 8,
    "Екатеринбург": 8,
    "Казань": 8,
    "Уфа": 8,
    "Краснодар": 8,
    "Кубань": 8,
    "Воронеж": 8,
    "Тюмень": 8,
    "Дальний Восток": 8,
    "Калининград": 8,
    "Тверь": 8,
    "Махачкала": 8,
    "Невинномысск": 8,
    "Омск": 12,
    "Оренбург": 12,
    "Пермь": 12,
    "Самара": 12,
    "Саратов": 12,
    "Ярославль": 0,
    "Ростов": 0,
    "Красноярск": 0,
    "Новосибирск": 0,
    "Беларусь": 0,
    "Астана": 0,
    "Алматы": 0,
    "Кыргызстан": 0,
    "Узбекистан": 0,
    "Армения": 0,
    "Грузия": 0,
}


_fbo_initialized_companies: set[str] = set()
_fbo_default_company_cache: str | None = None


def _resolve_default_company_id() -> str | None:
    """First company in users.db, cached. Used as fallback for background workers
    (Telegram bot, slot-hunter polling) where the request ContextVar is not set
    and the legacy data/fbo.db has been migrated away."""
    global _fbo_default_company_cache
    if _fbo_default_company_cache is not None:
        return _fbo_default_company_cache
    try:
        users_db = Path("data/users.db")
        if not users_db.exists():
            return None
        conn = sqlite3.connect(str(users_db))
        try:
            row = conn.execute("SELECT id FROM companies ORDER BY created_at LIMIT 1").fetchone()
        finally:
            conn.close()
        if row:
            _fbo_default_company_cache = str(row[0])
            return _fbo_default_company_cache
    except Exception:
        pass
    return None


def get_fbo_connection(company_id: str | None = None) -> sqlite3.Connection:
    from shared.db_pool import get_company_db, get_current_company_id

    cid = company_id or get_current_company_id() or _resolve_default_company_id()
    if cid:
        conn = get_company_db(cid)
        # FBO tables aren't in the shared company schema (kept inside this module),
        # so ensure them once per company per process. This also handles the case
        # where /fbo is opened before the first sync ever runs.
        if cid not in _fbo_initialized_companies:
            init_fbo_db(conn)
            _fbo_initialized_companies.add(cid)
        return conn
    # Legacy single-tenant fallback (no companies in DB yet — fresh install or CLI).
    FBO_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(FBO_DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_fbo_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _run_migrations(conn)


def _run_migrations(conn) -> None:
    for sql in _MIGRATIONS:
        if hasattr(conn, "execute_ddl"):
            conn.execute_ddl(sql)
        else:
            try:
                conn.execute(sql)
                conn.commit()
            except sqlite3.OperationalError:
                pass


# ── Read helpers ──────────────────────────────────────────────────────────────


def get_sku_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All SKUs with summary data for the main table, sorted by recommendation desc."""
    rows = conn.execute("""
        SELECT s.sku, s.offer_id, s.name, s.snapshot_date,
               s.abc_revenue, s.abc_qty,
               s.qty_10d, s.qty_28d, s.qty_30d, s.revenue_30d, s.avg_daily_qty,
               s.fact_stock, s.in_transit,
               s.days_to_zero, s.actual_turnover,
               s.ozon_idc_days, s.ozon_idc_grade, s.ozon_turnover, s.ozon_grade,
               s.total_recommendation, s.status,
               COALESCE(cfg.is_active, 1) AS is_active,
               COALESCE(cfg.tags, '')    AS tags,
               COALESCE(cfg.comment, '') AS comment,
               COALESCE(cfg.priority, 1.0) AS priority
        FROM fbo_sku_summary s
        LEFT JOIN fbo_sku_settings cfg ON cfg.sku = s.sku
        ORDER BY s.total_recommendation DESC, s.qty_30d DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_sku_detail(conn: sqlite3.Connection, sku: str) -> dict[str, Any] | None:
    """Full detail for one SKU including cluster recommendations."""
    row = conn.execute(
        """
        SELECT s.*, COALESCE(cfg.is_active,1) AS is_active,
               COALESCE(cfg.tags,'') AS tags, COALESCE(cfg.comment,'') AS comment,
               COALESCE(cfg.priority,1.0) AS priority, COALESCE(cfg.to_order,0) AS to_order
        FROM fbo_sku_summary s
        LEFT JOIN fbo_sku_settings cfg ON cfg.sku = s.sku
        WHERE s.sku = ?
    """,
        (sku,),
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    all_clusters = conn.execute(
        """
        SELECT r.cluster_name, r.fact_stock, r.in_transit, r.qty_28d, r.avg_daily_qty,
               r.target_days, r.recommendation,
               COALESCE(cfg.is_active, 1)      AS is_active,
               COALESCE(cfg.lead_time_days, 8) AS lead_time_days,
               cfg.fallback_cluster
        FROM fbo_cluster_recommendations r
        LEFT JOIN fbo_cluster_settings cfg ON cfg.cluster_name = r.cluster_name
        WHERE r.sku = ?
          AND r.snapshot_date = (
              SELECT MAX(snapshot_date) FROM fbo_cluster_recommendations WHERE sku = r.sku
          )
        ORDER BY r.recommendation DESC, r.qty_28d DESC
    """,
        (sku,),
    ).fetchall()
    result["clusters"] = _apply_redistribution([dict(c) for c in all_clusters])
    return result


def get_sku_by_offer_id(conn: sqlite3.Connection, offer_id: str) -> dict[str, Any] | None:
    """Find SKU summary by offer_id (seller article)."""
    row = conn.execute(
        """
        SELECT sku FROM fbo_sku_summary WHERE offer_id = ?
    """,
        (offer_id,),
    ).fetchone()
    if not row:
        return None
    return get_sku_detail(conn, row["sku"])


def upsert_sku_settings(
    conn: sqlite3.Connection,
    sku: str,
    *,
    is_active: int | None = None,
    tags: str | None = None,
    comment: str | None = None,
    to_order: int | None = None,
    priority: float | None = None,
) -> None:
    from datetime import datetime

    existing = conn.execute("SELECT * FROM fbo_sku_settings WHERE sku = ?", (sku,)).fetchone()
    now = datetime.utcnow().isoformat()
    if not existing:
        conn.execute(
            """
            INSERT INTO fbo_sku_settings (sku, is_active, tags, comment, to_order, priority, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                sku,
                1 if is_active is None else is_active,
                tags or "",
                comment or "",
                to_order or 0,
                priority or 1.0,
                now,
            ),
        )
    else:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [now]
        if is_active is not None:
            fields.append("is_active = ?")
            values.append(is_active)
        if tags is not None:
            fields.append("tags = ?")
            values.append(tags)
        if comment is not None:
            fields.append("comment = ?")
            values.append(comment)
        if to_order is not None:
            fields.append("to_order = ?")
            values.append(to_order)
        if priority is not None:
            fields.append("priority = ?")
            values.append(priority)
        values.append(sku)
        conn.execute(f"UPDATE fbo_sku_settings SET {', '.join(fields)} WHERE sku = ?", values)
    conn.commit()


def _find_nearest_active(cluster: str, active_names: set[str]) -> str | None:
    """Return nearest active cluster for redistribution, or None."""
    for candidate in _CLUSTER_NEAREST.get(cluster, []):
        if candidate in active_names:
            return candidate
    return next(iter(active_names)) if active_names else None


def _apply_redistribution(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Redistribute demand from disabled clusters to nearest active ones.
    Returns only active clusters, with merged_from[] showing absorbed demand.
    """
    active_map: dict[str, dict[str, Any]] = {
        c["cluster_name"]: {**c, "merged_from": []} for c in clusters if c.get("is_active", 1)
    }
    active_names = set(active_map)

    for c in clusters:
        if c.get("is_active", 1):
            continue  # skip active ones
        rec = c.get("recommendation", 0)
        if rec <= 0:
            continue  # nothing to redistribute
        # Check if manual fallback set in cluster settings (via fallback_cluster field)
        fallback = c.get("fallback_cluster") or _find_nearest_active(
            c["cluster_name"], active_names
        )
        if fallback and fallback in active_map:
            active_map[fallback]["recommendation"] += rec
            active_map[fallback]["merged_from"].append(
                {
                    "cluster": c["cluster_name"],
                    "units": rec,
                }
            )

    return sorted(active_map.values(), key=lambda x: -(x["recommendation"] or 0))


def get_cluster_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """All clusters with aggregated stats + manual settings, sorted by sales volume."""
    sales = conn.execute("""
        SELECT cluster_name,
               SUM(qty_30d)     AS qty_30d,
               SUM(qty_10d)     AS qty_10d,
               SUM(revenue_30d) AS revenue_30d
        FROM fbo_sales_cluster
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fbo_sales_cluster)
        GROUP BY cluster_name
    """).fetchall()

    stock = {
        r["cluster_name"]: dict(r)
        for r in conn.execute("""
        SELECT cluster_name,
               SUM(fact_stock) AS fact_stock,
               SUM(in_transit) AS in_transit
        FROM fbo_stock_cluster
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fbo_stock_cluster)
        GROUP BY cluster_name
    """).fetchall()
    }

    recs = {
        r["cluster_name"]: r["total_rec"]
        for r in conn.execute("""
        SELECT cluster_name, SUM(recommendation) AS total_rec
        FROM fbo_cluster_recommendations
        WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fbo_cluster_recommendations)
        GROUP BY cluster_name
    """).fetchall()
    }

    settings = {
        r["cluster_name"]: dict(r)
        for r in conn.execute("SELECT * FROM fbo_cluster_settings").fetchall()
    }

    total_qty = sum((r["qty_30d"] or 0) for r in sales)
    total_rev = sum((r["revenue_30d"] or 0.0) for r in sales)

    # Merge all clusters from sales + stock
    all_names = {r["cluster_name"] for r in sales} | set(stock)
    sales_map = {r["cluster_name"]: dict(r) for r in sales}

    result: list[dict[str, Any]] = []
    for name in all_names:
        s = sales_map.get(name, {"qty_30d": 0, "qty_10d": 0, "revenue_30d": 0.0})
        st = stock.get(name, {"fact_stock": 0, "in_transit": 0})
        cfg = settings.get(name, {})
        qty = s.get("qty_30d") or 0
        avg_daily = qty / 30.0
        fact = st.get("fact_stock") or 0
        days_cov = round(fact / avg_daily, 1) if avg_daily > 0 else None
        result.append(
            {
                "cluster_name": name,
                "qty_30d": qty,
                "qty_10d": s.get("qty_10d") or 0,
                "revenue_30d": round(s.get("revenue_30d") or 0.0, 2),
                "pct_qty": round(qty / total_qty * 100, 1) if total_qty else 0,
                "pct_rev": round((s.get("revenue_30d") or 0.0) / total_rev * 100, 1)
                if total_rev
                else 0,
                "fact_stock": fact,
                "in_transit": st.get("in_transit") or 0,
                "days_coverage": days_cov,
                "recommendation": recs.get(name) or 0,
                "surcharge_pct": _CLUSTER_SURCHARGE.get(name, 8),
                "is_active": cfg.get("is_active", 1),
                "priority": cfg.get("priority", 0),
                "lead_time_days": cfg.get("lead_time_days", 8),
                "fallback_cluster": cfg.get("fallback_cluster"),
            }
        )

    result.sort(key=lambda x: (-(x["priority"] or 0), -(x["qty_30d"] or 0)))
    for i, row in enumerate(result):
        row["rank"] = i + 1
    return result


def upsert_cluster_settings(
    conn: sqlite3.Connection,
    cluster_name: str,
    *,
    is_active: int | None = None,
    lead_time_days: int | None = None,
    fallback_cluster: str | None = ...,  # type: ignore[assignment]
    priority: int | None = None,
) -> None:
    from datetime import datetime

    now = datetime.utcnow().isoformat()
    existing = conn.execute(
        "SELECT * FROM fbo_cluster_settings WHERE cluster_name = ?", (cluster_name,)
    ).fetchone()
    if not existing:
        conn.execute(
            """
            INSERT INTO fbo_cluster_settings
                (cluster_name, is_active, priority, lead_time_days, fallback_cluster, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                cluster_name,
                1 if is_active is None else is_active,
                0 if priority is None else priority,
                8 if lead_time_days is None else lead_time_days,
                None if fallback_cluster is ... else fallback_cluster,  # type: ignore[comparison-overlap]
                now,
            ),
        )
    else:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [now]
        if is_active is not None:
            fields.append("is_active = ?")
            values.append(is_active)
        if lead_time_days is not None:
            fields.append("lead_time_days = ?")
            values.append(lead_time_days)
        if fallback_cluster is not ...:  # type: ignore[comparison-overlap]
            fields.append("fallback_cluster = ?")
            values.append(fallback_cluster)
        if priority is not None:
            fields.append("priority = ?")
            values.append(priority)
        values.append(cluster_name)
        conn.execute(
            f"UPDATE fbo_cluster_settings SET {', '.join(fields)} WHERE cluster_name = ?", values
        )
    conn.commit()


def get_fbo_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Summary stats for dashboard header.

    Includes freshness — three dates from independent sources, so UI can
    surface partial staleness (e.g. summary refreshed today, but turnover step
    failed and is still on yesterday's data).
    """
    row = conn.execute("""
        SELECT
            COUNT(*) AS total_skus,
            SUM(CASE WHEN total_recommendation > 0 THEN 1 ELSE 0 END) AS skus_to_ship,
            SUM(CASE WHEN status = 'ДЕФИЦИТ' THEN 1 ELSE 0 END) AS skus_deficit,
            SUM(CASE WHEN status = 'РИСК' THEN 1 ELSE 0 END) AS skus_risk,
            SUM(total_recommendation) AS total_units_to_ship,
            SUM(fact_stock) AS total_fact_stock,
            SUM(in_transit) AS total_in_transit,
            SUM(qty_10d) AS total_qty_10d,
            SUM(qty_30d) AS total_qty_30d,
            SUM(revenue_30d) AS total_revenue_30d,
            MAX(snapshot_date) AS last_sync
        FROM fbo_sku_summary
    """).fetchone()
    out = dict(row) if row else {}

    def _safe_max(sql: str) -> str | None:
        try:
            r = conn.execute(sql).fetchone()
            return r[0] if r else None
        except Exception:
            return None

    out["freshness"] = {
        "summary_date": out.get("last_sync"),  # MAX snapshot_date in fbo_sku_summary
        "stock_date": _safe_max("SELECT MAX(snapshot_date) FROM fbo_stock_cluster"),
        "turnover_at": _safe_max("SELECT MAX(updated_at) FROM fbo_turnover"),
    }
    return out


def get_fbo_financial_stats(
    fbo: sqlite3.Connection,
    analytics: sqlite3.Connection,
) -> dict[str, Any]:
    """Cross-DB financial metrics: stock/transit value at sell & cost price, missed revenue."""
    summary_rows = fbo.execute(
        "SELECT sku, fact_stock, in_transit, status, avg_daily_qty FROM fbo_sku_summary"
    ).fetchall()

    prices = {
        r["sku"]: (r["price"] or 0.0)
        for r in analytics.execute(
            "SELECT sku, price FROM products WHERE sku IS NOT NULL AND price IS NOT NULL"
        ).fetchall()
    }

    costs: dict[str, float] = {}
    try:
        for r in analytics.execute(
            "SELECT sku, cost_per_pack FROM sku_unit_economics "
            "WHERE sku IS NOT NULL AND cost_per_pack IS NOT NULL AND cost_per_pack > 0"
        ).fetchall():
            costs[r["sku"]] = float(r["cost_per_pack"])
    except Exception:
        pass

    stock_sell = 0.0
    stock_cost = 0.0
    transit_sell = 0.0
    transit_cost = 0.0
    missed_daily = 0.0
    missed_skus = 0

    for r in summary_rows:
        sku = r["sku"]
        price = prices.get(sku, 0.0)
        cost = costs.get(sku, 0.0)
        fact = r["fact_stock"] or 0
        transit = r["in_transit"] or 0

        stock_sell += fact * price
        transit_sell += transit * price
        if cost:
            stock_cost += fact * cost
            transit_cost += transit * cost

        if r["status"] == "ДЕФИЦИТ" and fact == 0 and price:
            missed_daily += (r["avg_daily_qty"] or 0.0) * price
            missed_skus += 1

    return {
        "stock_value_sell": round(stock_sell),
        "stock_value_cost": round(stock_cost) if stock_cost else None,
        "transit_value_sell": round(transit_sell),
        "transit_value_cost": round(transit_cost) if transit_cost else None,
        "missed_revenue_daily": round(missed_daily),
        "missed_revenue_30d": round(missed_daily * 30),
        "missed_skus": missed_skus,
    }


# ── Supply Orders CRUD ────────────────────────────────────────────────────────

import json as _json


def upsert_supply_order(conn: sqlite3.Connection, order: dict[str, Any]) -> None:
    """Save or update a supply order from Ozon API data."""
    from datetime import datetime

    conn.execute(
        """
        INSERT INTO fbo_supply_orders
            (supply_order_id, order_number, state, cluster,
             timeslot_from, timeslot_to, raw_json, last_synced)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(supply_order_id) DO UPDATE SET
            order_number  = excluded.order_number,
            state         = excluded.state,
            cluster       = excluded.cluster,
            timeslot_from = excluded.timeslot_from,
            timeslot_to   = excluded.timeslot_to,
            raw_json      = excluded.raw_json,
            last_synced   = excluded.last_synced
    """,
        (
            order["supply_order_id"],
            order.get("order_number", ""),
            order.get("state", ""),
            order.get("cluster", ""),
            order.get("timeslot_from", ""),
            order.get("timeslot_to", ""),
            _json.dumps(order, ensure_ascii=False),
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()


def delete_supply_order(conn: sqlite3.Connection, supply_order_id: int) -> None:
    """Remove a supply order and its history from local DB (does NOT affect Ozon)."""
    conn.execute(
        "DELETE FROM fbo_supply_order_history WHERE supply_order_id = ?", (supply_order_id,)
    )
    conn.execute("DELETE FROM fbo_supply_orders WHERE supply_order_id = ?", (supply_order_id,))
    conn.commit()


def record_order_history(conn: sqlite3.Connection, supply_order_id: int, state: str) -> None:
    """Add state change record if state differs from last recorded."""
    last = conn.execute(
        "SELECT state FROM fbo_supply_order_history WHERE supply_order_id = ? ORDER BY recorded_at DESC LIMIT 1",
        (supply_order_id,),
    ).fetchone()
    if last and last["state"] == state:
        return
    conn.execute(
        "INSERT INTO fbo_supply_order_history (supply_order_id, state) VALUES (?, ?)",
        (supply_order_id, state),
    )
    conn.commit()


def get_supply_orders(
    conn: sqlite3.Connection,
    states: list[str] | None = None,
) -> list[dict[str, Any]]:
    q = "SELECT * FROM fbo_supply_orders"
    params: list[Any] = []
    if states:
        placeholders = ",".join("?" * len(states))
        q += f" WHERE state IN ({placeholders})"
        params = list(states)
    q += " ORDER BY last_synced DESC"
    rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_supply_order(conn: sqlite3.Connection, supply_order_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM fbo_supply_orders WHERE supply_order_id = ?", (supply_order_id,)
    ).fetchone()
    return dict(row) if row else None


def get_supply_order_history(
    conn: sqlite3.Connection, supply_order_id: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT state, recorded_at FROM fbo_supply_order_history WHERE supply_order_id = ? ORDER BY recorded_at ASC",
        (supply_order_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_order_timeslot(
    conn: sqlite3.Connection,
    supply_order_id: int,
    timeslot_from: str,
    timeslot_to: str,
) -> None:
    """Patch only the timeslot columns of a cached supply order."""
    conn.execute(
        "UPDATE fbo_supply_orders SET timeslot_from=?, timeslot_to=?, last_synced=datetime('now') WHERE supply_order_id=?",
        (timeslot_from, timeslot_to, supply_order_id),
    )
    conn.commit()


# ── Sync status (Phase 0: per-step visibility) ───────────────────────────────


def upsert_sync_status(
    conn: sqlite3.Connection,
    step: str,
    status: str,
    *,
    started_at: str,
    finished_at: str | None = None,
    duration_ms: int | None = None,
    rows_affected: int | None = None,
    error_message: str | None = None,
) -> None:
    """Write per-step status row. UPSERT by step — last run wins."""
    conn.execute(
        """
        INSERT INTO fbo_sync_status
            (step, status, started_at, finished_at, duration_ms, rows_affected, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(step) DO UPDATE SET
            status        = excluded.status,
            started_at    = excluded.started_at,
            finished_at   = excluded.finished_at,
            duration_ms   = excluded.duration_ms,
            rows_affected = excluded.rows_affected,
            error_message = excluded.error_message
    """,
        (step, status, started_at, finished_at, duration_ms, rows_affected, error_message),
    )
    conn.commit()


def get_sync_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return per-step status of last sync run, ordered by canonical step order."""
    order = {
        "stocks_refresh": 1,
        "cluster_map": 2,
        "stock_by_cluster": 3,
        "sales_by_cluster": 4,
        "turnover": 5,
        "compute": 6,
    }
    try:
        rows = conn.execute("SELECT * FROM fbo_sync_status").fetchall()
    except Exception:
        return []
    items = [dict(r) for r in rows]
    items.sort(key=lambda r: order.get(r["step"], 99))
    return items


def get_supply_orders_stats(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) as cnt FROM fbo_supply_orders GROUP BY state"
    ).fetchall()
    counts: dict[str, int] = {r["state"]: r["cnt"] for r in rows}
    active_states = {"DATA_FILLING", "DATA_FILLED", "CONFIRMED", "APPROVED", "READY_TO_SUPPLY"}
    return {
        "active": sum(counts.get(s, 0) for s in active_states),
        "transit": counts.get("SUPPLYING", 0),
        "acceptance": counts.get("ACCEPTANCE", 0),
        "done": counts.get("SUPPLIED", 0),
    }
