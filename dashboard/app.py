"""Торговод · Отгрузки FBO — тонкое приложение на один модуль.

Single-tenant: токены Ozon берутся из .env (OZON_CLIENT_ID / OZON_API_KEY),
все данные лежат в локальных data/*.db. Никакой авторизации и мультиарендности —
это персональный инструмент, который человек поднимает у себя на компьютере.

Запуск:
    uvicorn dashboard.app:app --port 4000
"""

from __future__ import annotations

import logging
import os
import threading as _threading
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# ── FBO module imports (взяты из монолита Торговода, блок 296–374) ──────────────
from modules.fbo.slot_hunter import (
    confirm_supply_draft_v2 as slot_confirm_draft,
)
from modules.fbo.slot_hunter import (
    create_supply_draft_full as slot_create_draft_full,
)
from modules.fbo.slot_hunter import (
    get_available_timeslots as slot_get_timeslots,
)
from modules.fbo.slot_hunter import (
    get_drop_off_warehouses as slot_get_drop_off_warehouses,
)
from modules.fbo.slot_hunter import (
    get_fbo_clusters as slot_get_clusters,
)
from modules.fbo.slot_hunter import (
    get_scheduler as _get_slot_scheduler,
)
from modules.fbo.slot_hunter import (
    list_supply_orders as _slot_list_orders,
)
from modules.fbo.slot_hunter import (
    set_timeslot_sync as slot_set_timeslot,
)
from modules.fbo.slot_storage import (
    create_job as slot_create_job,
)
from modules.fbo.slot_storage import (
    delete_job as slot_delete_job,
)
from modules.fbo.slot_storage import (
    get_events as slot_get_events,
)
from modules.fbo.slot_storage import (
    get_job as slot_get_job,
)
from modules.fbo.slot_storage import (
    get_slot_connection,
    init_slot_db,
)
from modules.fbo.slot_storage import (
    list_jobs as slot_list_jobs,
)
from modules.fbo.slot_storage import (
    update_job_status as slot_update_status,
)
from modules.fbo.storage import (
    delete_supply_order,
    get_fbo_connection,
    get_fbo_financial_stats,
    get_fbo_stats,
    get_supply_order,
    get_supply_order_history,
    get_supply_orders,
    get_supply_orders_stats,
    init_fbo_db,
    record_order_history,
    update_order_timeslot,
    upsert_supply_order,
)
from modules.fbo.storage import (
    get_cluster_list as fbo_get_clusters,
)
from modules.fbo.storage import (
    get_sku_detail as fbo_get_sku_detail,
)
from modules.fbo.storage import (
    get_sku_list as fbo_get_sku_list,
)
from modules.fbo.storage import (
    get_sync_status as fbo_get_sync_status,
)
from modules.fbo.storage import (
    upsert_cluster_settings as fbo_upsert_cluster,
)
from modules.fbo.storage import (
    upsert_sku_settings as fbo_upsert_settings,
)
from shared.db.models import get_connection, init_db

load_dotenv()
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"
ENV_PATH = BASE_DIR / ".env"


def _init_databases() -> None:
    """В глобальном (single-tenant) режиме коннекторы не создают схему сами —
    создаём схемы трёх БД один раз при старте, чтобы пустые страницы не падали."""
    try:
        c = get_fbo_connection()
        init_fbo_db(c)
        c.close()
    except Exception:
        logger.exception("init fbo.db failed")
    try:
        c = get_slot_connection()
        init_slot_db(c)
        c.close()
    except Exception:
        logger.exception("init slot_hunter.db failed")
    try:
        c = get_connection()
        init_db(c)
        c.close()
    except Exception:
        logger.exception("init analytics.db failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_databases()
    # Фоновый поток слот-хантера: без него созданные задачи не опрашивают Ozon.
    if os.getenv("DISABLE_SLOT_HUNTER") != "1":
        try:
            _get_slot_scheduler().start()
        except Exception:
            logger.exception("slot-hunter scheduler start failed")
    yield


app = FastAPI(title="Торговод · Отгрузки FBO", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── Single-tenant: company_id всегда None → глобальные data/*.db + токены из .env
def _fbo_company_id(request: Request) -> None:
    return None


def _tokens_set() -> bool:
    return bool(os.getenv("OZON_CLIENT_ID") and os.getenv("OZON_API_KEY"))


_fbo_sync_running: dict = {}
_fbo_sync_lock = _threading.Lock()


# ── Настройки токенов ──────────────────────────────────────────────────────────
class SettingsBody(BaseModel):
    client_id: str
    api_key: str


def _write_env_tokens(client_id: str, api_key: str) -> None:
    """Сохранить токены в .env (создаёт/обновляет нужные строки) и в текущий процесс."""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    kept = [
        ln
        for ln in lines
        if not ln.strip().startswith(("OZON_CLIENT_ID=", "OZON_API_KEY="))
    ]
    kept.append(f"OZON_CLIENT_ID={client_id}")
    kept.append(f"OZON_API_KEY={api_key}")
    ENV_PATH.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.environ["OZON_CLIENT_ID"] = client_id
    os.environ["OZON_API_KEY"] = api_key


@app.get("/", response_class=HTMLResponse)
async def root():
    if not _tokens_set():
        return RedirectResponse("/settings", status_code=302)
    return RedirectResponse("/fbo", status_code=302)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cid = os.getenv("OZON_CLIENT_ID", "")
    masked = (cid[:3] + "•••••") if cid else ""
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"active_page": "settings", "client_id_masked": masked, "has_tokens": _tokens_set()},
    )


@app.post("/api/settings")
async def api_settings_save(body: SettingsBody):
    client_id = body.client_id.strip()
    api_key = body.api_key.strip()
    if not client_id or not api_key:
        return JSONResponse({"ok": False, "error": "Заполните оба поля"}, status_code=400)
    _write_env_tokens(client_id, api_key)
    return {"ok": True}


# ════════════════════════════════════════════════════════════════════════════════
# FBO routes — перенесены из монолита Торговода (dashboard/app.py:9590–10124).
# Единственное изменение: _fbo_company_id всегда None (single-tenant).
# ════════════════════════════════════════════════════════════════════════════════


@app.get("/fbo", response_class=HTMLResponse)
async def fbo_page(request: Request):
    if not _tokens_set():
        return RedirectResponse("/settings", status_code=302)
    return templates.TemplateResponse(request, "fbo.html", {"active_page": "fbo"})


@app.get("/api/fbo/skus")
async def api_fbo_skus(request: Request):
    conn = get_fbo_connection(company_id=_fbo_company_id(request))
    try:
        skus = fbo_get_sku_list(conn)
    finally:
        conn.close()
    return {"skus": skus}


@app.get("/api/fbo/stats")
async def api_fbo_stats(request: Request):
    from shared.db.models import get_connection as _get_analytics

    cid = _fbo_company_id(request)
    fbo = get_fbo_connection(company_id=cid)
    analytics = _get_analytics(company_id=cid)
    try:
        stats = get_fbo_stats(fbo)
        stats.update(get_fbo_financial_stats(fbo, analytics))
    finally:
        fbo.close()
        analytics.close()
    return stats


@app.get("/api/fbo/sku/{sku}")
async def api_fbo_sku_detail(request: Request, sku: str):
    conn = get_fbo_connection(company_id=_fbo_company_id(request))
    try:
        detail = fbo_get_sku_detail(conn, sku)
        if not detail:
            return JSONResponse({"error": "not found"}, status_code=404)
    finally:
        conn.close()
    return detail


class FboSettingsBody(BaseModel):
    is_active: int | None = None
    tags: str | None = None
    comment: str | None = None
    to_order: int | None = None
    priority: float | None = None


@app.post("/api/fbo/sku/{sku}/settings")
async def api_fbo_sku_settings(request: Request, sku: str, body: FboSettingsBody):
    conn = get_fbo_connection(company_id=_fbo_company_id(request))
    try:
        fbo_upsert_settings(
            conn,
            sku,
            is_active=body.is_active,
            tags=body.tags,
            comment=body.comment,
            to_order=body.to_order,
            priority=body.priority,
        )
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/fbo/barcode/{offer_id}")
async def api_fbo_barcode(offer_id: str):
    from fastapi.responses import Response

    from modules.fbo.barcode import get_barcode_for_sku

    try:
        png_bytes, display_value = get_barcode_for_sku("", offer_id)
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={"Content-Disposition": f'attachment; filename="{offer_id}.png"'},
        )
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/fbo/sync")
async def api_fbo_sync(request: Request):
    cid = _fbo_company_id(request)
    with _fbo_sync_lock:
        if _fbo_sync_running.get(cid):
            return {"started": False, "message": "Already running"}
        _fbo_sync_running[cid] = True

    def _do_sync():
        try:
            from pipeline import run_pipeline

            run_pipeline()
        finally:
            with _fbo_sync_lock:
                _fbo_sync_running.pop(cid, None)

    _threading.Thread(target=_do_sync, daemon=True).start()
    return {"started": True}


@app.get("/api/fbo/sync/status")
async def api_fbo_sync_status(request: Request):
    cid = _fbo_company_id(request)
    return {"running": bool(_fbo_sync_running.get(cid))}


@app.get("/api/fbo/sync/last-status")
async def api_fbo_sync_last_status(request: Request):
    conn = get_fbo_connection(company_id=_fbo_company_id(request))
    try:
        steps = fbo_get_sync_status(conn)
    finally:
        conn.close()
    return {"steps": steps}


@app.get("/fbo/clusters", response_class=HTMLResponse)
async def fbo_clusters_page(request: Request):
    return templates.TemplateResponse(request, "fbo_clusters.html", {"active_page": "fbo"})


@app.get("/api/fbo/clusters")
async def api_fbo_clusters(request: Request):
    conn = get_fbo_connection(company_id=_fbo_company_id(request))
    try:
        clusters = fbo_get_clusters(conn)
    finally:
        conn.close()
    return {"clusters": clusters}


class FboClusterSettingsBody(BaseModel):
    is_active: int | None = None
    lead_time_days: int | None = None
    fallback_cluster: str | None = None
    clear_fallback: bool = False
    priority: int | None = None


@app.post("/api/fbo/clusters/{cluster_name}/settings")
async def api_fbo_cluster_settings(request: Request, cluster_name: str, body: FboClusterSettingsBody):
    conn = get_fbo_connection(company_id=_fbo_company_id(request))
    try:
        fbo_upsert_cluster(
            conn,
            cluster_name,
            is_active=body.is_active,
            lead_time_days=body.lead_time_days,
            fallback_cluster=None if body.clear_fallback else body.fallback_cluster,
            priority=body.priority,
        )
    finally:
        conn.close()
    return {"ok": True}


# ── FBO Slot Hunter ─────────────────────────────────────────────────────────────


@app.get("/fbo/slot-hunter", response_class=HTMLResponse)
async def fbo_slot_hunter_page(request: Request):
    return templates.TemplateResponse(request, "fbo_slot_hunter.html", {"active_page": "fbo"})


@app.get("/api/fbo/slot-hunter/supply-orders")
async def api_slot_hunter_supply_orders(q: str = "", states: str = ""):
    try:
        state_list = [s.strip() for s in states.split(",") if s.strip()] if states else None
        orders = _slot_list_orders(states=state_list)
    except Exception as e:
        logging.getLogger(__name__).exception("slot-hunter supply-orders error")
        return {"orders": [], "error": str(e)}
    if q:
        q_lower = q.lower()
        orders = [
            o
            for o in orders
            if q_lower in str(o.get("supply_order_id", "")).lower()
            or q_lower in (o.get("order_number") or "").lower()
            or q_lower in (o.get("supply_order_number") or "").lower()
        ]
    return {"orders": orders[:100], "total": len(orders)}


class SlotJobCreateBody(BaseModel):
    supply_order_id: int
    supply_order_num: str = ""
    target_date_from: str
    target_date_to: str
    target_time_from: str
    target_time_to: str
    interval_sec: int = 60


@app.post("/api/fbo/slot-hunter/jobs")
async def api_slot_hunter_create_job(body: SlotJobCreateBody):
    conn = get_slot_connection()
    try:
        job_id = slot_create_job(
            conn,
            supply_order_id=body.supply_order_id,
            supply_order_num=body.supply_order_num,
            target_date_from=body.target_date_from,
            target_date_to=body.target_date_to,
            target_time_from=body.target_time_from,
            target_time_to=body.target_time_to,
            interval_sec=max(30, body.interval_sec),
        )
    finally:
        conn.close()
    return {"id": job_id, "ok": True}


@app.get("/api/fbo/slot-hunter/jobs")
async def api_slot_hunter_list_jobs():
    conn = get_slot_connection()
    try:
        jobs = slot_list_jobs(conn)
    finally:
        conn.close()
    return {"jobs": jobs}


@app.post("/api/fbo/slot-hunter/jobs/{job_id}/pause")
async def api_slot_hunter_pause(job_id: int):
    conn = get_slot_connection()
    try:
        slot_update_status(conn, job_id, "paused")
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/fbo/slot-hunter/jobs/{job_id}/resume")
async def api_slot_hunter_resume(job_id: int):
    conn = get_slot_connection()
    try:
        slot_update_status(conn, job_id, "active")
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/fbo/slot-hunter/jobs/{job_id}")
async def api_slot_hunter_delete(job_id: int):
    conn = get_slot_connection()
    try:
        slot_delete_job(conn, job_id)
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/fbo/slot-hunter/jobs/{job_id}/events")
async def api_slot_hunter_events(job_id: int, limit: int = 50):
    conn = get_slot_connection()
    try:
        events = slot_get_events(conn, job_id, limit=limit)
        job = slot_get_job(conn, job_id)
    finally:
        conn.close()
    return {"events": events, "job": job}


# ── FBO Orders ──────────────────────────────────────────────────────────────────


@app.get("/fbo/orders", response_class=HTMLResponse)
async def fbo_orders_page(request: Request):
    return templates.TemplateResponse(request, "fbo_orders.html", {"active_page": "fbo"})


def _sync_orders_to_db(company_id, states: list | None = None) -> list:
    orders = _slot_list_orders(states=states)
    conn = get_fbo_connection(company_id=company_id)
    try:
        for order in orders:
            upsert_supply_order(conn, order)
            if order.get("state"):
                record_order_history(conn, order["supply_order_id"], order["state"])
    finally:
        conn.close()
    return orders


@app.get("/api/fbo/orders")
async def api_fbo_orders(request: Request, sync: bool = False, states: str = ""):
    cid = _fbo_company_id(request)
    state_list = [s.strip() for s in states.split(",") if s.strip()] if states else None
    try:
        if sync:
            orders = _sync_orders_to_db(cid, states=state_list)
        else:
            conn = get_fbo_connection(company_id=cid)
            try:
                orders = get_supply_orders(conn, states=state_list)
            finally:
                conn.close()
            if not orders:
                orders = _sync_orders_to_db(cid, states=state_list)
    except Exception as e:
        logger.exception("api_fbo_orders error")
        return {"orders": [], "error": str(e)}
    conn = get_fbo_connection(company_id=cid)
    try:
        stats = get_supply_orders_stats(conn)
    finally:
        conn.close()
    return {"orders": orders, "stats": stats}


@app.post("/api/fbo/orders/sync")
async def api_fbo_orders_sync(request: Request):
    cid = _fbo_company_id(request)
    try:
        orders = _sync_orders_to_db(
            cid,
            states=[
                "READY_TO_SUPPLY",
                "DATA_FILLING",
                "DATA_FILLED",
                "CONFIRMED",
                "APPROVED",
                "SUPPLYING",
                "ACCEPTANCE",
            ],
        )
    except Exception as e:
        logger.exception("api_fbo_orders_sync error")
        return {"ok": False, "error": str(e)}
    return {"ok": True, "synced": len(orders)}


@app.delete("/api/fbo/orders/{order_id}")
async def api_fbo_order_delete(request: Request, order_id: int):
    conn = get_fbo_connection(company_id=_fbo_company_id(request))
    try:
        delete_supply_order(conn, order_id)
    except Exception as e:
        logger.exception("api_fbo_order_delete error")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/fbo/orders/{order_id}")
async def api_fbo_order_detail(request: Request, order_id: int):
    conn = get_fbo_connection(company_id=_fbo_company_id(request))
    try:
        order = get_supply_order(conn, order_id)
        history = get_supply_order_history(conn, order_id)
    finally:
        conn.close()
    if not order:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"order": order, "history": history}


class FboOrderSlotHunterBody(BaseModel):
    target_date_from: str
    target_date_to: str
    target_time_from: str
    target_time_to: str
    interval_sec: int = 60


@app.post("/api/fbo/orders/{order_id}/slot-hunter")
async def api_fbo_order_start_hunter(request: Request, order_id: int, body: FboOrderSlotHunterBody):
    conn_fbo = get_fbo_connection(company_id=_fbo_company_id(request))
    try:
        order = get_supply_order(conn_fbo, order_id)
    finally:
        conn_fbo.close()
    if not order:
        return JSONResponse({"error": "Заявка не найдена"}, status_code=404)
    conn_slot = get_slot_connection()
    try:
        job_id = slot_create_job(
            conn_slot,
            supply_order_id=order_id,
            supply_order_num=order.get("order_number", ""),
            target_date_from=body.target_date_from,
            target_date_to=body.target_date_to,
            target_time_from=body.target_time_from,
            target_time_to=body.target_time_to,
            interval_sec=max(30, body.interval_sec),
        )
    finally:
        conn_slot.close()
    return {"ok": True, "job_id": job_id}


@app.get("/api/fbo/orders/{order_id}/timeslots")
async def api_fbo_order_timeslots(order_id: int):
    try:
        timeslots = slot_get_timeslots(order_id)
        return {"timeslots": timeslots}
    except Exception as e:
        logger.exception("api_fbo_order_timeslots error")
        return JSONResponse({"error": str(e)}, status_code=500)


class FboSetTimeslotBody(BaseModel):
    slot_from: str
    slot_to: str


@app.post("/api/fbo/orders/{order_id}/timeslot")
async def api_fbo_order_set_timeslot(request: Request, order_id: int, body: FboSetTimeslotBody):
    try:
        status = slot_set_timeslot(order_id, body.slot_from, body.slot_to)
    except Exception as e:
        logger.exception("api_fbo_order_set_timeslot error")
        return JSONResponse({"error": str(e)}, status_code=500)
    if status == "STATUS_SUCCESS":
        conn = get_fbo_connection(company_id=_fbo_company_id(request))
        try:
            update_order_timeslot(conn, order_id, body.slot_from, body.slot_to)
        finally:
            conn.close()
        return {"ok": True}
    return JSONResponse({"ok": False, "error": f"Статус операции: {status}"}, status_code=400)


@app.get("/api/fbo/supply/clusters")
async def api_fbo_supply_clusters():
    try:
        clusters = slot_get_clusters()
        return {"clusters": clusters}
    except Exception as e:
        logger.exception("api_fbo_supply_clusters error")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/fbo/supply/drop-off-warehouses")
async def api_fbo_drop_off_warehouses(q: str = ""):
    try:
        warehouses = slot_get_drop_off_warehouses(q)
        return {"warehouses": warehouses}
    except Exception as e:
        logger.exception("api_fbo_drop_off_warehouses error")
        return JSONResponse({"error": str(e)}, status_code=500)


class FboDraftCreateBody(BaseModel):
    macrolocal_cluster_id: int
    items: list[dict]
    supply_type: str = "DIRECT"
    drop_off_warehouse_id: int | None = None
    drop_off_warehouse_type: str = "CROSS_DOCK"


@app.post("/api/fbo/orders/draft")
async def api_fbo_orders_create_draft(body: FboDraftCreateBody):
    try:
        draft_id, warehouses, timeslots = slot_create_draft_full(
            body.macrolocal_cluster_id,
            body.items,
            supply_type=body.supply_type,
            drop_off_warehouse_id=body.drop_off_warehouse_id,
            drop_off_warehouse_type=body.drop_off_warehouse_type,
        )
        return {"draft_id": draft_id, "warehouses": warehouses, "timeslots": timeslots}
    except Exception as e:
        logger.exception("api_fbo_orders_create_draft error")
        return JSONResponse({"error": str(e)}, status_code=500)


class FboDraftConfirmBody(BaseModel):
    macrolocal_cluster_id: int
    storage_warehouse_id: int
    slot_from: str
    slot_to: str
    supply_type: str = "DIRECT"


@app.post("/api/fbo/orders/draft/{draft_id}/confirm")
async def api_fbo_draft_confirm(draft_id: str, body: FboDraftConfirmBody):
    try:
        result = slot_confirm_draft(
            draft_id,
            body.macrolocal_cluster_id,
            body.storage_warehouse_id,
            body.slot_from,
            body.slot_to,
            body.supply_type,
        )
        return {"ok": True, "result": result}
    except Exception as e:
        logger.exception("api_fbo_draft_confirm error")
        return JSONResponse({"error": str(e)}, status_code=500)
