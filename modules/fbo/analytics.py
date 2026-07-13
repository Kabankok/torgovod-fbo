"""FBO analytics: compute ABC, cluster recommendations, and per-SKU summaries.

Called after sync to populate fbo_sku_summary and fbo_cluster_recommendations.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

TARGET_COVERAGE_CAP_DAYS = 120  # hard cap: never recommend more than N days of stock
DEFICIT_DAYS_THRESHOLD = 7  # days_to_zero < 7 → ДЕФИЦИТ
RISK_DAYS_THRESHOLD = 14  # days_to_zero < 14 → РИСК
SURPLUS_DAYS_THRESHOLD = 90  # days_to_zero > 90 and rec == 0 → ИЗБЫТОК
MIN_SALES_FOR_TREND = 10  # qty_30d must reach this before trend blending is trusted
SALES_CAP_MULTIPLIER = 5  # recommendation never exceeds qty_28d × this

# Fallback cluster for SKUs that sell only via FBS and have no FBO history at all.
# When such a SKU shows up with positive demand but no clusters yet, we attribute
# everything to the largest hub so the user at least sees a recommendation.
# (Long-term fix tracked in plans/2026-05-04-fbs-cluster-tracking.md.)
_DEFAULT_FBS_FALLBACK_CLUSTER = "Москва, МО и Дальние регионы"

# Forward-looking coverage target in days, by ABC priority.
# Logic: maintain enough stock at the cluster to cover lead time + safety buffer.
# Old formula (qty_28d × buffer=1.0) was equivalent to ~28 days → fix to 60–90 days.
_TARGET_DAYS_BY_PRIORITY: dict[str, int] = {
    "A": 90,  # top sellers: ~3 months, never risk a stockout
    "B": 60,  # mid sellers: ~2 months
    "C": 60,  # slow sellers: still ~2 months (was effectively 28 days — the bug)
}

# Ozon IDC urgency multipliers: if Ozon flags understocking, be more aggressive
_IDC_GRADE_MULTIPLIER: dict[str, float] = {
    "RED": 1.30,
    "YELLOW": 1.15,
    "GREEN": 1.00,
}


def _abc_category(cumulative_share: float) -> str:
    if cumulative_share <= 0.80:
        return "A"
    if cumulative_share <= 0.95:
        return "B"
    return "C"


def _target_coverage_days(abc_rev: str, abc_qty: str, idc_grade: str) -> float:
    """Forward-looking days of stock to maintain at a cluster.

    Uses the "stronger" ABC dimension so that A/C and C/A both get priority treatment.
    Boosts target when Ozon's IDC grade signals understocking.
    """
    if "A" in (abc_rev, abc_qty):
        base = _TARGET_DAYS_BY_PRIORITY["A"]
    elif "B" in (abc_rev, abc_qty):
        base = _TARGET_DAYS_BY_PRIORITY["B"]
    else:
        base = _TARGET_DAYS_BY_PRIORITY["C"]
    multiplier = _IDC_GRADE_MULTIPLIER.get(_normalize_grade(idc_grade), 1.0)
    return base * multiplier


def _sku_status(days_to_zero: float | None, total_recommendation: int) -> str:
    if days_to_zero is not None and days_to_zero < DEFICIT_DAYS_THRESHOLD:
        return "ДЕФИЦИТ"
    if (
        days_to_zero is not None and days_to_zero < RISK_DAYS_THRESHOLD
    ) or total_recommendation > 0:
        return "РИСК"
    if (
        days_to_zero is not None
        and days_to_zero > SURPLUS_DAYS_THRESHOLD
        and total_recommendation == 0
    ):
        return "ИЗБЫТОК"
    return "НОРМА"


def compute_all(
    fbo: sqlite3.Connection,
    analytics: sqlite3.Connection,
    today: str,
) -> None:
    """Run full analytics computation for today's snapshot."""
    logger.info("[fbo/analytics] Computing ABC and recommendations...")

    # Which snapshot do we read stock from? Normally today's. But if the stock step failed
    # (Ozon down, network, rate limit), sync_all logs the error and calls compute_all anyway —
    # and reading today's empty snapshot would silently rewrite every SKU with fact_stock = 0,
    # i.e. "you have nothing anywhere, ship everything". Fall back to the last real snapshot:
    # slightly stale numbers, honestly dated in the UI, instead of confidently wrong zeros.
    stock_date = today
    if not fbo.execute(
        "SELECT 1 FROM fbo_stock_cluster WHERE snapshot_date = ? LIMIT 1", (today,)
    ).fetchone():
        row = fbo.execute("SELECT MAX(snapshot_date) FROM fbo_stock_cluster").fetchone()
        if row and row[0]:
            stock_date = row[0]
            logger.warning(
                "[fbo/analytics] no stock snapshot for %s — falling back to %s", today, stock_date
            )
        else:
            logger.warning("[fbo/analytics] no stock snapshot at all — stock treated as empty")

    # ── 1. Load products from analytics.db ──────────────────────────────────
    products: dict[str, dict[str, str]] = {}
    for row in analytics.execute("SELECT sku, offer_id, name FROM products"):
        products[row["sku"]] = {"offer_id": row["offer_id"] or "", "name": row["name"] or ""}

    # ── 2. Load per-SKU aggregate sales (FBO + FBS combined) ─────────────────
    # PRIMARY SOURCE: sku_analytics_daily — aggregated from /v1/analytics/data, contains
    # both FBO and FBS sales. Previously we read from fbo_sales_cluster, which is built
    # from get_fbo_postings() and so SEES ONLY FBO orders. For any SKU that sells mostly
    # via FBS (e.g. 8-CR-MGF-PLO-03: 49 шт/30d real vs 1 шт/30d in FBO postings) the
    # ABC, avg_daily, days_to_zero and recommendations were all wildly underestimated.
    # Reading from sku_analytics_daily fixes this for the SKU summary; per-cluster
    # split for recommendations is handled below in step 7 with stock-share fallback.
    sales_by_sku: dict[str, dict[str, Any]] = {}
    d30_from = _days_ago_str(today, 30)
    d28_from = _days_ago_str(today, 28)
    d10_from = _days_ago_str(today, 10)
    for row in analytics.execute(
        """
        SELECT sku,
               SUM(ordered_units) AS qty_30d,
               SUM(CASE WHEN date >= ? THEN ordered_units ELSE 0 END) AS qty_28d,
               SUM(CASE WHEN date >= ? THEN ordered_units ELSE 0 END) AS qty_10d,
               SUM(revenue) AS revenue_30d
        FROM sku_analytics_daily
        WHERE date >= ?
        GROUP BY sku
    """,
        (d28_from, d10_from, d30_from),
    ):
        sales_by_sku[row["sku"]] = {
            "qty_10d": row["qty_10d"] or 0,
            "qty_28d": row["qty_28d"] or 0,
            "qty_30d": row["qty_30d"] or 0,
            "revenue_30d": row["revenue_30d"] or 0.0,
            "avg_daily_qty": round((row["qty_30d"] or 0) / 30.0, 4),
        }

    # FALLBACK: SKUs present in fbo_sales_cluster but missing from sku_analytics_daily
    # (analytics-sync hasn't run for them). Keeps old behaviour for edge cases — never
    # downgrades data we already have.
    fbo_only_rows = fbo.execute(
        """
        SELECT sku,
               SUM(qty_10d) AS qty_10d,
               SUM(qty_28d) AS qty_28d,
               SUM(qty_30d) AS qty_30d,
               SUM(revenue_30d) AS revenue_30d
        FROM fbo_sales_cluster
        WHERE snapshot_date = ?
        GROUP BY sku
    """,
        (today,),
    ).fetchall()
    for r in fbo_only_rows:
        if r["sku"] in sales_by_sku:
            continue
        sales_by_sku[r["sku"]] = {
            "qty_10d": r["qty_10d"] or 0,
            "qty_28d": r["qty_28d"] or 0,
            "qty_30d": r["qty_30d"] or 0,
            "revenue_30d": r["revenue_30d"] or 0.0,
            "avg_daily_qty": round((r["qty_30d"] or 0) / 30.0, 4),
        }

    # ── 3. Load total FBO stock per SKU ──────────────────────────────────────
    stock_by_sku: dict[str, dict[str, int]] = {}
    for row in fbo.execute(
        """
        SELECT sku, SUM(fact_stock) AS fact, SUM(in_transit) AS transit
        FROM fbo_stock_cluster
        WHERE snapshot_date = ?
        GROUP BY sku
    """,
        (stock_date,),
    ):
        stock_by_sku[row["sku"]] = {
            "fact": row["fact"] or 0,
            "transit": row["transit"] or 0,
        }

    # ── 4. Load turnover data ─────────────────────────────────────────────────
    turnover_by_sku: dict[str, dict[str, Any]] = {}
    for row in fbo.execute("SELECT * FROM fbo_turnover"):
        turnover_by_sku[row["sku"]] = dict(row)

    # ── 5. ABC analysis ───────────────────────────────────────────────────────
    all_skus = set(sales_by_sku) | set(stock_by_sku)

    total_revenue_30 = sum(sales_by_sku.get(s, {}).get("revenue_30d", 0.0) for s in all_skus)
    total_qty_30 = sum(sales_by_sku.get(s, {}).get("qty_30d", 0) for s in all_skus)

    # Sort by revenue desc for ABC by revenue
    sorted_by_rev = sorted(
        all_skus, key=lambda s: sales_by_sku.get(s, {}).get("revenue_30d", 0.0), reverse=True
    )
    sorted_by_qty = sorted(
        all_skus, key=lambda s: sales_by_sku.get(s, {}).get("qty_30d", 0), reverse=True
    )

    abc_revenue: dict[str, str] = {}
    cum = 0.0
    for sku in sorted_by_rev:
        cum += sales_by_sku.get(sku, {}).get("revenue_30d", 0.0)
        abc_revenue[sku] = _abc_category(cum / total_revenue_30 if total_revenue_30 else 1.0)

    abc_qty: dict[str, str] = {}
    cum = 0
    for sku in sorted_by_qty:
        cum += sales_by_sku.get(sku, {}).get("qty_30d", 0)
        abc_qty[sku] = _abc_category(cum / total_qty_30 if total_qty_30 else 1.0)

    # ── 6. Load cluster-level data for recommendations ───────────────────────
    cluster_stock: dict[tuple[str, str], dict[str, int]] = {}
    for row in fbo.execute(
        """
        SELECT sku, cluster_name, fact_stock, in_transit
        FROM fbo_stock_cluster
        WHERE snapshot_date = ?
    """,
        (stock_date,),
    ):
        cluster_stock[(row["sku"], row["cluster_name"])] = {
            "fact": row["fact_stock"] or 0,
            "transit": row["in_transit"] or 0,
        }

    # Same fallback as stock: a failed sales step must not erase the per-cluster demand split.
    sales_date = today
    if not fbo.execute(
        "SELECT 1 FROM fbo_sales_cluster WHERE snapshot_date = ? LIMIT 1", (today,)
    ).fetchone():
        row = fbo.execute("SELECT MAX(snapshot_date) FROM fbo_sales_cluster").fetchone()
        if row and row[0]:
            sales_date = row[0]
            logger.warning(
                "[fbo/analytics] no sales snapshot for %s — falling back to %s", today, sales_date
            )

    cluster_sales: dict[tuple[str, str], dict[str, Any]] = {}
    for row in fbo.execute(
        """
        SELECT sku, cluster_name, qty_10d, qty_28d, qty_30d, avg_daily_qty
        FROM fbo_sales_cluster
        WHERE snapshot_date = ?
    """,
        (sales_date,),
    ):
        cluster_sales[(row["sku"], row["cluster_name"])] = {
            "qty_10d": row["qty_10d"] or 0,
            "qty_28d": row["qty_28d"] or 0,
            "qty_30d": row["qty_30d"] or 0,
            "avg_daily_qty": row["avg_daily_qty"] or 0.0,
        }

    # All unique clusters
    all_cluster_keys = set(cluster_stock) | set(cluster_sales)
    clusters_per_sku: dict[str, set[str]] = {}
    for sku, cluster in all_cluster_keys:
        clusters_per_sku.setdefault(sku, set()).add(cluster)

    # Virtual fallback cluster for SKUs that sell (likely FBS) but have no FBO history
    # at all (no stock + no FBO postings). Without this branch the main loop skips
    # them entirely and the user sees status=НОРМА with rec=0 even though demand is real.
    for sku in all_skus:
        if sku in clusters_per_sku:
            continue
        if (sales_by_sku.get(sku, {}).get("qty_30d", 0) or 0) > 0:
            clusters_per_sku[sku] = {_DEFAULT_FBS_FALLBACK_CLUSTER}

    # ── 7. Compute cluster recommendations ───────────────────────────────────
    cluster_rec_rows: list[tuple] = []
    total_rec_by_sku: dict[str, int] = {}

    for sku in all_skus:
        ar = abc_revenue.get(sku, "C")
        aq = abc_qty.get(sku, "C")
        t = turnover_by_sku.get(sku, {})
        idc_grade = _normalize_grade(t.get("idc_grade", ""))
        # Ozon's own daily-sales estimate at SKU level (ads_daily from turnover API).
        # Used as a floor when cluster-level history is sparse.
        ozon_ads_daily_sku = float(t.get("ads_daily") or 0.0)

        # Stock-share denominator for distributing ozon_ads_daily to clusters
        sku_clusters = clusters_per_sku.get(sku, set())
        sku_total_fact = sum(cluster_stock.get((sku, c), {"fact": 0})["fact"] for c in sku_clusters)

        # SKU-level totals (FBO + FBS) — used as fallback when per-cluster
        # FBO postings have no signal but the SKU is selling well via FBS.
        sku_sales = sales_by_sku.get(sku, {})
        sku_total_qty28 = sku_sales.get("qty_28d", 0) or 0
        sku_total_qty30 = sku_sales.get("qty_30d", 0) or 0
        sku_total_qty10 = sku_sales.get("qty_10d", 0) or 0
        sku_total_avg = sku_sales.get("avg_daily_qty", 0.0) or 0.0

        # FBO-postings totals across all of this SKU's clusters
        sku_fbo_qty28 = sum(
            (cluster_sales.get((sku, c), {}).get("qty_28d") or 0) for c in sku_clusters
        )
        sku_fbo_qty30 = sum(
            (cluster_sales.get((sku, c), {}).get("qty_30d") or 0) for c in sku_clusters
        )
        sku_fbo_qty10 = sum(
            (cluster_sales.get((sku, c), {}).get("qty_10d") or 0) for c in sku_clusters
        )

        # FBS demand = total Ozon analytics minus what we already accounted for via
        # FBO postings. Distribute this remainder across clusters by stock share
        # (or evenly if there's no stock anywhere). This is what makes FBS-dominant
        # SKUs (e.g. 8-CR-MGF-PLO-03: 49/30d total, 1/30d FBO-only) actually surface
        # a meaningful "везти на FBO" recommendation.
        fbs_qty28 = max(0, sku_total_qty28 - sku_fbo_qty28)
        fbs_qty30 = max(0, sku_total_qty30 - sku_fbo_qty30)
        fbs_qty10 = max(0, sku_total_qty10 - sku_fbo_qty10)

        sku_total_rec = 0
        for cluster in sku_clusters:
            cs = cluster_stock.get((sku, cluster), {"fact": 0, "transit": 0})
            sale = dict(
                cluster_sales.get(
                    (sku, cluster),
                    {
                        "qty_10d": 0,
                        "qty_28d": 0,
                        "qty_30d": 0,
                        "avg_daily_qty": 0.0,
                    },
                )
            )

            # Top up cluster sale with the FBS share. NEVER subtracts — only adds
            # whatever was missing from FBO postings, so the existing FBO-cluster
            # signal is preserved verbatim.
            if fbs_qty28 > 0:
                if sku_total_fact > 0:
                    share = cs["fact"] / sku_total_fact
                else:
                    share = 1.0 / max(1, len(sku_clusters))
                sale["qty_10d"] = (sale["qty_10d"] or 0) + int(fbs_qty10 * share)
                sale["qty_28d"] = (sale["qty_28d"] or 0) + int(fbs_qty28 * share)
                sale["qty_30d"] = (sale["qty_30d"] or 0) + int(fbs_qty30 * share)
                sale["avg_daily_qty"] = sale["qty_30d"] / 30.0

            qty_28d = sale["qty_28d"]
            fact = cs["fact"]
            transit = cs["transit"]

            # ── Demand signal ────────────────────────────────────────────────
            # Primary: cluster 30-day average (stable baseline)
            avg_30d = sale["avg_daily_qty"]  # qty_30d / 30

            # Recent trend: blend in only when sample is large enough to be meaningful.
            # With fewer than MIN_SALES_FOR_TREND units in 30d any short-window spike
            # is noise, not signal — applying the blend inflates recommendations badly.
            daily_10d = (sale["qty_10d"] or 0) / 10.0
            qty_30d = sale["qty_30d"] or 0
            if daily_10d > avg_30d and qty_30d >= MIN_SALES_FOR_TREND:
                avg_qty = daily_10d * 0.6 + avg_30d * 0.4
            else:
                avg_qty = avg_30d

            # Ozon floor: if cluster history is sparse, use Ozon's ads_daily
            # distributed proportionally to cluster stock share
            if ozon_ads_daily_sku > 0:
                cluster_share = (
                    cs["fact"] / sku_total_fact
                    if sku_total_fact > 0
                    else 1.0 / max(1, len(clusters_per_sku.get(sku, set())))
                )
                ozon_cluster_daily = ozon_ads_daily_sku * cluster_share
                avg_qty = max(avg_qty, ozon_cluster_daily)

            # ── Forward-looking target ───────────────────────────────────────
            # How many days of stock to maintain at the cluster.
            # Old formula (qty_28d × buffer) was backward-looking and gave
            # rec = qty_28d when stock = 0, which is wrong.
            target_days = _target_coverage_days(ar, aq, idc_grade)
            target = avg_qty * target_days
            raw_rec = math.ceil(target - fact - transit)
            raw_rec = max(0, raw_rec)

            # Hard cap: never order more than TARGET_COVERAGE_CAP_DAYS of supply
            if avg_qty > 0:
                max_stock = math.ceil(avg_qty * TARGET_COVERAGE_CAP_DAYS)
                max_rec = max(0, math.ceil(max_stock - fact - transit))
                raw_rec = min(raw_rec, max_rec)

            # Sanity cap: recommendation cannot exceed SALES_CAP_MULTIPLIER × qty_28d.
            # Guards against formula outliers on items with zero recent history.
            if qty_28d > 0:
                raw_rec = min(raw_rec, qty_28d * SALES_CAP_MULTIPLIER)
            elif (sale["qty_30d"] or 0) == 0:
                raw_rec = 0

            sku_total_rec += raw_rec
            cluster_rec_rows.append(
                (
                    sku,
                    cluster,
                    today,
                    fact,
                    transit,
                    qty_28d,
                    avg_qty,
                    target_days,
                    raw_rec,
                )
            )

        total_rec_by_sku[sku] = sku_total_rec

    with fbo:
        # Rebuild today's recommendations rather than upserting over them: if a warehouse
        # changed cluster, the stale row would keep recommending a shipment to the cluster
        # the stock never actually sat in.
        if cluster_rec_rows:
            fbo.execute("DELETE FROM fbo_cluster_recommendations WHERE snapshot_date = ?", (today,))
        fbo.executemany(
            """
            INSERT INTO fbo_cluster_recommendations
                (sku, cluster_name, snapshot_date, fact_stock, in_transit,
                 qty_28d, avg_daily_qty, target_days, recommendation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku, cluster_name, snapshot_date) DO UPDATE SET
                fact_stock    = excluded.fact_stock,
                in_transit    = excluded.in_transit,
                qty_28d       = excluded.qty_28d,
                avg_daily_qty = excluded.avg_daily_qty,
                target_days   = excluded.target_days,
                recommendation = excluded.recommendation
        """,
            cluster_rec_rows,
        )

    logger.info("[fbo/analytics] Written %d cluster recommendations", len(cluster_rec_rows))

    # ── 8. Compute per-SKU summary ────────────────────────────────────────────
    summary_rows: list[tuple] = []
    for sku in all_skus:
        prod = products.get(sku, {"offer_id": "", "name": ""})
        sales = sales_by_sku.get(sku, {})
        stock = stock_by_sku.get(sku, {"fact": 0, "transit": 0})
        t = turnover_by_sku.get(sku, {})

        avg_daily = sales.get("avg_daily_qty", 0.0)
        fact = stock["fact"]
        transit = stock["transit"]

        days_to_zero: float | None = None
        actual_turnover: float | None = None
        if avg_daily > 0:
            days_to_zero = round(fact / avg_daily, 1)
            actual_turnover = round((fact + transit) / avg_daily, 1)

        total_rec = total_rec_by_sku.get(sku, 0)
        status = _sku_status(days_to_zero, total_rec)

        summary_rows.append(
            (
                sku,
                prod["offer_id"],
                prod["name"],
                today,
                abc_revenue.get(sku, "C"),
                abc_qty.get(sku, "C"),
                sales.get("qty_10d", 0),
                sales.get("qty_28d", 0),
                sales.get("qty_30d", 0),
                sales.get("revenue_30d", 0.0),
                avg_daily,
                fact,
                transit,
                days_to_zero,
                actual_turnover,
                t.get("idc_days"),
                t.get("idc_grade"),
                t.get("turnover_days"),
                t.get("turnover_grade"),
                total_rec,
                status,
            )
        )

    with fbo:
        fbo.executemany(
            """
            INSERT INTO fbo_sku_summary
                (sku, offer_id, name, snapshot_date,
                 abc_revenue, abc_qty,
                 qty_10d, qty_28d, qty_30d, revenue_30d, avg_daily_qty,
                 fact_stock, in_transit,
                 days_to_zero, actual_turnover,
                 ozon_idc_days, ozon_idc_grade, ozon_turnover, ozon_grade,
                 total_recommendation, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(sku) DO UPDATE SET
                offer_id            = excluded.offer_id,
                name                = excluded.name,
                snapshot_date       = excluded.snapshot_date,
                abc_revenue         = excluded.abc_revenue,
                abc_qty             = excluded.abc_qty,
                qty_10d             = excluded.qty_10d,
                qty_28d             = excluded.qty_28d,
                qty_30d             = excluded.qty_30d,
                revenue_30d         = excluded.revenue_30d,
                avg_daily_qty       = excluded.avg_daily_qty,
                fact_stock          = excluded.fact_stock,
                in_transit          = excluded.in_transit,
                days_to_zero        = excluded.days_to_zero,
                actual_turnover     = excluded.actual_turnover,
                ozon_idc_days       = excluded.ozon_idc_days,
                ozon_idc_grade      = excluded.ozon_idc_grade,
                ozon_turnover       = excluded.ozon_turnover,
                ozon_grade          = excluded.ozon_grade,
                total_recommendation= excluded.total_recommendation,
                status              = excluded.status
        """,
            summary_rows,
        )

        # fbo_sku_summary is keyed by sku alone (no snapshot_date in the key), so a SKU that
        # disappeared from the catalogue is never overwritten and lingers forever — in the table
        # and in the header counters. Every row this run touched carries snapshot_date = today,
        # so anything left on an older date is stale.
        #
        # But only prune when this run actually SAW the sales window. summary_rows covers
        # sales ∪ stock, so with an empty sku_analytics_daily (a first run, or a user returning
        # after more than 30 days — sales are synced AFTER this step) it shrinks to the few SKUs
        # that still hold FBO stock. The list is non-empty, so a bare `if summary_rows` guard
        # would happily delete everything else: measured at 6783 of 7197 products wiped from
        # "Что грузить". No sales window → no pruning; the next full run cleans up instead.
        # (Deleting by a NOT IN list of SKUs would also blow past SQLite's bound-parameter limit.)
        if summary_rows and sales_by_sku:
            removed = fbo.execute(
                "DELETE FROM fbo_sku_summary WHERE snapshot_date <> ?", (today,)
            ).rowcount
            if removed:
                logger.info("[fbo/analytics] Removed %d stale SKU summaries", removed)
        elif summary_rows:
            logger.warning(
                "[fbo/analytics] sales window empty — keeping %d existing SKU rows untouched",
                fbo.execute("SELECT COUNT(*) FROM fbo_sku_summary").fetchone()[0],
            )

    logger.info("[fbo/analytics] Written %d SKU summaries", len(summary_rows))


def _days_ago_str(today: str, days: int) -> str:
    from datetime import date, timedelta

    d = date.fromisoformat(today) - timedelta(days=days)
    return d.isoformat()


def _normalize_grade(grade: str) -> str:
    """Normalize grade strings from different API versions."""
    grade = grade.upper()
    if "GREEN" in grade:
        return "GREEN"
    if "YELLOW" in grade:
        return "YELLOW"
    if "RED" in grade:
        return "RED"
    return grade


def format_sku_for_telegram(detail: dict[str, Any]) -> str:
    """Format SKU detail as a Telegram message for the warehouse manager."""
    name = detail.get("name") or "—"
    offer_id = detail.get("offer_id") or detail.get("sku", "")
    abc_r = detail.get("abc_revenue") or "?"
    abc_q = detail.get("abc_qty") or "?"
    fact = detail.get("fact_stock") or 0
    transit = detail.get("in_transit") or 0
    days = detail.get("days_to_zero")
    avg = detail.get("avg_daily_qty") or 0.0
    total_rec = detail.get("total_recommendation") or 0
    status = detail.get("status") or "НОРМА"

    # Status emoji
    emoji = {"ДЕФИЦИТ": "🔴", "РИСК": "🟡", "ИЗБЫТОК": "🔵", "НОРМА": "🟢"}.get(status, "⚪")

    days_str = f"{days:.1f} дн." if days is not None else "н/д"

    lines = [
        f"📦 <b>{name}</b>",
        f"Артикул: <code>{offer_id}</code>  |  ABC: {abc_r}/{abc_q}  {emoji}",
        "",
        "📊 <b>Остатки и спрос:</b>",
        f"• Факт FBO: <b>{fact} шт</b>",
        f"• В пути: {transit} шт",
        f"• Продажи/день: {avg:.1f} шт",
        f"• Дней покрытия: <b>{days_str}</b>",
        "",
    ]

    # Top clusters with recommendations
    clusters = [c for c in (detail.get("clusters") or []) if c.get("recommendation", 0) > 0]
    if clusters:
        lines.append("🚚 <b>Везти:</b>")
        for c in clusters[:8]:
            cname = c["cluster_name"]
            rec = c["recommendation"]
            stock = c.get("fact_stock", 0)
            sold = c.get("qty_28d", 0)
            lines.append(f"• {cname}: <b>{rec} шт</b>  (сток: {stock}, прод.28д: {sold})")
        lines.append("")
        lines.append(f"<b>Итого: {total_rec} шт к отгрузке</b>")
    else:
        lines.append("✅ Отгружать не нужно (rec=0)")

    return "\n".join(lines)
