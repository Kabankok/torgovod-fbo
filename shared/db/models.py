"""SQLite database: schema init and helpers.

DB path: data/analytics.db (override with ANALYTICS_DB_PATH env var).
Run from project root so relative path resolves correctly.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(os.getenv("ANALYTICS_DB_PATH", "data/analytics.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;

-- Product catalog: bridge between sku, offer_id (seller article), product_id
CREATE TABLE IF NOT EXISTS products (
    sku         TEXT PRIMARY KEY,
    product_id  TEXT,
    offer_id    TEXT,
    name        TEXT,
    price       REAL,
    old_price   REAL,
    image_url   TEXT,
    is_archived INTEGER DEFAULT 0,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_offer_id ON products(offer_id);

-- Sales analytics per SKU per day (from /v1/analytics/data)
-- Full metric set (Premium Plus): 18 metrics across 2 API requests
CREATE TABLE IF NOT EXISTS sku_analytics_daily (
    sku                  TEXT NOT NULL,
    date                 TEXT NOT NULL,
    -- Basic metrics (all sellers)
    revenue              REAL,
    ordered_units        INTEGER,
    -- Visibility
    hits_view            INTEGER,
    hits_view_search     INTEGER,
    hits_view_pdp        INTEGER,
    -- Sessions (unique visitors)
    session_view         INTEGER,
    session_view_search  INTEGER,
    session_view_pdp     INTEGER,
    -- Add-to-cart
    hits_tocart          INTEGER,
    hits_tocart_search   INTEGER,
    hits_tocart_pdp      INTEGER,
    -- Conversion to cart
    conv_tocart          REAL,
    conv_tocart_search   REAL,
    conv_tocart_pdp      REAL,
    -- Order outcomes
    delivered_units      INTEGER,
    returns              INTEGER,
    cancellations        INTEGER,
    -- Search position
    position_category    REAL,
    PRIMARY KEY (sku, date)
);

-- Stock totals per SKU per day (FBO + FBS aggregate)
CREATE TABLE IF NOT EXISTS sku_stocks_daily (
    sku        TEXT NOT NULL,
    date       TEXT NOT NULL,
    stock_fbo  INTEGER,
    stock_fbs  INTEGER,
    PRIMARY KEY (sku, date)
);

-- Stock per warehouse per SKU per day (Студенческий, Офис, FBO warehouses, etc.)
-- Source: /v2/analytics/stock_on_warehouses (real warehouse names)
CREATE TABLE IF NOT EXISTS sku_stocks_by_warehouse (
    sku             TEXT NOT NULL,
    date            TEXT NOT NULL,
    warehouse_name  TEXT NOT NULL,
    warehouse_type  TEXT,     -- fbo | fbs
    present         INTEGER,  -- free_to_sell_amount
    promised        INTEGER,  -- promised_amount (in transit to buyer)
    reserved        INTEGER,  -- reserved_amount (held for pending orders)
    PRIMARY KEY (sku, date, warehouse_name)
);
CREATE INDEX IF NOT EXISTS idx_stocks_warehouse ON sku_stocks_by_warehouse(warehouse_name, date);

-- Price snapshots per SKU per day
CREATE TABLE IF NOT EXISTS sku_prices_daily (
    sku        TEXT NOT NULL,
    date       TEXT NOT NULL,
    price      REAL,
    old_price  REAL,
    PRIMARY KEY (sku, date)
);

-- Снимки СПП (скидки площадки покупателю) — ежедневно по SKU
-- Источник: POST /v1/product/prices/details (требует Premium Pro).
-- seller_price — цена с акциями продавца (без СПП).
-- customer_price — цена для покупателя (с учётом скидки Ozon).
-- spp_rub = seller_price − customer_price (деньги, которые доплачивает Ozon).
-- Все деньги — в копейках, чтобы не терять копейки на округлениях.
CREATE TABLE IF NOT EXISTS sku_spp_daily (
    company_id          TEXT NOT NULL,
    sku                 TEXT NOT NULL,
    offer_id            TEXT,
    date                TEXT NOT NULL,
    seller_price_kop    INTEGER,
    customer_price_kop  INTEGER,
    spp_pct             REAL,
    spp_rub_kop         INTEGER,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (company_id, sku, date)
);
CREATE INDEX IF NOT EXISTS idx_sku_spp_date ON sku_spp_daily(date);

-- Unit economics per SKU (synced from Google Sheets "ЮНИТ-Э 2.0")
-- cost_per_pack is the main cost figure: price paid for one sellable unit (may contain multiple pieces)
CREATE TABLE IF NOT EXISTS sku_unit_economics (
    offer_id       TEXT PRIMARY KEY,
    sku            TEXT,             -- matched from products table
    units_per_pack INTEGER,          -- pieces in one sellable package
    cost_per_unit  REAL,             -- purchase price per single piece
    cost_per_pack  REAL,             -- purchase price per package (what we actually sell)
    tax_regime     TEXT,             -- e.g. "АУСН Д-Р"
    tax_rate       REAL,             -- e.g. 20 (percent)
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ue_sku ON sku_unit_economics(sku);

-- Ad campaigns (from Performance API)
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id      TEXT PRIMARY KEY,
    title            TEXT,
    state            TEXT,
    adv_object_type  TEXT,
    payment_type     TEXT,
    placement        TEXT,
    created_at       TEXT,
    updated_at       TEXT NOT NULL
);

-- Which SKUs are in which campaigns
CREATE TABLE IF NOT EXISTS campaign_products (
    campaign_id  TEXT NOT NULL,
    sku          TEXT NOT NULL,
    bid_api      INTEGER,   -- ставка в единицах API (1 руб = 1_000_000)
    bid_rub      REAL,      -- ставка в рублях (bid_api / 1_000_000)
    PRIMARY KEY (campaign_id, sku)
);

-- Campaign-level daily stats (spend, views, clicks, orders)
-- cart_adds — «в корзину, шт»: распределяется sync_campaign_sku_stats
-- из периодного агрегата toCart пропорционально кликам в день.
CREATE TABLE IF NOT EXISTS campaign_stats_daily (
    campaign_id    TEXT NOT NULL,
    date           TEXT NOT NULL,
    views          INTEGER,
    clicks         INTEGER,
    spend          REAL,
    orders         INTEGER,
    orders_revenue REAL,
    cart_adds      INTEGER DEFAULT 0,
    PRIMARY KEY (campaign_id, date)
);

-- История изменений ставок
CREATE TABLE IF NOT EXISTS campaign_bid_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  TEXT NOT NULL,
    sku          TEXT NOT NULL,
    bid_old_rub  REAL,
    bid_new_rub  REAL,
    reason       TEXT,
    triggered_by TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bid_history_campaign ON campaign_bid_history(campaign_id, created_at);

-- Настройки AI-управления по кампании
CREATE TABLE IF NOT EXISTS campaign_ai_settings (
    campaign_id  TEXT PRIMARY KEY,
    ai_enabled   INTEGER DEFAULT 1,
    target_drr   REAL,
    min_bid_rub  REAL,
    max_bid_rub  REAL,
    updated_at   TEXT NOT NULL
);

-- Ad stats per SKU per campaign (aggregated over the last sync period)
-- Source: Performance API /api/client/statistics/campaign/product/json
CREATE TABLE IF NOT EXISTS campaign_sku_stats (
    campaign_id    TEXT NOT NULL,
    sku            TEXT NOT NULL,
    period_days    INTEGER,
    views          INTEGER,
    clicks         INTEGER,
    spend          REAL,
    orders         INTEGER,
    orders_revenue REAL,
    ctr            REAL,
    drr            REAL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (campaign_id, sku)
);
CREATE INDEX IF NOT EXISTS idx_campaign_sku_stats_sku ON campaign_sku_stats(sku);

-- Лог действий AI
CREATE TABLE IF NOT EXISTS ads_ai_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id  TEXT NOT NULL,
    sku          TEXT,
    action       TEXT NOT NULL,
    detail       TEXT,
    applied      INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ads_ai_log_campaign ON ads_ai_log(campaign_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ads_ai_log_applied ON ads_ai_log(campaign_id, applied);

-- Лог действий пользователя по товарам с нулевым остатком
-- Используется для сохранения состояния между обновлениями страницы
CREATE TABLE IF NOT EXISTS zero_stock_log (
    sku          TEXT PRIMARY KEY,
    ad_paused_at TEXT,
    stocked_at   TEXT,
    stocked_qty  INTEGER,
    dismissed_at TEXT   -- "убрать из списка" на 30 дней
);

-- Связка товаров: один и тот же товар, разная комплектация / фасовка
-- Хранится в нормализованном виде: sku_a < sku_b лексикографически
CREATE TABLE IF NOT EXISTS sku_variants (
    sku_a       TEXT NOT NULL,
    sku_b       TEXT NOT NULL,
    label       TEXT,           -- пометка, напр. "5 шт"
    added_by    TEXT DEFAULT 'manual',  -- 'manual' | 'auto'
    created_at  TEXT NOT NULL,
    PRIMARY KEY (sku_a, sku_b)
);
CREATE INDEX IF NOT EXISTS idx_sku_variants_a ON sku_variants(sku_a);
CREATE INDEX IF NOT EXISTS idx_sku_variants_b ON sku_variants(sku_b);

CREATE INDEX IF NOT EXISTS idx_campaign_stats_campaign_date ON campaign_stats_daily(campaign_id, date);
CREATE INDEX IF NOT EXISTS idx_campaign_stats_date ON campaign_stats_daily(date);

CREATE INDEX IF NOT EXISTS idx_sku_analytics_sku_date ON sku_analytics_daily(sku, date);
CREATE INDEX IF NOT EXISTS idx_sku_stocks_sku ON sku_stocks_daily(sku, date);

-- Agent memory: key-value store for learned patterns (scope: 'global', 'sku:<sku>', 'campaign:<id>')
CREATE TABLE IF NOT EXISTS agent_memory (
    scope       TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

-- User ↔ Agent dialogue messages
CREATE TABLE IF NOT EXISTS agent_chat (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,   -- 'user' | 'agent'
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- User instructions / business rules the agent must follow
CREATE TABLE IF NOT EXISTS user_instructions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    scope       TEXT DEFAULT 'global',   -- 'global', 'sku:<sku>', 'campaign:<id>'
    is_active   INTEGER DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_instructions_active ON user_instructions(is_active);

-- SEO: product content snapshot (attributes + keywords + description)
-- Fetched weekly via /v4/product/info/attributes + /v1/product/info/description
CREATE TABLE IF NOT EXISTS product_content (
    sku                      TEXT PRIMARY KEY,
    offer_id                 TEXT,
    fetched_at               TEXT NOT NULL,
    description_text         TEXT,           -- text description
    keywords_raw             TEXT,           -- current keywords string (semicolon-separated)
    keywords_attr_id         INTEGER,        -- attribute_id of the keywords field in this category
    description_category_id  TEXT,
    attributes_json          TEXT            -- full attributes JSON for completeness analysis
);
CREATE INDEX IF NOT EXISTS idx_product_content_offer ON product_content(offer_id);

-- SEO: content rating from Ozon (/v1/product/rating-by-sku)
CREATE TABLE IF NOT EXISTS product_content_rating (
    sku                      TEXT NOT NULL,
    date                     TEXT NOT NULL,
    rating                   REAL,           -- 0–100 content quality score
    groups_json              TEXT,           -- rating breakdown by blocks
    improve_attributes_json  TEXT,           -- list of attribute_ids to fill for improvement
    PRIMARY KEY (sku, date)
);
CREATE INDEX IF NOT EXISTS idx_content_rating_sku ON product_content_rating(sku, date);

-- SEO: AI-generated recommendations with lifecycle tracking
CREATE TABLE IF NOT EXISTS seo_recommendations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sku              TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    rec_type         TEXT NOT NULL,  -- 'keywords_update' | 'attributes_fill' | 'content_quality'
    priority         INTEGER DEFAULT 2,  -- 1=critical, 2=high, 3=medium
    status           TEXT DEFAULT 'pending',  -- pending | applied | skipped | rejected
    keywords_before  TEXT,           -- keywords string before change
    keywords_after   TEXT,           -- proposed keywords string
    ai_reasoning     TEXT,           -- Claude explanation
    impact_score     REAL,           -- estimated impact 0–100
    applied_at       TEXT,
    result_note      TEXT            -- outcome after applying
);
CREATE INDEX IF NOT EXISTS idx_seo_rec_sku ON seo_recommendations(sku, status);
CREATE INDEX IF NOT EXISTS idx_seo_rec_status ON seo_recommendations(status, created_at);

-- Search position per SKU per day (from /v1/analytics/product-queries)
-- position = avg search position (Premium only, else NULL)
CREATE TABLE IF NOT EXISTS sku_search_position_daily (
    sku                  TEXT NOT NULL,
    date                 TEXT NOT NULL,
    position             REAL,
    unique_search_users  INTEGER,
    gmv                  REAL,
    PRIMARY KEY (sku, date)
);
CREATE INDEX IF NOT EXISTS idx_search_position_sku_date ON sku_search_position_daily(sku, date);

-- Top search queries per SKU per day (from /v1/analytics/product-queries/details)
CREATE TABLE IF NOT EXISTS sku_search_queries (
    sku                  TEXT NOT NULL,
    date                 TEXT NOT NULL,
    query                TEXT NOT NULL,
    position             REAL,
    gmv                  REAL,
    order_count          INTEGER,
    unique_search_users  INTEGER,
    PRIMARY KEY (sku, date, query)
);
CREATE INDEX IF NOT EXISTS idx_search_queries_sku_date ON sku_search_queries(sku, date);

-- Акции Ozon (платформенные)
CREATE TABLE IF NOT EXISTS ozon_actions (
    action_id           INTEGER PRIMARY KEY,
    company_id          TEXT NOT NULL,
    title               TEXT,
    action_type         TEXT,
    date_start          TEXT,
    date_end            TEXT,
    freeze_date         TEXT,
    potential_count     INTEGER DEFAULT 0,
    participating_count INTEGER DEFAULT 0,
    is_participating    INTEGER DEFAULT 0,
    synced_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_ozon_actions_company ON ozon_actions(company_id);

-- Товары в акциях Ozon (кандидаты + участники)
CREATE TABLE IF NOT EXISTS ozon_action_products (
    action_id           INTEGER NOT NULL,
    company_id          TEXT NOT NULL,
    product_id          TEXT NOT NULL,
    offer_id            TEXT,
    sku                 TEXT,
    current_price       REAL,
    max_action_price    REAL,
    action_price        REAL,
    is_participating    INTEGER DEFAULT 0,
    margin_pct          REAL,
    recommendation      TEXT,
    synced_at           TEXT,
    PRIMARY KEY (action_id, company_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_ozon_action_products_action ON ozon_action_products(action_id, company_id);
CREATE INDEX IF NOT EXISTS idx_ozon_action_products_offer ON ozon_action_products(company_id, offer_id);
"""

_MIGRATIONS = [
    # analytics full metrics
    "ALTER TABLE sku_analytics_daily ADD COLUMN hits_view INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN hits_view_search INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN hits_view_pdp INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN session_view INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN session_view_search INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN session_view_pdp INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN hits_tocart_search INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN hits_tocart_pdp INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN conv_tocart REAL",
    "ALTER TABLE sku_analytics_daily ADD COLUMN conv_tocart_search REAL",
    "ALTER TABLE sku_analytics_daily ADD COLUMN conv_tocart_pdp REAL",
    "ALTER TABLE sku_analytics_daily ADD COLUMN delivered_units INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN returns INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN cancellations INTEGER",
    "ALTER TABLE sku_analytics_daily ADD COLUMN position_category REAL",
    # rename conversion_to_cart -> conv_tocart handled by having both columns
    # warehouse breakdown: add promised/reserved (from /v2/analytics/stock_on_warehouses)
    "ALTER TABLE sku_stocks_by_warehouse ADD COLUMN promised INTEGER",
    "ALTER TABLE sku_stocks_by_warehouse ADD COLUMN reserved INTEGER",
    # ads module: bid columns in campaign_products
    "ALTER TABLE campaign_products ADD COLUMN bid_api INTEGER",
    "ALTER TABLE campaign_products ADD COLUMN bid_rub REAL",
    # product image url
    "ALTER TABLE products ADD COLUMN image_url TEXT",
    # archived products from Ozon (visibility=ARCHIVED) — нужны для инвентаризации
    "ALTER TABLE products ADD COLUMN is_archived INTEGER DEFAULT 0",
    # local_archived: ручной архив пользователя. НЕ синхронизируется с Ozon
    # (в отличие от is_archived, которое перезаписывает sync_catalog каждый синк).
    # Сбрасывается только явным действием в UI. Архивные исключаются из sync_stocks.
    "ALTER TABLE products ADD COLUMN local_archived INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_products_local_archived ON products(local_archived)",
    # Wizard «Создать новый товар» (Фаза 3 plans/2026-06-03-product-creation-wizard).
    # Те же колонки уже есть в shared/company_schema.py:apply_company_schema для
    # company-DB; здесь дублируем для analytics-DB — фильтр moderation_status='active'
    # в modules/ads/metrics.py:_compute_abc_by_offer_id срывал /ads на 500
    # (no such column: p.moderation_status), когда conn идёт по fallback в SQLite.
    "ALTER TABLE products ADD COLUMN moderation_status TEXT DEFAULT 'active'",
    "ALTER TABLE products ADD COLUMN moderation_task_id INTEGER",
    "ALTER TABLE products ADD COLUMN moderation_error TEXT",
    "ALTER TABLE products ADD COLUMN created_via TEXT DEFAULT 'sync'",
    "UPDATE products SET moderation_status='active' WHERE moderation_status IS NULL",
    "UPDATE products SET created_via='sync' WHERE created_via IS NULL",
    # AI-3: DRR alert threshold per campaign
    "ALTER TABLE campaign_ai_settings ADD COLUMN drr_alert_threshold REAL",
    # Performance indexes
    "CREATE INDEX IF NOT EXISTS idx_ads_ai_log_applied ON ads_ai_log(campaign_id, applied)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_stats_campaign_date ON campaign_stats_daily(campaign_id, date)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_stats_date ON campaign_stats_daily(date)",
    "CREATE INDEX IF NOT EXISTS idx_sku_analytics_sku_date ON sku_analytics_daily(sku, date)",
    "CREATE INDEX IF NOT EXISTS idx_sku_stocks_sku ON sku_stocks_daily(sku, date)",
    "ALTER TABLE campaigns ADD COLUMN created_at TEXT",
    "ALTER TABLE campaigns ADD COLUMN placement TEXT",
    # Agent system: extra columns on ads_ai_log
    "ALTER TABLE ads_ai_log ADD COLUMN reasoning TEXT",
    "ALTER TABLE ads_ai_log ADD COLUMN lifecycle TEXT",
    "ALTER TABLE ads_ai_log ADD COLUMN confidence REAL",
    # Agent memory + chat tables (CREATE IF NOT EXISTS is safe to run multiple times)
    """CREATE TABLE IF NOT EXISTS agent_memory (
        scope      TEXT NOT NULL,
        key        TEXT NOT NULL,
        value      TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (scope, key)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_chat (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        role       TEXT NOT NULL,
        content    TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS user_instructions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        text       TEXT NOT NULL,
        scope      TEXT DEFAULT 'global',
        is_active  INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_instructions_active ON user_instructions(is_active)",
    # Chat archiving: mark old messages hidden without deleting them
    "ALTER TABLE agent_chat ADD COLUMN archived INTEGER DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_agent_chat_archived ON agent_chat(archived, created_at)",
    """CREATE TABLE IF NOT EXISTS zero_stock_log (
        sku TEXT PRIMARY KEY,
        ad_paused_at TEXT,
        stocked_at TEXT,
        stocked_qty INTEGER,
        dismissed_at TEXT
    )""",
    "ALTER TABLE zero_stock_log ADD COLUMN dismissed_at TEXT",
    """CREATE TABLE IF NOT EXISTS sku_variants (
        sku_a      TEXT NOT NULL,
        sku_b      TEXT NOT NULL,
        label      TEXT,
        added_by   TEXT DEFAULT 'manual',
        created_at TEXT NOT NULL,
        PRIMARY KEY (sku_a, sku_b)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sku_variants_a ON sku_variants(sku_a)",
    "CREATE INDEX IF NOT EXISTS idx_sku_variants_b ON sku_variants(sku_b)",
    """CREATE TABLE IF NOT EXISTS sku_search_position_daily (
    sku TEXT NOT NULL, date TEXT NOT NULL,
    position REAL, unique_search_users INTEGER, gmv REAL,
    PRIMARY KEY (sku, date)
)""",
    "CREATE INDEX IF NOT EXISTS idx_search_position_sku_date ON sku_search_position_daily(sku, date)",
    """CREATE TABLE IF NOT EXISTS sku_search_queries (
    sku TEXT NOT NULL, date TEXT NOT NULL, query TEXT NOT NULL,
    position REAL, gmv REAL, order_count INTEGER, unique_search_users INTEGER,
    PRIMARY KEY (sku, date, query)
)""",
    "CREATE INDEX IF NOT EXISTS idx_search_queries_sku_date ON sku_search_queries(sku, date)",
    # Blacklist: campaigns permanently excluded from AI and manual actions
    "ALTER TABLE campaigns ADD COLUMN blacklisted INTEGER DEFAULT 0",
    "ALTER TABLE campaigns ADD COLUMN blacklisted_at TEXT",
    # Archive: campaigns disabled and moved to archive (never reactivated; create new instead)
    "ALTER TABLE campaigns ADD COLUMN archived INTEGER DEFAULT 0",
    "ALTER TABLE campaigns ADD COLUMN archived_at TEXT",
    # Night pause mode: pause campaign 00:00–07:00 to save budget
    "ALTER TABLE campaign_ai_settings ADD COLUMN night_pause INTEGER DEFAULT 0",
    "ALTER TABLE campaign_ai_settings ADD COLUMN night_paused INTEGER DEFAULT 0",
    # SEO module: product content + rating + recommendations
    """CREATE TABLE IF NOT EXISTS product_content (
        sku TEXT PRIMARY KEY, offer_id TEXT, fetched_at TEXT NOT NULL,
        description_text TEXT, keywords_raw TEXT, keywords_attr_id INTEGER,
        description_category_id TEXT, attributes_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_product_content_offer ON product_content(offer_id)",
    """CREATE TABLE IF NOT EXISTS product_content_rating (
        sku TEXT NOT NULL, date TEXT NOT NULL,
        rating REAL, groups_json TEXT, improve_attributes_json TEXT,
        PRIMARY KEY (sku, date)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_content_rating_sku ON product_content_rating(sku, date)",
    """CREATE TABLE IF NOT EXISTS seo_recommendations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT NOT NULL, created_at TEXT NOT NULL,
        rec_type TEXT NOT NULL, priority INTEGER DEFAULT 2,
        status TEXT DEFAULT 'pending',
        keywords_before TEXT, keywords_after TEXT,
        ai_reasoning TEXT, impact_score REAL,
        applied_at TEXT, result_note TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_seo_rec_sku ON seo_recommendations(sku, status)",
    "CREATE INDEX IF NOT EXISTS idx_seo_rec_status ON seo_recommendations(status, created_at)",
    # SEO rework queue: SKUs paused due to zero orders → need SEO/card improvement
    """CREATE TABLE IF NOT EXISTS seo_rework_queue (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        sku          TEXT NOT NULL UNIQUE,
        offer_id     TEXT,
        campaign_id  TEXT,
        pause_reason TEXT,
        added_at     TEXT NOT NULL,
        seo_status   TEXT DEFAULT 'pending',
        seo_done_at  TEXT,
        notes        TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_seo_queue_status ON seo_rework_queue(seo_status, added_at)",
    # Promotions module: Ozon actions + action products
    """CREATE TABLE IF NOT EXISTS ozon_actions (
        action_id           INTEGER PRIMARY KEY,
        company_id          TEXT NOT NULL,
        title               TEXT,
        action_type         TEXT,
        date_start          TEXT,
        date_end            TEXT,
        freeze_date         TEXT,
        potential_count     INTEGER DEFAULT 0,
        participating_count INTEGER DEFAULT 0,
        is_participating    INTEGER DEFAULT 0,
        synced_at           TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ozon_actions_company ON ozon_actions(company_id)",
    """CREATE TABLE IF NOT EXISTS ozon_action_products (
        action_id           INTEGER NOT NULL,
        company_id          TEXT NOT NULL,
        product_id          TEXT NOT NULL,
        offer_id            TEXT,
        sku                 TEXT,
        current_price       REAL,
        max_action_price    REAL,
        action_price        REAL,
        is_participating    INTEGER DEFAULT 0,
        margin_pct          REAL,
        recommendation      TEXT,
        synced_at           TEXT,
        PRIMARY KEY (action_id, company_id, product_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ozon_action_products_action ON ozon_action_products(action_id, company_id)",
    "CREATE INDEX IF NOT EXISTS idx_ozon_action_products_offer ON ozon_action_products(company_id, offer_id)",
    # Ozon minimum bid per SKU (fetched from /api/client/min/sku, stored for at_min_bid rule)
    "ALTER TABLE campaign_products ADD COLUMN ozon_min_bid_rub REAL",
    # Time-based schedule: pause campaign during a configurable HH:MM–HH:MM window (Moscow time)
    "ALTER TABLE campaign_ai_settings ADD COLUMN schedule_enabled INTEGER DEFAULT 0",
    "ALTER TABLE campaign_ai_settings ADD COLUMN schedule_pause_from TEXT",
    "ALTER TABLE campaign_ai_settings ADD COLUMN schedule_pause_to TEXT",
    "ALTER TABLE campaign_ai_settings ADD COLUMN schedule_paused INTEGER DEFAULT 0",
    # Product hypotheses (A/B-тесты гипотез по товарам — Ozon + WB)
    """CREATE TABLE IF NOT EXISTS product_hypotheses (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        sku             TEXT,
        wb_article      TEXT,
        date_from       TEXT NOT NULL,
        date_to         TEXT NOT NULL,
        title           TEXT NOT NULL,
        text            TEXT,
        color           TEXT NOT NULL DEFAULT 'blue',
        is_global_ozon  INTEGER NOT NULL DEFAULT 0,
        is_global_wb    INTEGER NOT NULL DEFAULT 0,
        created_by      TEXT,
        created_at      TEXT DEFAULT (datetime('now', '+3 hours'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_hyp_sku ON product_hypotheses(sku)",
    "CREATE INDEX IF NOT EXISTS idx_hyp_wb  ON product_hypotheses(wb_article)",
    "CREATE INDEX IF NOT EXISTS idx_hyp_glob_ozon ON product_hypotheses(is_global_ozon)",
    "CREATE INDEX IF NOT EXISTS idx_hyp_glob_wb   ON product_hypotheses(is_global_wb)",
    # СПП — снимки скидки площадки покупателю
    """CREATE TABLE IF NOT EXISTS sku_spp_daily (
        company_id          TEXT NOT NULL,
        sku                 TEXT NOT NULL,
        offer_id            TEXT,
        date                TEXT NOT NULL,
        seller_price_kop    INTEGER,
        customer_price_kop  INTEGER,
        spp_pct             REAL,
        spp_rub_kop         INTEGER,
        created_at          TEXT NOT NULL,
        PRIMARY KEY (company_id, sku, date)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sku_spp_date ON sku_spp_daily(date)",
    "ALTER TABLE campaign_ai_settings ADD COLUMN focused INTEGER DEFAULT 0",
    # «В корзину, шт» — поле toCart из /api/client/statistics/campaign/product/json.
    # Daily-эндпоинт его не возвращает: значение раздаётся по дням пропорционально
    # кликам в sync_campaign_sku_stats.
    "ALTER TABLE campaign_stats_daily ADD COLUMN cart_adds INTEGER DEFAULT 0",
    # Фаза 4 plans/2026-05-29-ads-settings.md: причина пропуска решения
    # ('stale_data' — данные устарели, агент отложил применение, applied=-1).
    "ALTER TABLE ads_ai_log ADD COLUMN skip_reason TEXT",
    # Пункт 2 plans/2026-05-30-agent-learning-system.md (накопитель статистики).
    # Какое правило из modules/ads/rules.py:RULES породило это решение.
    # Заводим заранее (рекламы в бою сейчас нет) — чтобы к моменту реализации
    # «портфеля правил с репутацией» уже накопилась история по rule_id.
    "ALTER TABLE ads_ai_log ADD COLUMN rule_id TEXT",
    "CREATE INDEX IF NOT EXISTS idx_ads_ai_log_rule ON ads_ai_log(rule_id, applied)",
    # Состояние синхронизации рекламы (фаза 4 plans/2026-05-29-ads-settings.md).
    # Одна строка с id=1; пишется из modules.ads.sync.run_ads_sync.
    """CREATE TABLE IF NOT EXISTS ads_sync_state (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        last_attempt_at TEXT,
        last_success_at TEXT,
        last_error      TEXT,
        source          TEXT
    )""",
    # Гипотезы агента (фаза 7 plans/2026-05-29-ads-settings.md).
    # Каждая запись — предложение «поменять настройку X на Y» в ожидании подтверждения.
    """CREATE TABLE IF NOT EXISTS agent_hypotheses (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        setting_key     TEXT NOT NULL,
        current_value   TEXT,
        suggested_value TEXT NOT NULL,
        reasoning       TEXT NOT NULL,
        source          TEXT,
        status          TEXT DEFAULT 'pending',
        created_at      TEXT NOT NULL,
        resolved_at     TEXT,
        resolved_by     TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_agent_hypotheses_status ON agent_hypotheses(status, created_at)",
    # Пункт 3 plans/2026-05-31-point-3-hypotheses.md (фаза 1).
    # Тип гипотезы: 'user_product' (как было — все старые), 'agent_rule',
    # 'agent_experiment'. Тип 'agent_setting' НЕ используется — настройки
    # остаются в отдельной agent_hypotheses.
    "ALTER TABLE product_hypotheses ADD COLUMN kind TEXT DEFAULT 'user_product'",
    "CREATE INDEX IF NOT EXISTS idx_hyp_kind ON product_hypotheses(kind)",
    # Ссылка на запись в agent_campaign_experiments (для агентских гипотез).
    # NULL для пользовательских.
    "ALTER TABLE product_hypotheses ADD COLUMN experiment_id INTEGER",
    "CREATE INDEX IF NOT EXISTS idx_hyp_experiment ON product_hypotheses(experiment_id)",
    # Технические записи эксперимента агента: pending → applied/applied_dry_run →
    # confirmed/failed/rolled_back. Snapshot для отката (ставка/состояние до).
    """CREATE TABLE IF NOT EXISTS agent_campaign_experiments (
        id                              INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id                     TEXT NOT NULL,
        sku_list_json                   TEXT,
        action                          TEXT NOT NULL,
        action_params_json              TEXT,
        snapshot_before_json            TEXT,
        prediction_metric               TEXT,
        prediction_value_before         REAL,
        prediction_value_after          REAL,
        prediction_revenue_floor_pct    REAL,
        check_period_days               INTEGER DEFAULT 7,
        status                          TEXT DEFAULT 'pending',
        status_reason                   TEXT,
        priority                        INTEGER DEFAULT 5,
        created_at                      TEXT NOT NULL,
        applied_at                      TEXT,
        checked_at                      TEXT,
        rolled_back_at                  TEXT,
        ads_ai_log_id                   INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ace_campaign ON agent_campaign_experiments(campaign_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_ace_status ON agent_campaign_experiments(status, created_at)",
    # Пункт 10 plans/2026-05-31-point-10-net-margin.md (фаза 1).
    # Дневной снимок чистой маржи за окно (по умолчанию 30 дней) per-SKU.
    # Считается ночью cron-job'ом ads_net_margin_daily; читается агентом
    # и UI карточки кампании / главной страницы Реклама.
    # components_json — JSON с разбивкой { revenue, commission, logistics, ... }
    # для tooltip и аудита формулы.
    """CREATE TABLE IF NOT EXISTS sku_net_margin_daily (
        sku             TEXT NOT NULL,
        date            TEXT NOT NULL,
        window_days     INTEGER NOT NULL,
        days_covered    INTEGER NOT NULL,
        revenue         REAL NOT NULL,
        commission      REAL NOT NULL,
        logistics       REAL NOT NULL,
        storage         REAL NOT NULL,
        acquiring       REAL NOT NULL,
        ads_spend       REAL NOT NULL,
        returns_amount  REAL NOT NULL,
        tax             REAL NOT NULL,
        net_margin      REAL NOT NULL,
        components_json TEXT,
        computed_at     TEXT NOT NULL,
        PRIMARY KEY (sku, date, window_days)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sku_nm_date ON sku_net_margin_daily(date)",
    "CREATE INDEX IF NOT EXISTS idx_sku_nm_sku  ON sku_net_margin_daily(sku, date)",
    # Пункт 5 plans/2026-05-31-point-5-knowledge-base.md (фаза 1).
    # База знаний по рекламе — отдельная вкладка в разделе Реклама.
    # Статьи пишет продавец вручную или сам агент (еженедельный синтез).
    # Перед каждым решением агент находит топ-3 релевантных через FTS5.
    """CREATE TABLE IF NOT EXISTS ads_knowledge_articles (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        title           TEXT NOT NULL,
        body_md         TEXT NOT NULL,
        category        TEXT NOT NULL DEFAULT 'instruction',
        tags            TEXT,
        importance      INTEGER NOT NULL DEFAULT 3,
        source          TEXT NOT NULL DEFAULT 'user',
        created_by      TEXT,
        pending_review  INTEGER NOT NULL DEFAULT 0,
        archived        INTEGER NOT NULL DEFAULT 0,
        valid_until     TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ads_knowledge_category ON ads_knowledge_articles(category, archived)",
    "CREATE INDEX IF NOT EXISTS idx_ads_knowledge_pending  ON ads_knowledge_articles(pending_review, archived)",
    "CREATE INDEX IF NOT EXISTS idx_ads_knowledge_archived ON ads_knowledge_articles(archived, updated_at)",
    # FTS5-индекс для полнотекстового поиска (русский + английский). Без external content —
    # таблица хранит копии нужных полей; триггеры держат её в синхроне с основной.
    # tokenize unicode61 — нормально работает с кириллицей и латиницей.
    """CREATE VIRTUAL TABLE IF NOT EXISTS ads_knowledge_articles_fts USING fts5(
        title, body_md, tags,
        content='ads_knowledge_articles',
        content_rowid='id',
        tokenize='unicode61'
    )""",
    # Триггеры синхронизации (стандартный паттерн external-content FTS5).
    """CREATE TRIGGER IF NOT EXISTS ads_knowledge_articles_ai
        AFTER INSERT ON ads_knowledge_articles BEGIN
        INSERT INTO ads_knowledge_articles_fts(rowid, title, body_md, tags)
        VALUES (new.id, new.title, new.body_md, COALESCE(new.tags, ''));
    END""",
    """CREATE TRIGGER IF NOT EXISTS ads_knowledge_articles_ad
        AFTER DELETE ON ads_knowledge_articles BEGIN
        INSERT INTO ads_knowledge_articles_fts(ads_knowledge_articles_fts, rowid, title, body_md, tags)
        VALUES ('delete', old.id, old.title, old.body_md, COALESCE(old.tags, ''));
    END""",
    """CREATE TRIGGER IF NOT EXISTS ads_knowledge_articles_au
        AFTER UPDATE ON ads_knowledge_articles BEGIN
        INSERT INTO ads_knowledge_articles_fts(ads_knowledge_articles_fts, rowid, title, body_md, tags)
        VALUES ('delete', old.id, old.title, old.body_md, COALESCE(old.tags, ''));
        INSERT INTO ads_knowledge_articles_fts(rowid, title, body_md, tags)
        VALUES (new.id, new.title, new.body_md, COALESCE(new.tags, ''));
    END""",
    # ads_ai_log: какие статьи знания агент использовал при принятии решения.
    # JSON-массив id (NULL по умолчанию для записей до фазы 3).
    "ALTER TABLE ads_ai_log ADD COLUMN knowledge_article_ids TEXT",
    # Пункт 6 plans/2026-06-01-point-6-model-routing.md (фаза 1).
    # Какая фактическая LLM-модель обслужила запрос (например 'claude-sonnet-4-6'
    # или 'qwen3:14b'). Суффикс '@fallback' — если основной маршрут упал и
    # сработал запасной вариант. NULL для записей до маршрутизации.
    "ALTER TABLE ads_ai_log ADD COLUMN model_used TEXT",
    "CREATE INDEX IF NOT EXISTS idx_ads_ai_log_model ON ads_ai_log(model_used, created_at)",
    # Пункт 7 plans/2026-06-01-point-7-agent-health.md (фаза 1).
    # forecast_success: оценка агента (0..1) — насколько он сам уверен, что
    # главная метрика улучшится. Рефлексия сверяет с фактом → калибровка.
    # NULL для записей, где агент не указал прогноз (старые / не-агентские).
    "ALTER TABLE ads_ai_log ADD COLUMN forecast_success REAL",
    # usefulness: оценка пользователя на сообщение агента в чате (👍=1 / 👎=0).
    # NULL — нет оценки. Считается в температуре «полезный чат».
    "ALTER TABLE agent_chat ADD COLUMN usefulness INTEGER",
    # Пункт 4 plans/2026-06-01-point-4-error-bank.md (фаза 1).
    # Банк ошибок — «обжог пальцы — записал, второй раз не сую».
    # Каждая запись — отпечаток (тип кампании × ведро ДРР × действие),
    # счётчик провалов и счётчик блокировок. Если провалов больше порога —
    # перед повтором того же действия в той же ситуации сработает блок.
    # `fingerprint_human` — рус-формулировка для UI (без md5 в видимых текстах).
    # `affected_campaigns` — JSON-список id кампаний, попавших в отпечаток.
    # `paused_until` — мягкая пауза «второго шанса», `archived` — мягкое удаление по TTL.
    """CREATE TABLE IF NOT EXISTS ads_error_bank (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        fingerprint         TEXT NOT NULL UNIQUE,
        fingerprint_human   TEXT NOT NULL,
        campaign_type       TEXT NOT NULL,
        drr_bucket          TEXT NOT NULL,
        action              TEXT NOT NULL,
        fail_count          INTEGER NOT NULL DEFAULT 0,
        block_count         INTEGER NOT NULL DEFAULT 0,
        last_fail_at        TEXT,
        last_block_at       TEXT,
        paused_until        TEXT,
        archived            INTEGER NOT NULL DEFAULT 0,
        affected_campaigns  TEXT,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_error_bank_fingerprint ON ads_error_bank(fingerprint)",
    "CREATE INDEX IF NOT EXISTS idx_error_bank_archived_updated ON ads_error_bank(archived, updated_at)",
    # Пункт 4 Ф2 plans/2026-06-01-point-4-error-bank.md: маркер «эскалация банка
    # подтвердила то же действие, которое банк хотел запретить». applied=1,
    # skip_reason=NULL, escalation_overrode_block=1 — для отчётности.
    "ALTER TABLE ads_ai_log ADD COLUMN escalation_overrode_block INTEGER DEFAULT 0",
    # Пункт 2 Ф1 plans/2026-06-02-point-2-rules-portfolio.md.
    # Портфель правил с репутацией: для каждого rule_id из RULES (и арма
    # claude_choice) копится статистика helped/harmed/neutral и параметры
    # Beta-распределения alpha/beta. Выбор правила в recommender.run_for_campaign
    # идёт через Thompson Sampling (см. modules/ads/rule_portfolio.py:select_rule).
    # archived=1 — мягкое удаление, enabled=0 — пользователь временно выключил.
    # Системные правила (core=True в RULES) защищены от disable/archive на уровне API.
    """CREATE TABLE IF NOT EXISTS ads_rule_stats (
        rule_id       TEXT PRIMARY KEY,
        helped        INTEGER NOT NULL DEFAULT 0,
        harmed        INTEGER NOT NULL DEFAULT 0,
        neutral       INTEGER NOT NULL DEFAULT 0,
        alpha         REAL NOT NULL DEFAULT 1.0,
        beta          REAL NOT NULL DEFAULT 1.0,
        enabled       INTEGER NOT NULL DEFAULT 1,
        archived      INTEGER NOT NULL DEFAULT 0,
        last_updated  TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_rule_stats_enabled_archived ON ads_rule_stats(enabled, archived)",
    "CREATE INDEX IF NOT EXISTS idx_rule_stats_alpha ON ads_rule_stats(alpha DESC)",
    # Пункт 2 Ф2 plans/2026-06-02-point-2-rules-portfolio.md.
    # Идемпотентный маркер: «outcome этого решения уже учтён в ads_rule_stats».
    # Когда reflection.measure_outcomes считает outcome для решения с rule_id —
    # вызывает rule_portfolio.record_outcome(...) и ставит флаг в 1.
    # Защита от двойного учёта при повторном запуске рефлексии (cron + кнопка).
    "ALTER TABLE ads_ai_log ADD COLUMN rule_stats_recorded INTEGER DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_ads_ai_log_rule_stats_recorded ON ads_ai_log(rule_stats_recorded, rule_id)",
    # Пункт 8 Ф1 plans/2026-06-02-point-8-exploration.md (разведка агента).
    # is_exploration=1 — решение принято в режиме эксперимента: действие
    # перевёрнуто относительно «обычного» (см. exploration.flip_action).
    # exploration_original_action — какое действие хотело правило ИЗНАЧАЛЬНО
    # (для отчётности, бейджа «🔬 Эксперимент» в журнале и счётчиков).
    # NULL/0 для обычных решений и для всех записей до миграции.
    "ALTER TABLE ads_ai_log ADD COLUMN is_exploration INTEGER DEFAULT 0",
    "ALTER TABLE ads_ai_log ADD COLUMN exploration_original_action TEXT",
    "CREATE INDEX IF NOT EXISTS idx_ads_ai_log_exploration ON ads_ai_log(is_exploration, created_at)",
    # Пункт 8 Ф2 plans/2026-06-02-point-8-exploration.md (идемпотентность).
    # ID карточки product_hypotheses kind='agent_experiment', созданной из
    # удачного эксперимента. NULL пока гипотеза не создана. После записи —
    # повторная рефлексия не создаёт дубликат гипотезы.
    "ALTER TABLE ads_ai_log ADD COLUMN experiment_hypothesis_id INTEGER",
    # Пункт 9 Ф1 plans/2026-06-02-point-9-rollback-snapshots.md (откат и снапшоты).
    # Каждое применённое решение агента (bid_increase/bid_decrease/pause/activate)
    # создаёт snapshot со «снимком состояния до». Через rollback_window_hours
    # (по умолчанию 48ч) ночной cron проверяет: просела ли net_margin > порога →
    # автоматический откат к prev-состоянию через change_bid/activate_campaign.
    # rollback_status: 'pending' — ждём окно; 'succeeded' — окно прошло, метрика ОК;
    # 'rolled_back' — откат применён; 'rollback_failed' — Ozon API упал;
    # 'skip' — не подлежит авто-откату (is_exploration=1, zero_stock-pause).
    # state_before / state_after — JSON {bid_rub, campaign_state, ...}.
    """CREATE TABLE IF NOT EXISTS ads_action_snapshots (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        ai_log_id         INTEGER NOT NULL,
        campaign_id       TEXT NOT NULL,
        sku               TEXT,
        action            TEXT NOT NULL,
        state_before      TEXT NOT NULL,
        state_after       TEXT,
        applied_at        TEXT NOT NULL,
        rollback_status   TEXT NOT NULL DEFAULT 'pending',
        rolled_back_at    TEXT,
        rollback_reason   TEXT,
        notified          INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_status_applied ON ads_action_snapshots(rollback_status, applied_at)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_log ON ads_action_snapshots(ai_log_id)",
]


def get_connection(company_id: str | None = None):
    import os

    if os.getenv("DATABASE_URL"):
        cid = company_id
        if not cid:
            from shared.db_pool import get_current_company_id

            cid = get_current_company_id()
        if cid:
            from shared.db_pool import get_company_db

            return get_company_db(cid)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def init_db(conn) -> None:
    conn.executescript(SCHEMA)
    _run_migrations(conn)
    conn.commit()


def _run_migrations(conn) -> None:
    """Apply ALTER TABLE migrations safely (ignore 'duplicate column' errors)."""
    for sql in _MIGRATIONS:
        if hasattr(conn, "execute_ddl"):
            conn.execute_ddl(sql)
        else:
            try:
                conn.execute(sql)
            except Exception:
                pass  # column already exists
    conn.commit()


# Daily tables that participate in rolling-window retention
_RETENTION_TABLES = [
    "sku_analytics_daily",
    "sku_stocks_daily",
    "sku_stocks_by_warehouse",
    "sku_prices_daily",
    "sku_spp_daily",
    "campaign_stats_daily",
    "sku_search_position_daily",
    "sku_search_queries",
]


def cleanup_old_data(conn: sqlite3.Connection, retention_days: int = 90) -> None:
    """Delete rows older than retention_days from all daily tables.

    Keeps the most recent retention_days of data; older rows are removed.
    Runs VACUUM afterwards to reclaim disk space.
    """
    cutoff = f"date('now', '-{retention_days} days')"
    total_deleted = 0
    with conn:
        for table in _RETENTION_TABLES:
            cur = conn.execute(f"DELETE FROM {table} WHERE date < {cutoff}")
            if cur.rowcount:
                logger.info(
                    "[cleanup] %s: deleted %d rows older than %d days",
                    table,
                    cur.rowcount,
                    retention_days,
                )
                total_deleted += cur.rowcount
    if total_deleted:
        conn.execute("VACUUM")
        logger.info("[cleanup] Total deleted: %d rows, VACUUM done", total_deleted)
    else:
        logger.info("[cleanup] Nothing to delete (all data within %d days)", retention_days)
