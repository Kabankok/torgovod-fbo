"""Unified Ozon Seller API client.

Covers:
  - /v1/analytics/data  — sales metrics by SKU by day
  - /v3/product/list + /v3/product/info/list  — full product catalog with prices
  - /v4/product/info/stocks  — FBO/FBS stock levels by offer_id
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_BASE = "https://api-seller.ozon.ru"
_THROTTLE = 2.0  # seconds between requests (conservative)


def _avg_range(d: dict, min_key: str, max_key: str) -> float:
    """Среднее из пары min/max полей (если оба заданы), иначе максимум из доступных."""
    try:
        mn = float(d.get(min_key) or 0)
        mx = float(d.get(max_key) or 0)
    except (TypeError, ValueError):
        return 0.0
    if mn and mx:
        return (mn + mx) / 2.0
    return mx or mn


def _extract_image_url(item: dict) -> str:
    """Safely extract image URL from product info response.
    primary_image may be a string or a list depending on API version.
    """
    raw = item.get("primary_image") or ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    if raw:
        return str(raw)
    images = item.get("images") or []
    if isinstance(images, list) and images:
        first = images[0]
        return (
            str(first)
            if not isinstance(first, dict)
            else str(first.get("url") or first.get("file_path") or "")
        )
    return ""


# API limit: max 14 metrics per request. We split into 2 batches.
# Batch 1 (14 metrics): visibility + sessions + cart
_METRICS_BATCH1 = [
    "revenue",
    "ordered_units",
    "hits_view",
    "hits_view_search",
    "hits_view_pdp",
    "session_view",
    "session_view_search",
    "session_view_pdp",
    "hits_tocart",
    "hits_tocart_search",
    "hits_tocart_pdp",
    "conv_tocart",
    "conv_tocart_search",
    "conv_tocart_pdp",
]
# Batch 2 (4 metrics): outcomes + position
_METRICS_BATCH2 = ["delivered_units", "returns", "cancellations", "position_category"]
# Fallback for sellers without Premium Plus
_METRICS_BASIC = ["revenue", "ordered_units"]


def _get_seller_credential(company_id: str | None, key_name: str) -> str | None:
    """Try DB first, fall back to .env."""
    if company_id:
        try:
            from shared.auth.credentials import get_credential

            val = get_credential(company_id, "ozon_seller", key_name)
            if val:
                return val
        except Exception:
            pass
    env_map = {
        "client_id": "OZON_CLIENT_ID",
        "api_key": "OZON_API_KEY",
    }
    return os.getenv(env_map.get(key_name, ""))


class SellerClient:
    def __init__(self, company_id: str | None = None) -> None:
        self._cid = _get_seller_credential(company_id, "client_id")
        self._key = _get_seller_credential(company_id, "api_key")
        if not self._cid or not self._key:
            raise ValueError("OZON_CLIENT_ID / OZON_API_KEY not set in .env or DB")
        self._s = requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "Client-Id": self._cid,
            "Api-Key": self._key,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        time.sleep(_THROTTLE)
        r = self._s.post(f"{_BASE}{path}", headers=self._headers(), json=body, timeout=60)
        r.raise_for_status()
        return r.json()

    # ── Warehouses & stock management ─────────────────────────────────────────

    def get_warehouses(self) -> list[dict[str, Any]]:
        """POST /v2/warehouse/list — seller FBS warehouses (active only).

        Returns list of dicts: {warehouse_id, name, type}.
        Filters to status='created' to exclude disabled warehouses.
        """
        data = self._post("/v2/warehouse/list", {})
        warehouses = data.get("warehouses") or []
        return [
            {
                "warehouse_id": int(w.get("warehouse_id", 0)),
                "name": w.get("name") or "",
                "type": w.get("warehouse_type") or "",
            }
            for w in warehouses
            if w.get("status") == "created"
        ]

    def set_stocks(self, stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """POST /v2/products/stocks — update FBS stock levels.

        Each stock dict: {"offer_id": str, "stock": int, "warehouse_id": int}.
        Returns list of result items: {warehouse_id, offer_id, updated, errors}.
        """
        data = self._post("/v2/products/stocks", {"stocks": stocks})
        return data.get("result") or []

    # ── Sales analytics ────────────────────────────────────────────────────────

    def analytics_data_full(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Collect all 18 metrics via 2 API requests, merge by (sku, date).

        Batch 1: 14 visibility/session/cart metrics
        Batch 2: 4 outcome/position metrics
        Falls back to basic if Premium Plus unavailable.
        """
        logger.info("analytics/data batch 1 (14 metrics)...")
        batch1 = self._analytics_paginated(date_from, date_to, _METRICS_BATCH1)

        # Index batch1 by (sku, date) for merging
        index: dict[tuple[str, str], dict[str, Any]] = {(r["sku"], r["date"]): r for r in batch1}

        if index:  # only fetch batch2 if batch1 returned data (not basic fallback)
            logger.info("analytics/data batch 2 (returns/cancellations/position)...")
            batch2 = self._analytics_paginated(date_from, date_to, _METRICS_BATCH2)
            for r in batch2:
                key = (r["sku"], r["date"])
                if key in index:
                    index[key].update({k: v for k, v in r.items() if k not in ("sku", "date")})
                else:
                    index[key] = r

        return list(index.values())

    def analytics_sales_basic(self, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Только revenue + ordered_units по SKU×день — всё, что нужно FBO-расчёту.

        Один батч вместо двух и без зависимости от Premium-метрик — в разы
        быстрее, чем analytics_data_full(), для больших каталогов.
        """
        logger.info("analytics/data basic (revenue + ordered_units)...")
        return self._analytics_paginated(date_from, date_to, list(_METRICS_BASIC))

    def _analytics_paginated(
        self, date_from: date, date_to: date, metrics: list[str]
    ) -> list[dict[str, Any]]:
        """Single paginated call to /v1/analytics/data. Falls back to basic on 400."""
        use_full = True
        active_metrics = list(metrics)
        out: list[dict[str, Any]] = []
        offset = 0
        _INT_COLS = {
            "ordered_units",
            "hits_view",
            "hits_view_search",
            "hits_view_pdp",
            "session_view",
            "session_view_search",
            "session_view_pdp",
            "hits_tocart",
            "hits_tocart_search",
            "hits_tocart_pdp",
            "delivered_units",
            "returns",
            "cancellations",
        }

        while True:
            body: dict[str, Any] = {
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "dimension": ["sku", "day"],
                "metrics": active_metrics,
                "sort": [{"key": "revenue", "order": "DESC"}],
                "limit": 1000,
                "offset": offset,
            }
            restart = False
            r: requests.Response | None = None
            for attempt in range(5):
                time.sleep(_THROTTLE)
                r = self._s.post(
                    f"{_BASE}/v1/analytics/data",
                    headers=self._headers(),
                    json=body,
                    timeout=60,
                )
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                if r.status_code in (400, 403) and use_full and offset == 0:
                    logger.warning("analytics/data: Premium metrics unavailable, basic fallback")
                    active_metrics = list(_METRICS_BASIC)
                    use_full = False
                    out, offset = [], 0
                    restart = True
                    break
                r.raise_for_status()
                break

            if restart:
                continue
            assert r is not None

            # Retries exhausted on 429: the loop falls through here with the error response
            # still in `r`. Parsing it yields no "result", rows == [] and the loop below would
            # `break` — silently returning a truncated report as if it were complete. Fail loudly
            # instead, so the sync step goes red and the user knows the data is short.
            if r.status_code != 200:
                r.raise_for_status()

            page_num = offset // 1000 + 1
            data = r.json()
            rows = (data.get("result") or {}).get("data") or []
            logger.debug("page %d, offset=%d, rows so far: %d", page_num, offset, len(out))
            if not rows:
                break

            for row in rows:
                dims = row.get("dimensions") or []
                mets = row.get("metrics") or []
                sku = str(dims[0].get("id")) if dims else ""
                day = str(dims[1].get("id")) if len(dims) > 1 else ""
                entry: dict[str, Any] = {"sku": sku, "date": day}
                for i, m in enumerate(active_metrics):
                    v = mets[i] if i < len(mets) else None
                    if v is None:
                        entry[m] = None
                    elif m in _INT_COLS:
                        entry[m] = int(float(v))
                    else:
                        entry[m] = float(v)
                out.append(entry)

            if len(rows) < 1000:
                break
            offset += 1000

        return out

    # ── Product catalog ────────────────────────────────────────────────────────

    def list_all_products(self, include_archived: bool = True) -> list[dict[str, Any]]:
        """Paginated /v3/product/list → /v3/product/info/list.

        Returns list of dicts: {sku, product_id, offer_id, name, price, old_price, is_archived}.

        `visibility=ALL` у Ozon API **исключает** архивные товары — для полного
        каталога нужна вторая выгрузка с `visibility=ARCHIVED`. На складе
        архивные могут быть и нужны для инвентаризации.
        """
        # 1. Активный каталог
        active_ids = self._list_product_ids(visibility="ALL")
        archived_ids: list[int] = []
        if include_archived:
            archived_ids = self._list_product_ids(visibility="ARCHIVED")

        archived_set = set(archived_ids)
        all_ids = active_ids + archived_ids
        items = self._get_product_info(all_ids)
        for item in items:
            try:
                pid = int(item.get("product_id") or 0)
            except (TypeError, ValueError):
                pid = 0
            item["is_archived"] = 1 if pid in archived_set else 0
        logger.info(
            "list_all_products: active=%d archived=%d total=%d",
            len(active_ids),
            len(archived_ids),
            len(items),
        )
        return items

    def _list_product_ids(self, visibility: str) -> list[int]:
        """Один проход пагинации /v3/product/list по фильтру visibility."""
        product_ids: list[int] = []
        last_id = ""
        for _page in range(200):
            time.sleep(_THROTTLE)
            r = self._s.post(
                f"{_BASE}/v3/product/list",
                headers=self._headers(),
                json={"filter": {"visibility": visibility}, "last_id": last_id, "limit": 1000},
                timeout=60,
            )
            r.raise_for_status()
            result = r.json().get("result", {})
            page_items = result.get("items", [])
            if not page_items:
                break
            product_ids.extend(int(p["product_id"]) for p in page_items if p.get("product_id"))
            last_id = result.get("last_id", "")
            logger.debug("product/list[%s]: collected %d product_ids", visibility, len(product_ids))
            if not last_id or len(page_items) < 1000:
                break
        return product_ids

    def _get_product_info(self, product_ids: list[int]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(product_ids), 1000):
            chunk = product_ids[i : i + 1000]
            time.sleep(_THROTTLE)
            r = self._s.post(
                f"{_BASE}/v3/product/info/list",
                headers=self._headers(),
                json={"product_id": chunk},
                timeout=60,
            )
            r.raise_for_status()
            for item in r.json().get("items", []):
                sku = item.get("sku")
                if not sku:
                    continue
                # price may be nested under sale_schema or at top level
                price = item.get("price") or item.get("marketing_price")
                old_price = item.get("old_price")
                try:
                    price = float(price) if price else None
                    old_price = float(old_price) if old_price else None
                except (TypeError, ValueError):
                    price = old_price = None
                out.append(
                    {
                        "sku": str(sku),
                        "product_id": str(item.get("id") or ""),
                        "offer_id": str(item.get("offer_id") or ""),
                        "name": str(item.get("name") or ""),
                        "price": price,
                        "old_price": old_price,
                        "image_url": _extract_image_url(item),
                    }
                )
        return out

    # ── Stock levels ────────────────────────────────────────────────────────────

    def get_stock_on_warehouses(
        self, warehouse_type: str = "ALL"
    ) -> dict[str, list[dict[str, Any]]]:
        """POST /v2/analytics/stock_on_warehouses.

        Returns {sku: [{warehouse_name, warehouse_type, present, promised, reserved}]}.
        Call with warehouse_type="FBO" then "FBS" to tag types correctly.
        present  = free_to_sell_amount
        promised = promised_amount  (in transit to buyer)
        reserved = reserved_amount  (reserved for pending orders)
        """
        out: dict[str, list[dict[str, Any]]] = {}
        offset = 0
        wtype = warehouse_type.lower()

        while True:
            time.sleep(_THROTTLE)
            r = self._s.post(
                f"{_BASE}/v2/analytics/stock_on_warehouses",
                headers=self._headers(),
                json={"limit": 1000, "offset": offset, "warehouse_type": warehouse_type},
                timeout=60,
            )
            r.raise_for_status()
            rows = (r.json().get("result") or {}).get("rows") or []
            if not rows:
                break

            for row in rows:
                sku = str(row.get("sku") or "").strip()
                if not sku:
                    continue
                out.setdefault(sku, []).append(
                    {
                        "warehouse_name": str(row.get("warehouse_name") or "unknown"),
                        "warehouse_type": wtype if wtype != "all" else "fbo",
                        "present": int(row.get("free_to_sell_amount") or 0),
                        "promised": int(row.get("promised_amount") or 0),
                        "reserved": int(row.get("reserved_amount") or 0),
                    }
                )

            if len(rows) < 1000:
                break
            offset += 1000

        return out

    def get_stocks(self, offer_ids: list[str]) -> dict[str, tuple[int, int]]:
        """POST /v4/product/info/stocks. Returns {offer_id: (fbo_stock, fbs_stock)} aggregated."""
        detailed = self.get_stocks_detailed(offer_ids)
        out: dict[str, tuple[int, int]] = {}
        for oid, entries in detailed.items():
            fbo = sum(e["present"] for e in entries if e["warehouse_type"] == "fbo")
            fbs = sum(e["present"] for e in entries if e["warehouse_type"] in ("fbs", "rfbs"))
            out[oid] = (fbo, fbs)
        return out

    def get_stocks_detailed(self, offer_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """POST /v4/product/info/stocks. Returns per-warehouse breakdown.

        Returns {offer_id: [{warehouse_name, warehouse_type, present}, ...]}
        Allows separating FBO from each FBS warehouse (e.g. "Студенческий" vs "Офис").
        """
        out: dict[str, list[dict[str, Any]]] = {}
        # Chunk of 1000 (the API's own page limit), not 200: at 200 a catalogue of several
        # thousand SKUs meant five times more round-trips, each one throttled and each one
        # another chance to hit Ozon's rate limit. Cursor pagination inside a chunk still works.
        for i in range(0, len(offer_ids), 1000):
            chunk = offer_ids[i : i + 1000]
            cursor = ""
            for _page in range(50):  # не более 50 страниц на чанк (защита от бесконечного цикла)
                body: dict[str, Any] = {
                    "filter": {"offer_id": chunk, "visibility": "ALL"},
                    "limit": 1000,
                }
                if cursor:
                    body["cursor"] = cursor
                time.sleep(_THROTTLE)
                r = self._s.post(
                    f"{_BASE}/v4/product/info/stocks",
                    headers=self._headers(),
                    json=body,
                    timeout=60,
                )
                r.raise_for_status()
                data = r.json()
                items = data.get("items") or []
                for item in items:
                    oid = str(item.get("offer_id") or "").strip()
                    if not oid:
                        continue
                    entries: list[dict[str, Any]] = []
                    for s in item.get("stocks") or []:
                        wtype = str(s.get("type") or "").lower()
                        wname = str(s.get("warehouse_name") or s.get("type") or "unknown")
                        try:
                            qty = int(s.get("present") or 0)
                        except (TypeError, ValueError):
                            qty = 0
                        entries.append(
                            {
                                "warehouse_name": wname,
                                "warehouse_type": wtype,
                                "present": qty,
                            }
                        )
                    out[oid] = entries
                cursor = str(data.get("cursor") or "").strip()
                # Выходим если: нет курсора ИЛИ вернули меньше лимита (последняя страница)
                if not cursor or len(items) < 1000:
                    break
        return out

    # ── FBS warehouse management ─────────────────────────────────────────────

    def list_fbs_warehouses(self) -> list[dict[str, Any]]:
        """GET /v1/warehouse/list. Returns seller's registered FBS warehouses."""
        time.sleep(_THROTTLE)
        r = self._s.get(
            f"{_BASE}/v1/warehouse/list",
            headers=self._headers(),
            timeout=30,
        )
        r.raise_for_status()
        result = r.json().get("result") or []
        return [
            {"warehouse_id": int(w["warehouse_id"]), "name": str(w.get("name") or "")}
            for w in result
            if w.get("warehouse_id")
        ]

    def update_fbs_stock(self, offer_id: str, warehouse_id: int, quantity: int) -> dict[str, Any]:
        """POST /v2/products/stocks. Set FBS stock for one SKU at one warehouse.
        Returns {"updated": True/False, "errors": [...]}
        """
        time.sleep(_THROTTLE)
        r = self._s.post(
            f"{_BASE}/v2/products/stocks",
            headers=self._headers(),
            json={
                "stocks": [{"offer_id": offer_id, "stock": quantity, "warehouse_id": warehouse_id}]
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        result = (data.get("result") or [{}])[0]
        errors = result.get("errors") or []
        return {"updated": result.get("updated", False), "errors": errors}

    # ── FBS postings (orders) ─────────────────────────────────────────────────

    @staticmethod
    def _fbs_default_with() -> dict[str, bool]:
        """Все вспомогательные секции, которые нам нужны в ответах /fbs/list*.

        tariffication приходит дефолтом (поле верхнего уровня), отдельный флаг
        не нужен и Ozon его не принимает.
        """
        return {
            "analytics_data": True,
            "financial_data": True,
            "products": True,
            "barcodes": True,
            "translit": True,
        }

    def list_fbs_unfulfilled(
        self,
        filter: dict[str, Any] | None = None,
        limit: int = 1000,
        offset: int = 0,
        with_: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """POST /v3/posting/fbs/unfulfilled/list — что нужно собирать сейчас.

        Возвращает «сырой» dict ответа: {"result": {"postings": [...], "count": N}}.
        Постраничную обработку делает вызывающая сторона (sync.py), здесь — один заход.
        """
        body: dict[str, Any] = {
            "dir": "ASC",
            "filter": filter or {},
            "limit": limit,
            "offset": offset,
            "with": with_ if with_ is not None else self._fbs_default_with(),
        }
        return self._post("/v3/posting/fbs/unfulfilled/list", body)

    def list_fbs(
        self,
        filter: dict[str, Any] | None = None,
        limit: int = 1000,
        offset: int = 0,
        dir: str = "ASC",
        with_: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """POST /v3/posting/fbs/list — список отправлений за период (фильтр по статусу/датам)."""
        body: dict[str, Any] = {
            "dir": dir,
            "filter": filter or {},
            "limit": limit,
            "offset": offset,
            "with": with_ if with_ is not None else self._fbs_default_with(),
        }
        return self._post("/v3/posting/fbs/list", body)

    def get_fbs_posting(
        self,
        posting_number: str,
        with_: dict[str, bool] | None = None,
    ) -> dict[str, Any]:
        """POST /v3/posting/fbs/get — детали одного отправления (с полным tariffication)."""
        body: dict[str, Any] = {
            "posting_number": posting_number,
            "with": with_ if with_ is not None else self._fbs_default_with(),
        }
        data = self._post("/v3/posting/fbs/get", body)
        return data.get("result") or {}

    def fetch_package_label(self, posting_numbers: list[str]) -> bytes:
        """POST /v2/posting/fbs/package-label — QR-стикеры 50×75 мм (до 20 отправлений).

        Возвращает сырые байты PDF. Один стикер — одна страница.
        """
        time.sleep(_THROTTLE)
        r = self._s.post(
            f"{_BASE}/v2/posting/fbs/package-label",
            headers=self._headers(),
            json={"posting_number": posting_numbers},
            timeout=60,
        )
        r.raise_for_status()
        return r.content

    # ── FBO-specific methods ──────────────────────────────────────────────────

    def get_turnover_stocks(self) -> list[dict[str, Any]]:
        """POST /v1/analytics/turnover/stocks — Ozon-calculated turnover data.

        Returns list of dicts per SKU:
          sku, current_stock, ads (avg_daily_sold), idc (days of coverage),
          idc_grade (RED/YELLOW/GREEN), turnover (days), turnover_grade
        """
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            time.sleep(_THROTTLE)
            r = self._s.post(
                f"{_BASE}/v1/analytics/turnover/stocks",
                headers=self._headers(),
                json={"limit": 1000, "offset": offset},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            # Swagger v2.1: {"items": [...]}; old API: {"result": {"rows": [...]}}
            rows = data.get("items") or (data.get("result") or {}).get("rows") or []
            if not rows:
                break
            for row in rows:
                out.append(
                    {
                        "sku": str(row.get("sku") or ""),
                        "current_stock": int(row.get("current_stock") or 0),
                        "ads_daily": float(row.get("ads") or 0.0),
                        "idc_days": float(row.get("idc") or 0.0),
                        "idc_grade": str(row.get("idc_grade") or ""),
                        "turnover_days": float(row.get("turnover") or 0.0),
                        "turnover_grade": str(row.get("turnover_grade") or ""),
                    }
                )
            if len(rows) < 1000:
                break
            offset += 1000
        return out

    def get_cluster_list(self) -> list[dict[str, Any]]:
        """POST /v1/cluster/list — Ozon business clusters (regional groupings).

        Returns list of dicts: {cluster_id, cluster_name, warehouses: [...]}
        Each warehouse: {warehouse_id, warehouse_name}

        Swagger v2.1 (2026-04-22):
          - cluster_type is REQUIRED; omitting it causes 400
          - response key: "clusters" (old API used "result")
          - cluster fields: "id"/"name" (old API used "cluster_id"/"cluster_name")
          - warehouses nested under logistic_clusters[].warehouses[].name
        Code is backwards-compatible: falls back to old field names if new ones absent.
        """
        time.sleep(_THROTTLE)
        r = self._s.post(
            f"{_BASE}/v1/cluster/list",
            headers=self._headers(),
            json={"cluster_type": "CLUSTER_TYPE_OZON"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        # New API returns {"clusters": [...]}, old API returned {"result": [...]}
        clusters = data.get("clusters") or data.get("result") or []
        out: list[dict[str, Any]] = []
        for c in clusters:
            # New API: "id"/"name"; old API: "cluster_id"/"cluster_name"
            cluster_id = str(c.get("id") or c.get("cluster_id") or "")
            cluster_name = str(c.get("name") or c.get("cluster_name") or "")
            warehouses: list[dict[str, str]] = []
            # New API: warehouses nested under logistic_clusters[n].warehouses
            for lc in c.get("logistic_clusters") or []:
                for w in lc.get("warehouses") or []:
                    wname = str(w.get("name") or w.get("warehouse_name") or "")
                    wid = str(w.get("warehouse_id") or "")
                    if wname:
                        warehouses.append({"warehouse_id": wid, "warehouse_name": wname})
            # Old API: warehouses directly on cluster object
            for w in c.get("warehouses") or []:
                wname = str(w.get("warehouse_name") or w.get("name") or "")
                wid = str(w.get("warehouse_id") or "")
                if wname:
                    warehouses.append({"warehouse_id": wid, "warehouse_name": wname})
            out.append(
                {
                    "cluster_id": cluster_id,
                    "cluster_name": cluster_name,
                    "warehouses": warehouses,
                }
            )
        return out

    def get_fbo_postings(
        self, date_from: str, date_to: str, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """POST /v2/posting/fbo/list — FBO order history for date range.

        date_from, date_to: ISO strings like '2025-03-01T00:00:00Z'
        Returns list of dicts per posting: {posting_number, sku, offer_id,
          quantity, price, created_at, cluster_from, cluster_to}
        """
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            time.sleep(_THROTTLE)
            r = self._s.post(
                f"{_BASE}/v2/posting/fbo/list",
                headers=self._headers(),
                json={
                    "dir": "asc",
                    "filter": {
                        "since": date_from,
                        "to": date_to,
                        "status": "",
                    },
                    "limit": limit,
                    "offset": offset,
                    "with": {"analytics_data": True, "financial_data": True},
                },
                timeout=60,
            )
            r.raise_for_status()
            postings = r.json().get("result") or []
            if not postings:
                break
            for p in postings:
                # cluster_from/cluster_to live in financial_data (not analytics_data)
                financial = p.get("financial_data") or {}
                analytics = p.get("analytics_data") or {}
                # Try financial_data first, fall back to analytics_data
                cluster_from = str(financial.get("cluster_from") or "") or str(
                    analytics.get("cluster_from") or ""
                )
                cluster_to = str(financial.get("cluster_to") or "") or str(
                    analytics.get("cluster_to") or ""
                )
                for prod in p.get("products") or []:
                    out.append(
                        {
                            "posting_number": str(p.get("posting_number") or ""),
                            "created_at": str(p.get("in_process_at") or p.get("created_at") or ""),
                            "sku": str(prod.get("sku") or ""),
                            "offer_id": str(prod.get("offer_id") or ""),
                            "name": str(prod.get("name") or ""),
                            "quantity": int(prod.get("quantity") or 0),
                            "price": float(prod.get("price") or 0.0),
                            "cluster_from": cluster_from,
                            "cluster_to": cluster_to,
                            "region": str(analytics.get("region") or ""),
                        }
                    )
            if len(postings) < limit:
                break
            offset += limit
        return out

    def get_product_barcodes(self, offer_ids: list[str]) -> dict[str, list[str]]:
        """POST /v1/product/info/list — extract barcodes per offer_id.

        Returns {offer_id: [barcode1, barcode2, ...]}
        """
        out: dict[str, list[str]] = {}
        for i in range(0, len(offer_ids), 100):
            chunk = offer_ids[i : i + 100]
            time.sleep(_THROTTLE)
            r = self._s.post(
                f"{_BASE}/v2/product/info/list",
                headers=self._headers(),
                json={"offer_id": chunk},
                timeout=60,
            )
            r.raise_for_status()
            for item in r.json().get("items") or []:
                oid = str(item.get("offer_id") or "")
                barcodes = [str(b) for b in (item.get("barcodes") or []) if b]
                if oid and barcodes:
                    out[oid] = barcodes
        return out

    # ── Search position & queries ────────────────────────────────────────────

    def product_queries(
        self, date_from: date, date_to: date, skus: list[str]
    ) -> list[dict[str, Any]]:
        """POST /v1/analytics/product-queries
        Returns avg search position per SKU for the period.
        position field is NULL without Premium subscription.
        """
        if not skus:
            return []
        results = []
        page = 0
        page_size = 1000
        while True:
            body = {
                "date_from": f"{date_from.isoformat()}T00:00:00Z",
                "date_to": f"{date_to.isoformat()}T23:59:59Z",
                "skus": [str(s) for s in skus],
                "page": page,
                "page_size": page_size,
                "sort_by": "BY_SEARCHES",
                "sort_dir": "DESCENDING",
            }
            data = self._post("/v1/analytics/product-queries", body)
            items = data.get("items") or []
            results.extend(items)
            page_count = data.get("page_count") or 0
            if not items or page >= page_count - 1:
                break
            page += 1
        return results

    # ── SEO: product content & attributes ───────────────────────────────────────

    def get_product_attributes(self, offer_ids: list[str]) -> list[dict[str, Any]]:
        """POST /v4/product/info/attributes — all product attributes incl. keywords field.

        Returns list of {offer_id, description_category_id, attributes: [{id, values}]}.
        Paginated with last_id cursor.
        """
        out: list[dict[str, Any]] = []
        last_id = ""
        while True:
            body: dict[str, Any] = {
                "filter": {"offer_id": offer_ids, "visibility": "ALL"},
                "limit": 100,
                "sort_dir": "ASC",
            }
            if last_id:
                body["last_id"] = last_id
            data = self._post("/v4/product/info/attributes", body)
            result = data.get("result") or []
            out.extend(result)
            last_id = str(data.get("last_id") or "").strip()
            if not result or not last_id:
                break
        return out

    def get_product_description(self, offer_id: str) -> str:
        """POST /v1/product/info/description — product text description."""
        data = self._post("/v1/product/info/description", {"offer_id": offer_id})
        return str(data.get("description") or "")

    def get_content_rating(self, skus: list[str]) -> list[dict[str, Any]]:
        """POST /v1/product/rating-by-sku — content rating (0–100) per SKU.

        Returns list of {sku, rating, groups, improve_attributes} where
        groups = [{name, weight, rating, conditions, improve_attributes}].
        Improve_attributes = attribute_ids to fill for rating improvement.
        """
        if not skus:
            return []
        out: list[dict[str, Any]] = []
        for i in range(0, len(skus), 100):
            chunk = [str(s) for s in skus[i : i + 100]]
            data = self._post("/v1/product/rating-by-sku", {"skus": chunk})
            items = data.get("result") or []
            out.extend(items)
        return out

    def update_product_attributes(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """POST /v1/product/attributes/update — update product attributes (incl. keywords).

        Each item: {"offer_id": str, "attributes": [{"id": int, "complex_id": 0,
                    "values": [{"value": str}]}]}
        Returns list of {offer_id, updated, errors}. Async on Ozon side — changes
        go through moderation (up to several days).
        """
        if not items:
            return []
        data = self._post("/v1/product/attributes/update", {"items": items})
        return data.get("result") or []

    # ── Unit economics: prices + commissions + price index ───────────────────

    def get_product_prices_with_commissions(self) -> list[dict[str, Any]]:
        """POST /v5/product/info/prices — комиссии, логистика, индекс цен по всем SKU продавца.

        Пагинация через cursor: ответ содержит {"items": [...], "cursor": "...", "total": N}.
        Следующая страница: body["cursor"] = cursor из предыдущего ответа.

        Returns list of dicts per offer_id:
          offer_id, commission_pct_fbo, commission_pct_fbs,
          delivery_fbo, delivery_fbs, return_fbo, return_fbs,
          acquiring_pct, price_index_color, price_index_value, competitor_min_price
        """
        out: list[dict[str, Any]] = []
        cursor = ""
        for _page in range(500):
            body: dict[str, Any] = {"filter": {"visibility": "ALL"}, "limit": 1000}
            if cursor:
                body["cursor"] = cursor
            time.sleep(_THROTTLE)
            r = self._s.post(
                f"{_BASE}/v5/product/info/prices",
                headers=self._headers(),
                json=body,
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("items") or []
            for item in items:
                comm = item.get("commissions") or {}
                acq_raw = item.get("acquiring")
                try:
                    acquiring = float(acq_raw) if acq_raw is not None else 0.0
                except (TypeError, ValueError):
                    acquiring = 0.0
                price_obj = item.get("price") or {}
                try:
                    cur_price = float(price_obj.get("price") or 0) or None
                except (TypeError, ValueError):
                    cur_price = None
                try:
                    old_p = float(price_obj.get("old_price") or 0) or None
                except (TypeError, ValueError):
                    old_p = None
                pidx = item.get("price_indexes") or {}
                ozon_idx = pidx.get("ozon_index_data") or {}
                ext_idx = pidx.get("external_index_data") or {}
                try:
                    # external_index_data содержит сравнение с внешними конкурентами
                    idx_val = float(
                        ext_idx.get("price_index_value") or ozon_idx.get("price_index_value") or 0
                    )
                except (TypeError, ValueError):
                    idx_val = 0.0
                try:
                    comp_price = float(ext_idx.get("min_price") or 0) or None
                except (TypeError, ValueError):
                    comp_price = None

                # Магистральная логистика — API возвращает диапазон min/max,
                # берём среднее как реалистичную оценку для юнит-экономики.
                fbo_trans = _avg_range(
                    comm, "fbo_direct_flow_trans_min_amount", "fbo_direct_flow_trans_max_amount"
                )
                fbs_trans = _avg_range(
                    comm, "fbs_direct_flow_trans_min_amount", "fbs_direct_flow_trans_max_amount"
                )
                fbs_first_mile = _avg_range(
                    comm, "fbs_first_mile_min_amount", "fbs_first_mile_max_amount"
                )

                out.append(
                    {
                        "offer_id": str(item.get("offer_id") or ""),
                        "price": cur_price,
                        "old_price": old_p,
                        "commission_pct_fbo": float(comm.get("sales_percent_fbo") or 0),
                        "commission_pct_fbs": float(comm.get("sales_percent_fbs") or 0),
                        "delivery_fbo": float(comm.get("fbo_deliv_to_customer_amount") or 0),
                        "delivery_fbs": float(comm.get("fbs_deliv_to_customer_amount") or 0),
                        "return_fbo": float(comm.get("fbo_return_flow_amount") or 0),
                        "return_fbs": float(comm.get("fbs_return_flow_amount") or 0),
                        "acquiring_pct": acquiring,
                        "fbo_direct_flow_amount": fbo_trans,
                        # FBS: магистраль + первая миля (Ozon забирает у продавца)
                        "fbs_direct_flow_amount": fbs_trans + fbs_first_mile,
                        "price_index_color": str(pidx.get("color_index") or "WITHOUT_INDEX"),
                        "price_index_value": idx_val,
                        "competitor_min_price": comp_price,
                    }
                )
            cursor = str(data.get("cursor") or "").strip()
            if not items or not cursor:
                break
        return out

    def update_prices(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /v1/product/import/prices — обновить цены товаров.

        Each item: {offer_id, price, min_price?, old_price?}
        """
        payload = []
        for it in items:
            entry: dict[str, Any] = {
                "offer_id": str(it["offer_id"]),
                "price": str(it["price"]),
            }
            if it.get("min_price") is not None:
                entry["min_price"] = str(it["min_price"])
            if it.get("old_price") is not None:
                entry["old_price"] = str(it["old_price"])
            payload.append(entry)
        return self._post("/v1/product/import/prices", {"prices": payload})

    def get_category_attributes(
        self, description_category_id: int, type_id: int = 0
    ) -> list[dict[str, Any]]:
        """POST /v1/description-category/attribute — list attributes for a category.

        Use to find the attribute_id of the 'Ключевые слова' field.
        Returns list of {id, name, is_required, is_collection, type}.
        """
        data = self._post(
            "/v1/description-category/attribute",
            {
                "description_category_id": description_category_id,
                "type_id": type_id,
                "language": "DEFAULT",
            },
        )
        return data.get("result") or []

    def get_category_tree(self, language: str = "DEFAULT") -> list[dict[str, Any]]:
        """POST /v1/description-category/tree — полное дерево категорий Ozon.

        Returns nested list:
          [{description_category_id, category_name, disabled,
            children: [{description_category_id, category_name, type_id, type_name, ...}]}]
        Узлы с непустым type_id обычно листья (конечные категории), но не всегда —
        иерархия делится «промежуточная категория → тип товара». Раскрытие в плоский
        вид и определение is_leaf делает category_cache.refresh_ozon_tree().
        """
        data = self._post(
            "/v1/description-category/tree",
            {"language": language},
        )
        return data.get("result") or []

    def get_import_info(self, task_id: int) -> dict[str, Any]:
        """POST /v1/product/import/info — подробный статус задачи импорта.

        Альтернатива get_import_task_status, возвращает развёрнутый ответ Ozon
        со списком items {offer_id, product_id, status, errors[]}. Используется
        фоновым воркером модерации в Фазе 4.
        """
        return self._post("/v1/product/import/info", {"task_id": task_id})

    def get_attribute_dictionary(
        self,
        attribute_id: int,
        category_id: int,
        type_id: int,
        last_value_id: int = 0,
        limit: int = 5000,
    ) -> dict[str, Any]:
        """POST /v1/description-category/attribute/values — справочные значения атрибута.

        Например для атрибута «Бренд» вернёт список ID + название.
        Пагинация — через last_value_id (0 на первой странице).
        Returns {"result": [{id, value, info, picture}], "has_next": bool}.

        Старый путь /v2/category/attribute/values отдаёт 404 — Ozon перенёс ручку
        в семейство /v1/description-category/* (проверено 2026-06-04).
        """
        return self._post(
            "/v1/description-category/attribute/values",
            {
                "attribute_id": attribute_id,
                "description_category_id": category_id,
                "type_id": type_id,
                "language": "DEFAULT",
                "last_value_id": last_value_id,
                "limit": limit,
            },
        )

    # ── Акции (promotions) ────────────────────────────────────────────────────

    def get_actions(self) -> list[dict[str, Any]]:
        """GET /v1/actions — все акции платформы Ozon."""
        time.sleep(_THROTTLE)
        r = self._s.get(
            f"{_BASE}/v1/actions",
            headers=self._headers(),
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("result") or []

    def get_action_candidates(self, action_id: int) -> list[dict[str, Any]]:
        """POST /v1/actions/candidates — товары-кандидаты акции (пагинация)."""
        out: list[dict[str, Any]] = []
        offset = 0
        limit = 100
        while True:
            time.sleep(_THROTTLE)
            r = self._s.post(
                f"{_BASE}/v1/actions/candidates",
                headers=self._headers(),
                json={"action_id": action_id, "offset": offset, "limit": limit},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json().get("result") or {}
            items = data.get("products") or []
            out.extend(items)
            if len(items) < limit:
                break
            offset += limit
        return out

    def get_action_products(self, action_id: int) -> list[dict[str, Any]]:
        """POST /v1/actions/products — товары уже участвующие в акции (пагинация)."""
        out: list[dict[str, Any]] = []
        offset = 0
        limit = 100
        while True:
            time.sleep(_THROTTLE)
            r = self._s.post(
                f"{_BASE}/v1/actions/products",
                headers=self._headers(),
                json={"action_id": action_id, "offset": offset, "limit": limit},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json().get("result") or {}
            items = data.get("products") or []
            out.extend(items)
            if len(items) < limit:
                break
            offset += limit
        return out

    def add_to_action(self, action_id: int, products: list[dict[str, Any]]) -> dict[str, Any]:
        """POST /v1/actions/products/activate — добавить товары в акцию (до 1000 за раз).

        products: [{"product_id": int, "action_price": float}]
        """
        return self._post(
            "/v1/actions/products/activate",
            {
                "action_id": action_id,
                "products": products,
            },
        )

    def remove_from_action(self, action_id: int, product_ids: list[int]) -> dict[str, Any]:
        """POST /v1/actions/products/deactivate — убрать товары из акции."""
        return self._post(
            "/v1/actions/products/deactivate",
            {
                "action_id": action_id,
                "product_ids": product_ids,
            },
        )

    # ── Product import ────────────────────────────────────────────────────────

    def import_product_cards(self, items: list[dict]) -> dict:
        """POST /v3/product/import — загрузить или обновить карточки товаров.

        Each item: {name, offer_id, description, attributes, images, primary_image}.
        Returns {"result": {"task_id": int}}.
        """
        return self._post("/v3/product/import", {"items": items})

    def get_import_task_status(self, task_id: int) -> dict:
        """POST /v1/product/import/task/info — статус задачи импорта.

        Returns {"result": {"items": [{offer_id, status, errors}], "total": N}}.
        Status values: pending | imported | failed.
        """
        return self._post("/v1/product/import/task/info", {"task_id": task_id})

    def get_prices_details(self, skus: list[str]) -> list[dict[str, Any]]:
        """POST /v1/product/prices/details — цены продавца и покупателя (с учётом СПП).

        Метод требует подписку Premium Pro. Доступные поля по каждому SKU:
          - sku, offer_id
          - price.amount — цена с акциями продавца (без СПП)
          - customer_price.amount — цена для покупателя (с учётом скидки Ozon)
          - discount_percent — процент скидки за счёт Ozon (СПП %)

        Принимает SKU (не offer_id — это требование API). Бьёт батчами по 1000.
        Возвращает список словарей:
          {sku, offer_id, seller_price, customer_price, discount_percent}
        Цены — float в рублях. Конвертацию в копейки делает sync-функция.
        """
        if not skus:
            return []
        out: list[dict[str, Any]] = []
        for i in range(0, len(skus), 1000):
            chunk = [str(s) for s in skus[i : i + 1000] if s]
            if not chunk:
                continue
            # Ретрай на 429 с экспоненциальной паузой — у Ozon частый rate-limit
            # на этом эндпоинте даже после _THROTTLE=2с.
            data: dict[str, Any] = {}
            for attempt in range(5):
                time.sleep(_THROTTLE)
                r = self._s.post(
                    f"{_BASE}/v1/product/prices/details",
                    headers=self._headers(),
                    json={"skus": chunk},
                    timeout=60,
                )
                if r.status_code == 429:
                    time.sleep(5 * (attempt + 1))
                    continue
                if r.status_code == 403:
                    # Ozon обычно возвращает {"code":7,"message":"no access to this method"}.
                    # Причин может быть несколько: нет подписки Premium Pro, не включён
                    # API-доступ на конкретный модуль в ЛК, права ключа.
                    try:
                        body = r.json()
                        ozon_msg = f"{body.get('code')}: {body.get('message')}"
                    except Exception:
                        ozon_msg = r.text[:200]
                    raise RuntimeError(
                        "Нет доступа к методу СПП. Ответ Ozon: "
                        f"{ozon_msg}. Проверьте: 1) подписка Premium Pro активна; "
                        "2) в ЛК Ozon включён API-доступ к ценам/СПП; "
                        "3) права API-ключа на /v1/product/prices/details."
                    )
                r.raise_for_status()
                data = r.json()
                break
            for item in data.get("prices") or []:
                sku = str(item.get("sku") or "")
                if not sku:
                    continue
                try:
                    seller_price = float((item.get("price") or {}).get("amount") or 0)
                except (TypeError, ValueError):
                    seller_price = 0.0
                try:
                    customer_price = float((item.get("customer_price") or {}).get("amount") or 0)
                except (TypeError, ValueError):
                    customer_price = 0.0
                try:
                    discount_pct = float(item.get("discount_percent") or 0)
                except (TypeError, ValueError):
                    discount_pct = 0.0
                out.append(
                    {
                        "sku": sku,
                        "offer_id": str(item.get("offer_id") or ""),
                        "seller_price": seller_price,
                        "customer_price": customer_price,
                        "discount_percent": discount_pct,
                    }
                )
        return out

    def product_queries_details(
        self, date_from: date, date_to: date, skus: list[str], limit_by_sku: int = 15
    ) -> list[dict[str, Any]]:
        """POST /v1/analytics/product-queries/details
        Returns top search queries per SKU for the period.
        Ozon limits page_size to (0, 100] for this endpoint.
        """
        if not skus:
            return []
        results = []
        page = 0
        page_size = 100
        while True:
            body = {
                "date_from": f"{date_from.isoformat()}T00:00:00Z",
                "date_to": f"{date_to.isoformat()}T23:59:59Z",
                "skus": [str(s) for s in skus],
                "limit_by_sku": limit_by_sku,
                "page": page,
                "page_size": page_size,
                "sort_by": "BY_SEARCHES",
                "sort_dir": "DESCENDING",
            }
            data = self._post("/v1/analytics/product-queries/details", body)
            queries = data.get("queries") or []
            results.extend(queries)
            page_count = data.get("page_count") or 0
            if not queries or page >= page_count - 1:
                break
            page += 1
        return results

    # ── Placement reports (платное хранение) ──────────────────────────────────

    def create_placement_by_products_report(self, date_from: str, date_to: str) -> str:
        """POST /v1/report/placement/by-products/create — асинхронный отчёт «день × склад × SKU».

        Возвращает code для последующего поллинга через get_report_info(code).
        Период до 31 дня в одном запросе. Лимит 5 отчётов в день у Ozon.

        Аргументы:
            date_from, date_to — даты в формате 'YYYY-MM-DD'.
        """
        data = self._post(
            "/v1/report/placement/by-products/create",
            {"date_from": date_from, "date_to": date_to},
        )
        # API возвращает либо {"code": "..."} либо {"result": {"code": "..."}}
        code = (data.get("result") or data).get("code")
        if not code:
            raise RuntimeError(f"Ozon API не вернул code: {data}")
        return str(code)

    def create_placement_by_supplies_report(self, date_from: str, date_to: str) -> str:
        """POST /v1/report/placement/by-supplies/create — отчёт «поставка × склад × SKU + календарь».

        Аналогично by-products. Возвращает code.
        """
        data = self._post(
            "/v1/report/placement/by-supplies/create",
            {"date_from": date_from, "date_to": date_to},
        )
        code = (data.get("result") or data).get("code")
        if not code:
            raise RuntimeError(f"Ozon API не вернул code: {data}")
        return str(code)

    def get_report_info(self, code: str) -> dict[str, Any]:
        """POST /v1/report/info — статус отчёта по code.

        Возвращает result dict с полями: status (processing|success|failed),
        file (URL для скачивания), error (текст ошибки при failed).
        """
        data = self._post("/v1/report/info", {"code": code})
        return data.get("result") or {}

    def download_report_file(self, file_url: str) -> bytes:
        """GET готовый файл отчёта по URL из get_report_info().

        Файл xlsx. Авторизация передаётся через стандартные Ozon-заголовки.
        Таймаут 120 сек — файлы могут быть большими (до десятков МБ).
        """
        time.sleep(_THROTTLE)
        r = self._s.get(file_url, headers=self._headers(), timeout=120)
        r.raise_for_status()
        return r.content


# ── Finance API (для модуля финансовых отчётов, plan 2026-05-30-финотчёты) ──
#
# Три модульных функции (не методы класса) с собственными retry на 429/5xx,
# логированием каждого вызова в data/app_logs.db и обязательным print(r.text)
# при 4xx (см. memory/feedback_403_check_body_first.md).

import sqlite3 as _sqlite3
from pathlib import Path as _Path

_FIN_THROTTLE = 1.0
_FIN_RETRY_429 = [30, 60, 120]
_FIN_RETRY_5XX = [3, 8, 20, 45]
_FIN_TIMEOUT = 120
_APP_LOGS_PATH = _Path(__file__).resolve().parents[2] / "data" / "app_logs.db"


def _log_api_call(
    company_id: str | None,
    method: str,
    period: str,
    count: int,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Лог одного API-вызова в data/app_logs.db (idempotent init)."""
    try:
        _APP_LOGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _sqlite3.connect(str(_APP_LOGS_PATH)) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS api_calls (
                       id          INTEGER PRIMARY KEY AUTOINCREMENT,
                       ts          DATETIME DEFAULT CURRENT_TIMESTAMP,
                       company_id  TEXT,
                       method      TEXT NOT NULL,
                       period      TEXT,
                       count       INTEGER,
                       duration_ms INTEGER,
                       error       TEXT
                   )"""
            )
            conn.execute(
                "INSERT INTO api_calls (company_id, method, period, count, duration_ms, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (company_id, method, period, int(count or 0), duration_ms, error),
            )
            conn.commit()
    except Exception as exc:
        # Лог не должен ронять основную работу — выводим в logger и идём дальше.
        logger.warning("[finance] _log_api_call failed: %s", exc)


def _fin_post(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> dict[str, Any]:
    """POST с retry: 429 → длинные паузы, 5xx/таймаут → короткие.

    При любом 4xx (кроме 429) — печатает r.text перед поднятием исключения.
    """
    last_exc: Exception | None = None
    for attempt_429, delay_429 in enumerate([0] + _FIN_RETRY_429):
        if delay_429:
            logger.warning("[finance] 429 — ждём %ds (попытка %d)", delay_429, attempt_429 + 1)
            time.sleep(delay_429)
        r: requests.Response | None = None
        for attempt_5xx, delay_5xx in enumerate([0] + _FIN_RETRY_5XX):
            if delay_5xx:
                logger.warning(
                    "[finance] transient — ждём %ds (попытка %d)", delay_5xx, attempt_5xx + 1
                )
                time.sleep(delay_5xx)
            try:
                time.sleep(_FIN_THROTTLE)
                r = requests.post(url, headers=headers, json=body, timeout=_FIN_TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                r = None
                continue
            if r.status_code < 500:
                break
            last_exc = requests.HTTPError(f"{r.status_code} {r.reason}", response=r)
        if r is None:
            if last_exc:
                raise last_exc
            raise RuntimeError(f"[finance] POST {url}: все попытки исчерпаны")
        if r.status_code == 429:
            continue
        if 400 <= r.status_code < 500:
            # Печатаем тело ДО r.raise_for_status — иначе текст ответа теряется.
            print(f"[finance] {r.status_code} {url} body: {r.text[:1000]}")
            r.raise_for_status()
        r.raise_for_status()
        return r.json()
    # Все 429-ретраи исчерпаны
    if last_exc:
        raise last_exc
    raise RuntimeError(f"[finance] POST {url}: лимит 429 не снят за {len(_FIN_RETRY_429)} попыток")


def _fin_headers(client_id: str, api_key: str) -> dict[str, str]:
    return {
        "Client-Id": str(client_id),
        "Api-Key": str(api_key),
        "Content-Type": "application/json",
    }


def _to_ozon_iso(d: str) -> str:
    """'YYYY-MM-DD' → ISO 8601 с временем и Z (как ждёт Ozon)."""
    if "T" in d:
        return d
    # date_from — начало дня, date_to передаётся как 23:59:59 (см. вызывающую сторону)
    return f"{d}T00:00:00.000Z"


def fetch_finance_transactions(
    client_id: str,
    api_key: str,
    date_from: str,
    date_to: str,
    page_size: int = 1000,
    company_id: str | None = None,
) -> list[dict[str, Any]]:
    """POST /v3/finance/transaction/list — все операции за период.

    Ozon режет запросы > 31 дня — валидируем на входе.
    Постранично через page/page_count. Возвращает плоский список operations[].
    """
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    if (d_to - d_from).days > 31:
        raise ValueError(
            f"finance/transaction/list: период {date_from}..{date_to} > 31 дня — "
            "Ozon вернёт ошибку. Бить помесячно."
        )

    url = f"{_BASE}/v3/finance/transaction/list"
    headers = _fin_headers(client_id, api_key)
    out: list[dict[str, Any]] = []
    page = 1
    started = time.monotonic()
    error_msg: str | None = None
    try:
        while True:
            body = {
                "filter": {
                    "date": {
                        "from": _to_ozon_iso(date_from),
                        "to": f"{date_to}T23:59:59.999Z" if "T" not in date_to else date_to,
                    },
                    "operation_type": [],
                    "posting_number": "",
                    "transaction_type": "all",
                },
                "page": page,
                "page_size": int(page_size),
            }
            data = _fin_post(url, headers, body)
            result = data.get("result") or {}
            operations = result.get("operations") or []
            out.extend(operations)
            page_count = int(result.get("page_count") or 0)
            logger.info(
                "[finance] transaction/list page %d/%d, operations on page %d, total %d",
                page,
                page_count or page,
                len(operations),
                len(out),
            )
            if not operations or page >= page_count:
                break
            page += 1
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        _log_api_call(
            company_id,
            "ozon.finance.transaction_list",
            f"{date_from}..{date_to}",
            len(out),
            error=error_msg,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    return out


def fetch_cash_flow_statement(
    client_id: str,
    api_key: str,
    date_from: str,
    date_to: str,
    with_details: bool = True,
    page_size: int = 1000,
    company_id: str | None = None,
) -> dict[str, Any]:
    """POST /v1/finance/cash-flow-statement/list — сводный отчёт о движении.

    with_details=True обязателен — иначе теряем delivery/return/services/items
    и invoice_transfer (сумма к выплате за неделю).

    Возвращает dict {cash_flows: [...], details: [...], page_count: N}.
    Каждый элемент cash_flows[i] и details[i] — недельный период (Ozon режет
    автоматически по неделям). cash_flows[i] = агрегаты (orders/returns/...),
    details[i] = разбивка (delivery.items, services.items, invoice_transfer, ...).

    Длина периода Ozon не лимитирована в swagger, но > 3 мес начинает плодить
    страницы. Постранично через page/page_count.
    """
    url = f"{_BASE}/v1/finance/cash-flow-statement/list"
    headers = _fin_headers(client_id, api_key)
    started = time.monotonic()
    error_msg: str | None = None
    cash_flows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    page_count = 0
    try:
        page = 1
        while True:
            body = {
                "date": {
                    "from": _to_ozon_iso(date_from),
                    "to": f"{date_to}T23:59:59.999Z" if "T" not in date_to else date_to,
                },
                "page": page,
                "page_size": int(page_size),
                "with_details": bool(with_details),
            }
            data = _fin_post(url, headers, body)
            result = data.get("result") or {}
            cash_flows.extend(result.get("cash_flows") or [])
            details.extend(result.get("details") or [])
            page_count = int(result.get("page_count") or 0)
            logger.info(
                "[finance] cash-flow-statement page %d/%d, cash_flows=%d details=%d",
                page,
                page_count or page,
                len(cash_flows),
                len(details),
            )
            if page >= page_count or page_count == 0:
                break
            page += 1
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        _log_api_call(
            company_id,
            "ozon.finance.cash_flow_statement",
            f"{date_from}..{date_to}",
            len(cash_flows),
            error=error_msg,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    return {"cash_flows": cash_flows, "details": details, "page_count": page_count}


def fetch_finance_realization(
    client_id: str,
    api_key: str,
    month: int,
    year: int,
    company_id: str | None = None,
) -> dict[str, Any]:
    """POST /v2/finance/realization — отчёт о реализации за конкретный месяц.

    /v1 устарел и отдаёт 404 page not found; рабочий путь — /v2.
    Один запрос на месяц. Возвращает {header, rows[]}.
    Если отчёт за месяц ещё не сформирован (текущий или будущий месяц),
    Ozon ответит 404 {"code":5,"message":"Report was not found"} — это
    штатная ситуация, она прокидывается как HTTPError.
    """
    url = f"{_BASE}/v2/finance/realization"
    headers = _fin_headers(client_id, api_key)
    body = {"month": int(month), "year": int(year)}
    started = time.monotonic()
    error_msg: str | None = None
    result: dict[str, Any] = {}
    rows_count = 0
    try:
        data = _fin_post(url, headers, body)
        result = data.get("result") or {}
        rows_count = len(result.get("rows") or [])
        logger.info(
            "[finance] realization month=%d year=%d, rows %d",
            month,
            year,
            rows_count,
        )
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"[:500]
        raise
    finally:
        _log_api_call(
            company_id,
            "ozon.finance.realization",
            f"{year}-{month:02d}",
            rows_count,
            error=error_msg,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    return result
