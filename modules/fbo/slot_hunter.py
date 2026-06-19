"""FBO Slot Hunter: background scheduler that polls Ozon API for available timeslots.

API used (Ozon Seller API, swagger 2026-04-22):
  POST /v1/supply-order/timeslot/get    — list available timeslots for an order
  POST /v1/supply-order/timeslot/update — change timeslot (async, returns operation_id)
  POST /v1/supply-order/timeslot/status — check operation status
  POST /v3/supply-order/list            — list supply orders (for picker UI)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_BASE = "https://api-seller.ozon.ru"
# Safety: 30 sec default polling interval keeps us well under 20 req/min API limit.
# Minimum enforced interval = 30 sec regardless of job config.
_MIN_INTERVAL_SEC = 30
# How often the scheduler thread wakes to check for due jobs (seconds).
_TICK_SEC = 5
# Seconds to wait between timeslot/update and timeslot/status check.
_STATUS_POLL_DELAY = 3


def _headers() -> dict[str, str]:
    return {
        "Client-Id": os.getenv("OZON_CLIENT_ID", ""),
        "Api-Key": os.getenv("OZON_API_KEY", ""),
        "Content-Type": "application/json",
    }


# Ozon держит посекундный лимит на draft/timeslot-эндпоинтах (code 8:
# "request rate limit per second"). Разносим запросы во времени и ретраим 429.
_REQ_GAP_SEC = 1.1          # минимум между любыми запросами к этим ручкам
_RATE_BACKOFF = [1, 2, 4, 6]  # паузы перед повтором при 429, сек
_req_lock = threading.Lock()
_last_req_ts = 0.0


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    global _last_req_ts
    for attempt in range(len(_RATE_BACKOFF) + 1):
        # Разносим старты запросов минимум на _REQ_GAP_SEC (потокобезопасно).
        with _req_lock:
            gap = time.monotonic() - _last_req_ts
            if gap < _REQ_GAP_SEC:
                time.sleep(_REQ_GAP_SEC - gap)
            _last_req_ts = time.monotonic()

        r = requests.post(f"{_BASE}{path}", headers=_headers(), json=body, timeout=30)

        if r.status_code == 429 and attempt < len(_RATE_BACKOFF):
            wait = _RATE_BACKOFF[attempt]
            logger.warning("[slot_hunter] 429 на %s — пауза %ds и повтор", path, wait)
            time.sleep(wait)
            continue

        if not r.ok:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise requests.HTTPError(
                f"{r.status_code} {r.reason} for {path}: {detail}",
                response=r,
            )
        return r.json()

    # Сюда не доходим (последняя попытка либо вернёт, либо бросит выше),
    # но на всякий случай — явная ошибка.
    raise requests.HTTPError(f"429 Too Many Requests for {path}: лимит Ozon не снят")


# ── macrolocal_cluster_id → cluster name cache ────────────────────────────────
# Supply order details expose macrolocal_cluster_id in supplies[].
# Cluster names come from /v1/cluster/list (cluster_type=1).

_macro_cluster_cache: dict[str, str] = {}
_macro_cluster_loaded = False


def _load_macro_clusters() -> dict[str, str]:
    """Build {macrolocal_cluster_id: cluster_name}. Cached for process lifetime."""
    global _macro_cluster_cache, _macro_cluster_loaded
    if _macro_cluster_loaded:
        return _macro_cluster_cache
    try:
        data = _post("/v1/cluster/list", {"cluster_type": 1})
        mapping: dict[str, str] = {}
        for c in data.get("clusters") or []:
            mid = str(c.get("macrolocal_cluster_id") or "")
            name = c.get("name") or ""
            if mid and name:
                mapping[mid] = name
        _macro_cluster_cache = mapping
        _macro_cluster_loaded = True
        logger.info("[slot_hunter] loaded %d macrolocal_cluster entries", len(mapping))
    except Exception as e:
        logger.warning("[slot_hunter] cluster/list failed: %s", e)
    return _macro_cluster_cache


# ── Public API wrappers ───────────────────────────────────────────────────────


def get_available_timeslots(supply_order_id: int) -> list[dict[str, str]]:
    """Return list of {from, to} dicts for the supply order."""
    data = _post("/v1/supply-order/timeslot/get", {"supply_order_id": supply_order_id})
    return data.get("timeslots") or []


def update_timeslot(supply_order_id: int, slot_from: str, slot_to: str) -> str:
    """Start async timeslot update. Returns operation_id."""
    data = _post(
        "/v1/supply-order/timeslot/update",
        {
            "supply_order_id": supply_order_id,
            "timeslot": {"from": slot_from, "to": slot_to},
        },
    )
    return data.get("operation_id", "")


def check_update_status(operation_id: str) -> str:
    """Return STATUS_SUCCESS | STATUS_IN_PROGRESS | STATUS_ERROR."""
    data = _post("/v1/supply-order/timeslot/status", {"operation_id": operation_id})
    return data.get("status", "STATUS_ERROR")


def list_supply_orders(states: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Fetch supply orders for the picker UI.

    /v3/supply-order/list returns only order_ids; details are fetched per-order.
    sort_by must be numeric (1 = CREATED_AT); filter.states is required.
    """
    effective_states = states or ["READY_TO_SUPPLY", "DATA_FILLING"]
    body: dict[str, Any] = {
        "filter": {"states": effective_states},
        "limit": limit,
        "sort_by": 1,
        "sort_dir": "ASC",
    }
    data = _post("/v3/supply-order/list", body)
    order_ids: list[int] = data.get("order_ids") or []
    if not order_ids:
        logger.warning(
            "[slot_hunter] list_supply_orders: empty order_ids, keys=%s", list(data.keys())
        )
        return []

    macro_map = _load_macro_clusters()

    orders: list[dict[str, Any]] = []
    for oid in order_ids:
        try:
            det = _post("/v1/supply-order/details", {"order_id": oid})
            ts_val = (det.get("timeslot") or {}).get("value") or {}
            ts = ts_val.get("timeslot") or {}
            # cluster comes from supplies[0].macrolocal_cluster_id
            supplies = det.get("supplies") or []
            macro_id = str((supplies[0].get("macrolocal_cluster_id") or "") if supplies else "")
            cluster = macro_map.get(macro_id, "")
            orders.append(
                {
                    "supply_order_id": det.get("order_id") or oid,
                    "order_number": det.get("order_number", ""),
                    "state": det.get("state", ""),
                    "cluster": cluster,
                    "timeslot_from": ts.get("from", ""),
                    "timeslot_to": ts.get("to", ""),
                }
            )
        except Exception as e:
            logger.warning("[slot_hunter] details for order_id=%d: %s", oid, e)
            orders.append(
                {"supply_order_id": oid, "order_number": str(oid), "state": "", "cluster": ""}
            )
        time.sleep(0.25)
    return orders


# ── Slot matching ─────────────────────────────────────────────────────────────


def _parse_dt(s: str) -> datetime | None:
    """Parse ISO-like datetime string from Ozon API."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(s[:19], fmt[: len(s[:19])])
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        return None


def find_matching_slot(
    timeslots: list[dict[str, str]],
    target_date_from: str,
    target_date_to: str,
    target_time_from: str,
    target_time_to: str,
) -> dict[str, str] | None:
    """
    Return the first timeslot that falls within the desired date+time window.

    target_date_from / target_date_to: YYYY-MM-DD strings (inclusive).
    target_time_from / target_time_to: HH:MM strings.
    The slot is accepted when:
      - slot.from date  is between target_date_from and target_date_to
      - slot.from time  >= target_time_from
      - slot.to   time  <= target_time_to
      - at least 1 hour remains before slot.from (Ozon deadline rule)
    """
    now = datetime.utcnow()
    date_from = datetime.strptime(target_date_from, "%Y-%m-%d").date()
    date_to = datetime.strptime(target_date_to, "%Y-%m-%d").date()
    time_from_h, time_from_m = map(int, target_time_from.split(":"))
    time_to_h, time_to_m = map(int, target_time_to.split(":"))

    for slot in timeslots:
        dt_from = _parse_dt(slot.get("from", ""))
        dt_to = _parse_dt(slot.get("to", ""))
        if not dt_from or not dt_to:
            continue
        # Date range check
        if not (date_from <= dt_from.date() <= date_to):
            continue
        # Time window check
        slot_from_minutes = dt_from.hour * 60 + dt_from.minute
        slot_to_minutes = dt_to.hour * 60 + dt_to.minute
        target_from_minutes = time_from_h * 60 + time_from_m
        target_to_minutes = time_to_h * 60 + time_to_m
        if slot_from_minutes < target_from_minutes or slot_to_minutes > target_to_minutes:
            continue
        # Ozon deadline: must be > 1 hour before slot start (use UTC as approximation)
        if (dt_from - now).total_seconds() < 3600:
            continue
        return slot
    return None


# ── Telegram notification ─────────────────────────────────────────────────────


def _notify(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.warning("[slot_hunter] telegram notify error: %s", e)


# ── Scheduler ─────────────────────────────────────────────────────────────────


class SlotHunterScheduler:
    """
    Background daemon thread. Wakes every _TICK_SEC seconds,
    checks which jobs are due, and runs API polling for each.
    """

    def __init__(self) -> None:
        self._last_check: dict[int, float] = {}  # job_id → last check timestamp
        self._lock = threading.Lock()

    def start(self) -> None:
        t = threading.Thread(target=self._loop, daemon=True, name="slot-hunter")
        t.start()
        logger.info("[slot_hunter] scheduler started")

    def _loop(self) -> None:
        from modules.fbo.slot_storage import (
            get_slot_connection,
            init_slot_db,
        )

        # Ensure DB exists on first tick
        conn = get_slot_connection()
        init_slot_db(conn)
        conn.close()

        while True:
            try:
                self._tick()
            except Exception as e:
                logger.exception("[slot_hunter] tick error: %s", e)
            time.sleep(_TICK_SEC)

    def _tick(self) -> None:
        from modules.fbo.slot_storage import get_active_jobs, get_slot_connection

        conn = get_slot_connection()
        try:
            jobs = get_active_jobs(conn)
        finally:
            conn.close()

        now = time.monotonic()
        for job in jobs:
            job_id = job["id"]
            interval = max(job.get("interval_sec", 60), _MIN_INTERVAL_SEC)
            last = self._last_check.get(job_id, 0)
            if now - last < interval:
                continue
            with self._lock:
                self._last_check[job_id] = now
            # Run in separate thread so one slow job doesn't block others
            threading.Thread(
                target=self._check_job,
                args=(job,),
                daemon=True,
                name=f"slot-job-{job_id}",
            ).start()

    def _check_job(self, job: dict[str, Any]) -> None:
        from modules.fbo.slot_storage import (
            add_event,
            get_slot_connection,
            increment_checks,
            update_job_status,
        )

        job_id = job["id"]
        order_id = job["supply_order_id"]
        conn = get_slot_connection()
        try:
            increment_checks(conn, job_id)
            timeslots = get_available_timeslots(order_id)
            match = find_matching_slot(
                timeslots,
                job["target_date_from"],
                job["target_date_to"],
                job["target_time_from"],
                job["target_time_to"],
            )
            if not match:
                add_event(
                    conn,
                    job_id,
                    "check",
                    slots_count=len(timeslots),
                    message=f"Нет подходящего слота. Всего слотов: {len(timeslots)}",
                )
                logger.debug("[slot_hunter] job %d: no match (%d slots)", job_id, len(timeslots))
                return

            # Found a matching slot — try to book it
            slot_from = match["from"]
            slot_to = match["to"]
            add_event(
                conn,
                job_id,
                "found",
                slots_count=len(timeslots),
                message=f"Найден слот: {slot_from} — {slot_to}. Бронирую...",
            )
            logger.info("[slot_hunter] job %d: found slot %s–%s", job_id, slot_from, slot_to)

            try:
                operation_id = update_timeslot(order_id, slot_from, slot_to)
            except Exception as e:
                add_event(conn, job_id, "error", message=f"Ошибка update: {e}")
                logger.warning("[slot_hunter] job %d: update error: %s", job_id, e)
                return

            # Poll status up to 10 times (30 sec total)
            status = "STATUS_IN_PROGRESS"
            for _ in range(10):
                time.sleep(_STATUS_POLL_DELAY)
                try:
                    status = check_update_status(operation_id)
                except Exception as e:
                    logger.warning("[slot_hunter] job %d: status error: %s", job_id, e)
                    break
                if status != "STATUS_IN_PROGRESS":
                    break

            if status == "STATUS_SUCCESS":
                update_job_status(
                    conn, job_id, "done", found_slot_from=slot_from, found_slot_to=slot_to
                )
                add_event(
                    conn, job_id, "booked", message=f"Слот забронирован: {slot_from} — {slot_to}"
                )
                order_num = job.get("supply_order_num") or str(order_id)
                _notify(
                    f"🎯 <b>Слот найден и забронирован!</b>\n\n"
                    f"Заявка: <b>{order_num}</b>\n"
                    f"Новый слот: <b>{_fmt_slot(slot_from, slot_to)}</b>\n\n"
                    f"Бот остановлен. Успейте подготовить отгрузку."
                )
                logger.info("[slot_hunter] job %d: SUCCESS — slot booked", job_id)
            else:
                add_event(
                    conn,
                    job_id,
                    "error",
                    message=f"Бронирование не подтверждено. Статус: {status}. Продолжаю поиск.",
                )
                logger.warning("[slot_hunter] job %d: booking status=%s, retrying", job_id, status)
        except Exception as e:
            try:
                add_event(conn, job_id, "error", message=str(e))
            except Exception:
                pass
            logger.exception("[slot_hunter] job %d: unexpected error: %s", job_id, e)
        finally:
            conn.close()


# ── Draft supply order creation (new cluster-based API, swagger 2026) ─────────
#
# Correct flow:
#   1. GET  /v1/cluster/list              → clusters with macrolocal_cluster_id
#   2. POST /v1/draft/direct/create       → draft_id  (rate: 2/min, 50/h, 500/d)
#   3. Poll /v2/draft/create/info         → status SUCCESS + warehouses list
#   4. POST /v2/draft/timeslot/info       → timeslots
#   5. POST /v2/draft/supply/create       → async supply order creation
#   6. Poll /v2/draft/supply/create/status → order_id


def get_fbo_clusters() -> list[dict[str, Any]]:
    """Return Ozon FBO clusters (macroregions) for supply order creation.

    Uses /v1/cluster/list with CLUSTER_TYPE_OZON.
    Each item: {macrolocal_cluster_id, name, ...}.
    """
    data = _post("/v1/cluster/list", {"cluster_type": "CLUSTER_TYPE_OZON"})
    result = []
    for c in data.get("clusters") or []:
        result.append(
            {
                "macrolocal_cluster_id": c.get("macrolocal_cluster_id") or c.get("id"),
                "name": c.get("name") or f"Кластер {c.get('id')}",
                **c,
            }
        )
    return result


def _article_to_sku(offer_id: str) -> int:
    """Convert seller article (offer_id) to Ozon SKU via fbo_sku_summary."""
    from modules.fbo.storage import get_fbo_connection, get_sku_by_offer_id

    conn = get_fbo_connection()
    try:
        row = get_sku_by_offer_id(conn, offer_id)
        if row is None:
            raise ValueError(f"SKU not found for article '{offer_id}'")
        return int(row["sku"])
    finally:
        conn.close()


def get_drop_off_warehouses(city: str) -> list[dict[str, Any]]:
    """Search crossdock drop-off warehouses (PVZ, SC, crossdock points) by city name.

    Uses /v1/warehouse/fbo/list with filter_by_supply_type=CREATE_TYPE_CROSSDOCK.
    city must be at least 4 characters (Ozon API requirement).
    """
    if len(city.strip()) < 4:
        return []
    body = {
        "filter_by_supply_type": ["CREATE_TYPE_CROSSDOCK"],
        "search": city.strip(),
    }
    logger.info("[slot_hunter] get_drop_off_warehouses body: %s", body)
    data = _post("/v1/warehouse/fbo/list", body)
    logger.info("[slot_hunter] get_drop_off_warehouses response keys: %s", list(data.keys()))
    return data.get("search") or data.get("warehouses") or []


def create_direct_draft(
    macrolocal_cluster_id: int,
    items: list[dict[str, Any]],
) -> str:
    """Create FBO direct supply draft. Returns draft_id.

    POST /v1/draft/direct/create
    items: [{article: str, quantity: int}]  — articles are converted to SKU automatically.
    Rate limit: 2/min, 50/h, 500/day.
    """
    sku_items = []
    for item in items:
        sku = _article_to_sku(item["article"])
        sku_items.append({"sku": sku, "quantity": int(item["quantity"])})

    body = {
        "cluster_info": {
            "macrolocal_cluster_id": macrolocal_cluster_id,
            "items": sku_items,
        },
        "deletion_sku_mode": "PARTIAL",
    }
    logger.info("[slot_hunter] create_direct_draft body: %s", body)
    data = _post("/v1/draft/direct/create", body)
    logger.info("[slot_hunter] create_direct_draft response: %s", data)
    draft_id = data.get("draft_id")
    if not draft_id:
        raise ValueError(f"No draft_id in response: {data}")
    return str(draft_id)


def create_crossdock_draft(
    macrolocal_cluster_id: int,
    items: list[dict[str, Any]],
    drop_off_warehouse_id: int,
    drop_off_warehouse_type: str = "CROSS_DOCK",
) -> str:
    """Create FBO crossdock supply draft. Returns draft_id.

    POST /v1/draft/crossdock/create
    Seller ships to a drop-off point (PVZ/SC); Ozon routes to the destination warehouse.
    drop_off_warehouse_type: CROSS_DOCK | SORTING_CENTER | DELIVERY_POINT | ORDERS_RECEIVING_POINT
    Rate limit: same as direct (2/min, 50/h, 500/day).
    """
    sku_items = []
    for item in items:
        sku = _article_to_sku(item["article"])
        sku_items.append({"sku": sku, "quantity": int(item["quantity"])})

    # Search API returns "WAREHOUSE_TYPE_FOO" but draft API expects "FOO"
    _PREFIX = "WAREHOUSE_TYPE_"
    wh_type = drop_off_warehouse_type or "CROSS_DOCK"
    if wh_type.startswith(_PREFIX):
        wh_type = wh_type[len(_PREFIX) :]

    body = {
        "cluster_info": {
            "macrolocal_cluster_id": macrolocal_cluster_id,
            "items": sku_items,
        },
        "delivery_info": {
            "type": "DROPOFF",
            "drop_off_warehouse": {
                "warehouse_id": drop_off_warehouse_id,
                "warehouse_type": wh_type,
            },
        },
        "deletion_sku_mode": "PARTIAL",
    }
    logger.info("[slot_hunter] create_crossdock_draft body: %s", body)
    data = _post("/v1/draft/crossdock/create", body)
    logger.info("[slot_hunter] create_crossdock_draft response: %s", data)
    draft_id = data.get("draft_id")
    if not draft_id:
        raise ValueError(f"No draft_id in crossdock response: {data}")
    return str(draft_id)


def poll_draft_info(draft_id: str, polls: int = 20, delay: float = 3.0) -> dict[str, Any]:
    """Poll /v2/draft/create/info until status == SUCCESS. Returns full response.

    Response includes clusters[].warehouses[] with storage_warehouse_id and total_score.
    """
    for attempt in range(polls):
        time.sleep(delay)
        data = _post("/v2/draft/create/info", {"draft_id": draft_id})
        status = data.get("status", "")
        logger.info(
            "[slot_hunter] draft %s info status: %s (attempt %d)", draft_id, status, attempt + 1
        )
        if status == "SUCCESS":
            return data
        if status == "FAILED":
            errors = data.get("errors") or []
            codes = [e.get("error_message", "") for e in errors if e.get("error_message")]
            _ERR_MAP = {
                "DROP_OFF_POINT_HAS_NO_TIMESLOTS": "У выбранной точки отгрузки нет доступных слотов — выберите другую точку.",
                "NOT_AVAILABLE_TIMESLOT_FOR_DROP_OFF_POINT": "Нет слотов на точке отгрузки — выберите другую.",
                "NOT_AVAILABLE_TIMESLOT_FOR_STORAGE_WAREHOUSE": "Нет слотов на складе назначения — попробуйте позже.",
                "NOT_AVAILABLE_TIMESLOT_FOR_BOTH_WAREHOUSES": "Нет слотов ни на точке отгрузки, ни на складе назначения.",
            }
            msg = _ERR_MAP.get(codes[0], codes[0]) if codes else str(data)
            raise RuntimeError(msg)
    raise TimeoutError(f"Draft not ready in {polls * delay:.0f}s")


def get_draft_timeslots(
    draft_id: str,
    macrolocal_cluster_id: int,
    storage_warehouse_id: int | None = None,
    supply_type: str = "DIRECT",
    days_ahead: int = 14,
) -> list[dict[str, Any]]:
    """Return available timeslots for a supply draft.

    POST /v2/draft/timeslot/info — requires date_from, date_to, supply_type,
    selected_cluster_warehouses in addition to draft_id.
    supply_type: "DIRECT" | "CROSSDOCK"
    """
    import datetime

    today = datetime.date.today()
    date_from = today.isoformat()
    date_to = (today + datetime.timedelta(days=days_ahead)).isoformat()

    cluster_wh: dict[str, Any] = {"macrolocal_cluster_id": macrolocal_cluster_id}
    if storage_warehouse_id:
        cluster_wh["storage_warehouse_id"] = storage_warehouse_id

    body = {
        "draft_id": int(draft_id),
        "supply_type": supply_type,
        "date_from": date_from,
        "date_to": date_to,
        "selected_cluster_warehouses": [cluster_wh],
    }
    logger.info("[slot_hunter] get_draft_timeslots body: %s", body)
    data = _post("/v2/draft/timeslot/info", body)
    # Response: result.drop_off_warehouse_timeslots.days[].timeslots[]
    result = data.get("result") or {}
    drop_off = result.get("drop_off_warehouse_timeslots") or {}
    days = drop_off.get("days") or []
    timeslots = []
    for day in days:
        for slot in day.get("timeslots") or []:
            timeslots.append(
                {
                    "from": slot.get("from_in_timezone"),
                    "to": slot.get("to_in_timezone"),
                    "from_in_timezone": slot.get("from_in_timezone"),
                    "to_in_timezone": slot.get("to_in_timezone"),
                    "date": day.get("date_in_timezone"),
                }
            )
    logger.info("[slot_hunter] get_draft_timeslots timeslots count: %d", len(timeslots))
    return timeslots


def confirm_supply_draft_v2(
    draft_id: str,
    macrolocal_cluster_id: int,
    storage_warehouse_id: int,
    slot_from: str,
    slot_to: str,
    supply_type: str = "DIRECT",
) -> dict[str, Any]:
    """Create supply order from draft (async). Returns {draft_id, error_reasons}.

    POST /v2/draft/supply/create
    Poll /v2/draft/supply/create/status for order_id.
    supply_type: "DIRECT" | "CROSSDOCK"
    """
    body = {
        "draft_id": draft_id,
        "supply_type": supply_type,
        "selected_cluster_warehouses": [
            {
                "macrolocal_cluster_id": macrolocal_cluster_id,
                "storage_warehouse_id": storage_warehouse_id,
            }
        ],
        "timeslot": {
            "from_in_timezone": slot_from,
            "to_in_timezone": slot_to,
        },
    }
    logger.info("[slot_hunter] confirm_supply_draft_v2 body: %s", body)
    result = _post("/v2/draft/supply/create", body)
    logger.info("[slot_hunter] confirm_supply_draft_v2 response: %s", result)
    return result


def poll_supply_order_status(draft_id: str, polls: int = 20, delay: float = 3.0) -> dict[str, Any]:
    """Poll /v2/draft/supply/create/status until status != IN_PROGRESS. Returns response."""
    for attempt in range(polls):
        time.sleep(delay)
        data = _post("/v2/draft/supply/create/status", {"draft_id": draft_id})
        status = data.get("status", "")
        logger.info("[slot_hunter] supply order status: %s (attempt %d)", status, attempt + 1)
        if status in ("SUCCESS", "FAILED"):
            return data
    raise TimeoutError(f"Supply order creation did not complete in {polls * delay:.0f}s")


def create_supply_draft_full(
    macrolocal_cluster_id: int,
    items: list[dict[str, Any]],
    supply_type: str = "DIRECT",
    drop_off_warehouse_id: int | None = None,
    drop_off_warehouse_type: str = "CROSS_DOCK",
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Full flow: create draft, wait for info, get warehouses + timeslots. Blocks up to ~60s.

    Returns (draft_id, warehouses, timeslots).
    supply_type: "DIRECT" | "CROSSDOCK"
    drop_off_warehouse_id: required for CROSSDOCK — the PVZ/SC where seller ships to.
    drop_off_warehouse_type: warehouse type from Ozon API (CROSS_DOCK, SORTING_CENTER, etc.)
    """
    if supply_type == "CROSSDOCK":
        if not drop_off_warehouse_id:
            raise ValueError("drop_off_warehouse_id is required for CROSSDOCK supply")
        draft_id = create_crossdock_draft(
            macrolocal_cluster_id, items, drop_off_warehouse_id, drop_off_warehouse_type
        )
    else:
        draft_id = create_direct_draft(macrolocal_cluster_id, items)

    info = poll_draft_info(draft_id)
    # Extract warehouses from all clusters in the response
    warehouses: list[dict[str, Any]] = []
    for cluster in info.get("clusters") or []:
        for wh in cluster.get("warehouses") or []:
            wh["macrolocal_cluster_id"] = cluster.get("macrolocal_cluster_id")
            warehouses.append(wh)
    # Pick best warehouse to query timeslots for
    best_wh = (
        sorted(warehouses, key=lambda w: w.get("total_score", 0), reverse=True)[0]
        if warehouses
        else None
    )
    storage_wh_id: int | None = None
    if best_wh:
        storage_wh_id = (best_wh.get("storage_warehouse") or {}).get("warehouse_id") or best_wh.get(
            "warehouse_id"
        )
    timeslots = get_draft_timeslots(draft_id, macrolocal_cluster_id, storage_wh_id, supply_type)
    return draft_id, warehouses, timeslots


def set_timeslot_sync(supply_order_id: int, slot_from: str, slot_to: str) -> str:
    """Set timeslot for READY_TO_SUPPLY order and poll until done.
    Returns STATUS_SUCCESS | STATUS_ERROR.
    Blocks up to 30 seconds (10 polls × 3 sec).
    """
    operation_id = update_timeslot(supply_order_id, slot_from, slot_to)
    status = "STATUS_IN_PROGRESS"
    for _ in range(10):
        time.sleep(3)
        try:
            status = check_update_status(operation_id)
        except Exception as e:
            logger.warning("[slot_hunter] set_timeslot_sync status error: %s", e)
            break
        if status != "STATUS_IN_PROGRESS":
            break
    return status


def _fmt_slot(slot_from: str, slot_to: str) -> str:
    """Format slot times for Telegram: '02.05.2026 10:00 – 12:00'."""
    try:
        dt_f = _parse_dt(slot_from)
        dt_t = _parse_dt(slot_to)
        if dt_f and dt_t:
            return f"{dt_f.strftime('%d.%m.%Y %H:%M')} – {dt_t.strftime('%H:%M')}"
    except Exception:
        pass
    return f"{slot_from} – {slot_to}"


# Singleton — started once from app.py
_scheduler: SlotHunterScheduler | None = None


def get_scheduler() -> SlotHunterScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = SlotHunterScheduler()
    return _scheduler
