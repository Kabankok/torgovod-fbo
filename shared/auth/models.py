"""Users and sessions storage.

DB: data/users.db (override with USERS_DB_PATH env var).
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from passlib.context import CryptContext

USERS_DB_PATH = Path(os.getenv("USERS_DB_PATH", "data/users.db"))

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

SESSION_TTL_DAYS = 30

# Все доступные модули системы.
# Порядок и названия совпадают с левой колонкой dashboard/templates/base.html,
# чтобы в модалке «Права» сотрудник админа видел те же названия, что в сайдбаре.
ALL_MODULES: list[tuple[str, str]] = [
    # Обзор
    ("dashboard", "Дашборд"),
    # Маркетплейс
    ("products", "Товары"),
    ("hypotheses", "Гипотезы"),
    ("ads", "Реклама"),
    ("promotions", "Акции"),
    ("seo", "SEO"),
    ("product_cards", "Карточки товара"),
    # Склад
    ("inventory", "Остатки и инвентаризация"),
    ("fbo", "Отгрузка FBO"),
    ("fbs", "Отгрузки FBS"),
    ("placement", "Платное хранение"),
    ("sync_products", "Зеркало остатков"),
    # Документы
    ("documents", "Входящие документы (УПД, банк)"),
    ("invoices", "Счета на оплату"),
    # Финансы
    ("finance", "Табель платежей"),
    ("unit_economics", "Юнит-экономика"),
    ("costs", "Себестоимость"),
    ("ozon_expenses", "Финансы Ozon"),
    ("cashflow", "ДДС"),
    ("taxes", "Налоги"),
    ("pnl", "P&L"),
    # Автоматизация
    ("tasks", "Задачи"),
    # Контент
    ("tg_channel", "Telegram"),
    ("instagram", "Instagram"),
    ("nodes", "Ноды"),
    # Коммуникации
    ("email", "Почта"),
    ("calendar", "Календарь"),
    # Система
    ("settings", "Настройки"),
    ("team", "Команда"),
    ("personal", "🔒 Личное"),
]

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS companies (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    plan       TEXT DEFAULT 'trial',
    created_at TEXT NOT NULL,
    owner_id   TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'member',
    is_admin      INTEGER NOT NULL DEFAULT 0,
    permissions   TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    company_id    TEXT,
    yandex_id     TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token                   TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    expires_at              TEXT NOT NULL,
    company_id              TEXT,
    impersonated_by_user_id TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS company_settings (
    company_id TEXT NOT NULL DEFAULT '',
    key        TEXT NOT NULL,
    value      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (company_id, key)
);

CREATE TABLE IF NOT EXISTS company_credentials (
    company_id  TEXT NOT NULL,
    service     TEXT NOT NULL,
    key_name    TEXT NOT NULL,
    enc_value   TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (company_id, service, key_name)
);

CREATE TABLE IF NOT EXISTS company_modules (
    company_id  TEXT NOT NULL,
    module_key  TEXT NOT NULL,
    expires_at  TEXT,
    granted_by  TEXT NOT NULL,
    granted_at  TEXT NOT NULL,
    PRIMARY KEY (company_id, module_key)
);

CREATE TABLE IF NOT EXISTS invitations (
    token            TEXT PRIMARY KEY,
    company_id       TEXT NOT NULL,
    invited_by       TEXT NOT NULL,
    permissions      TEXT DEFAULT '[]',
    invited_role     TEXT DEFAULT 'member',
    expires_at       TEXT NOT NULL,
    used_at          TEXT,
    used_by_user_id  TEXT,
    status           TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS platform_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    value_type  TEXT DEFAULT 'string',
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_flags (
    flag_name           TEXT PRIMARY KEY,
    is_enabled_global   INTEGER DEFAULT 0,
    override_companies  TEXT DEFAULT '{}',
    updated_at          TEXT NOT NULL,
    updated_by          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_audit_log (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    action        TEXT NOT NULL,
    resource_type TEXT,
    resource_id   TEXT,
    old_value     TEXT,
    new_value     TEXT,
    ip_address    TEXT,
    timestamp     TEXT NOT NULL
);
"""

# ── Tax regimes ────────────────────────────────────────────────────────────────

TAX_REGIMES: dict[str, dict] = {
    "osno": {"label": "ОСНО", "is_vat_payer": True, "default_vat_rate": 22},
    "usn_income": {"label": "УСН «Доходы»", "is_vat_payer": False, "default_vat_rate": 0},
    "usn_income_expense": {
        "label": "УСН «Доходы − Расходы»",
        "is_vat_payer": False,
        "default_vat_rate": 0,
    },
    "ausn": {"label": "АУСН", "is_vat_payer": False, "default_vat_rate": 0},
    "psn": {"label": "ПСН (Патент)", "is_vat_payer": False, "default_vat_rate": 0},
    "npd": {"label": "НПД (Самозанятый)", "is_vat_payer": False, "default_vat_rate": 0},
    "eshn": {"label": "ЕСХН", "is_vat_payer": False, "default_vat_rate": 0},
}


# НДС-обязательства при УСН зависят от годового оборота (ФЗ №425-ФЗ от 28.11.2025, с 01.01.2026)
def _usn_vat_rate(annual_revenue: int) -> int:
    if annual_revenue >= 490_500_000:
        return 20  # должны перейти на ОСНО
    if annual_revenue >= 272_500_000:
        return 7
    if annual_revenue >= 20_000_000:
        return 5
    return 0


def get_company_settings(company_id: str | None = None) -> dict:
    with _connect() as conn:
        if company_id:
            rows = conn.execute(
                "SELECT key, value FROM company_settings WHERE company_id = ?", (company_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT key, value FROM company_settings").fetchall()
    return {r[0]: r[1] for r in rows}


def set_company_settings(data: dict, company_id: str | None = None) -> None:
    with _connect() as conn:
        for k, v in data.items():
            conn.execute(
                "INSERT OR REPLACE INTO company_settings (key, value, company_id) VALUES (?, ?, ?)",
                (str(k), str(v) if v is not None else "", company_id),
            )
        conn.commit()


def get_effective_company_settings(company_id: str | None = None) -> dict:
    """Return company settings with all derived values resolved."""
    raw = get_company_settings(company_id)
    regime_key = raw.get("tax_regime", "usn_income")
    regime_info = TAX_REGIMES.get(regime_key, TAX_REGIMES["usn_income"])

    is_usn = regime_key in ("usn_income", "usn_income_expense")
    try:
        annual_revenue = int(raw.get("annual_revenue_approx") or 0)
    except (ValueError, TypeError):
        annual_revenue = 0

    usn_auto_vat = _usn_vat_rate(annual_revenue) if is_usn else None

    override = raw.get("is_vat_payer_override")
    if override in ("true", "false"):
        is_vat_payer = override == "true"
    elif is_usn:
        is_vat_payer = (usn_auto_vat or 0) > 0
    else:
        is_vat_payer = regime_info["is_vat_payer"]

    try:
        vat_rate_out = int(raw.get("vat_rate_outgoing") or regime_info["default_vat_rate"])
    except (ValueError, TypeError):
        vat_rate_out = regime_info["default_vat_rate"]

    try:
        default_purchase_vat = int(raw.get("default_purchase_vat_rate") or 22)
    except (ValueError, TypeError):
        default_purchase_vat = 22

    return {
        "company_name": raw.get("company_name", ""),
        "legal_form": raw.get("legal_form", "ip"),
        "entity_type": raw.get("entity_type", raw.get("legal_form", "ip")),
        "tax_regime": regime_key,
        "tax_regime_label": regime_info["label"],
        "annual_revenue_approx": annual_revenue,
        "ausn_subtype": raw.get("ausn_subtype", "income"),
        "has_employees": raw.get("has_employees", "0") == "1",
        "tax_wizard_completed_at": raw.get("tax_wizard_completed_at", ""),
        "is_vat_payer": is_vat_payer,
        "usn_auto_vat_rate": usn_auto_vat,
        "vat_rate_outgoing": vat_rate_out,
        "default_purchase_vat_rate": default_purchase_vat,
        "cost_basis": "ex_vat" if is_vat_payer else "gross_with_vat",
    }


def _migrate(conn: sqlite3.Connection) -> None:
    existing_users = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "is_admin" not in existing_users:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET is_admin=1 WHERE role='admin'")
    if "permissions" not in existing_users:
        conn.execute("ALTER TABLE users ADD COLUMN permissions TEXT NOT NULL DEFAULT '[]'")
    if "yandex_id" not in existing_users:
        conn.execute("ALTER TABLE users ADD COLUMN yandex_id TEXT")
    if "company_id" not in existing_users:
        conn.execute("ALTER TABLE users ADD COLUMN company_id TEXT")

    existing_sessions = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "company_id" not in existing_sessions:
        conn.execute("ALTER TABLE sessions ADD COLUMN company_id TEXT")
    if "impersonated_by_user_id" not in existing_sessions:
        conn.execute("ALTER TABLE sessions ADD COLUMN impersonated_by_user_id TEXT")

    existing_cs = {r[1] for r in conn.execute("PRAGMA table_info(company_settings)").fetchall()}
    if "company_id" not in existing_cs:
        conn.execute("ALTER TABLE company_settings ADD COLUMN company_id TEXT")

    # Исправить PRIMARY KEY: пересоздать таблицу если PK только (key), не (company_id, key)
    cs_pk = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='company_settings'"
    ).fetchone()
    if cs_pk and "PRIMARY KEY (company_id, key)" not in cs_pk[0]:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_settings_new (
                company_id TEXT NOT NULL DEFAULT '',
                key        TEXT NOT NULL,
                value      TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (company_id, key)
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO company_settings_new (company_id, key, value)
            SELECT COALESCE(company_id, ''), key, value FROM company_settings
        """)
        conn.execute("DROP TABLE company_settings")
        conn.execute("ALTER TABLE company_settings_new RENAME TO company_settings")

    # Миграция ролей: role='user' — признак старой системы
    needs_role_migration = conn.execute("SELECT 1 FROM users WHERE role='user' LIMIT 1").fetchone()
    if needs_role_migration:
        conn.execute("UPDATE users SET role='platform_admin' WHERE is_admin=1")
        conn.execute("UPDATE users SET role='member' WHERE role='user'")

    # Создать компанию-заглушку и привязать всех существующих пользователей
    company_count = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    if company_count == 0:
        first_admin = conn.execute("SELECT id FROM users WHERE is_admin=1 LIMIT 1").fetchone()
        owner_id = first_admin[0] if first_admin else None
        company_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO companies (id, name, plan, created_at, owner_id) VALUES (?,?,?,?,?)",
            (company_id, "Моя компания", "trial", now, owner_id),
        )
        conn.execute("UPDATE users SET company_id=? WHERE company_id IS NULL", (company_id,))
        conn.execute(
            "UPDATE company_settings SET company_id=? WHERE company_id IS NULL", (company_id,)
        )
        try:
            from shared.db_pool import get_company_db as _get_company_db

            _cdb = _get_company_db(company_id)
            _cdb.close()
        except Exception:
            pass

    # Починить owner_id если пустой
    if conn.execute("SELECT 1 FROM companies WHERE owner_id IS NULL LIMIT 1").fetchone():
        conn.execute(
            """UPDATE companies SET owner_id=(SELECT id FROM users WHERE is_admin=1 LIMIT 1)
               WHERE owner_id IS NULL"""
        )

    # Обновить company_id в старых сессиях
    if conn.execute("SELECT 1 FROM sessions WHERE company_id IS NULL LIMIT 1").fetchone():
        conn.execute(
            """UPDATE sessions SET company_id=(
                   SELECT users.company_id FROM users WHERE users.id=sessions.user_id
               ) WHERE company_id IS NULL"""
        )

    # Каждой компании выдать все модули из ALL_MODULES, которых у неё ещё нет.
    all_companies = conn.execute("SELECT id, owner_id FROM companies").fetchall()
    if all_companies:
        now = datetime.now(UTC).isoformat()
        for company_row in all_companies:
            granted_by = company_row[1] or "system"
            for key, _ in ALL_MODULES:
                conn.execute(
                    "INSERT OR IGNORE INTO company_modules (company_id, module_key, expires_at, granted_by, granted_at)"
                    " VALUES (?,?,?,?,?)",
                    (company_row[0], key, None, granted_by, now),
                )

    # Для существующих компаний — закрыть налоговый wizard, чтобы он не всплыл
    # на следующем визите тем, кто уже на платформе. У новых компаний ключа нет —
    # они увидят модалку при первом открытии /taxes (фаза 5 разноса финмодулей).
    now_iso = datetime.now(UTC).isoformat()
    for company_row in all_companies:
        conn.execute(
            "INSERT OR IGNORE INTO company_settings (company_id, key, value) VALUES (?, ?, ?)",
            (company_row[0], "tax_wizard_completed_at", now_iso),
        )

    # Разнос finance → finance + unit_economics: кто имел доступ к Юнит-экономике
    # через finance, продолжает иметь его через новый ключ. Одноразовая операция,
    # помеченная флагом в company_settings (фирменный сторадж — у нас нет
    # platform_settings без updated_by).
    backfill_done = conn.execute(
        "SELECT 1 FROM company_settings WHERE company_id='' AND key='ue_perm_split_done' LIMIT 1"
    ).fetchone()
    if not backfill_done:
        user_rows = conn.execute("SELECT id, permissions FROM users").fetchall()
        for ur in user_rows:
            try:
                perms = json.loads(ur[1] or "[]")
            except Exception:
                perms = []
            if "finance" in perms and "unit_economics" not in perms:
                perms.append("unit_economics")
                conn.execute(
                    "UPDATE users SET permissions=? WHERE id=?",
                    (json.dumps(perms), ur[0]),
                )
        conn.execute(
            "INSERT OR IGNORE INTO company_settings (company_id, key, value) VALUES ('', 'ue_perm_split_done', ?)",
            (now_iso,),
        )

    # Разнос cards → nodes (Контент) + product_cards (Маркетплейс), 2026-06-04, ADR-020.
    # У всех компаний, имевших старый ключ "cards", выдаём оба новых; старый — удаляем.
    # У пользователей в `permissions` тоже мигрируем: cards → nodes + product_cards.
    # Одноразовая операция — помечена флагом cards_split_done в company_settings.
    cards_split_done = conn.execute(
        "SELECT 1 FROM company_settings WHERE company_id='' AND key='cards_split_done' LIMIT 1"
    ).fetchone()
    if not cards_split_done:
        # Берём список компаний, где был старый ключ.
        affected_companies = conn.execute(
            "SELECT DISTINCT company_id FROM company_modules WHERE module_key='cards'"
        ).fetchall()
        for crow in affected_companies:
            cid = crow[0]
            granted_by_row = conn.execute(
                "SELECT granted_by FROM company_modules"
                " WHERE company_id=? AND module_key='cards' LIMIT 1",
                (cid,),
            ).fetchone()
            granted_by = granted_by_row[0] if granted_by_row else "system"
            for new_key in ("nodes", "product_cards"):
                conn.execute(
                    "INSERT OR IGNORE INTO company_modules"
                    " (company_id, module_key, expires_at, granted_by, granted_at)"
                    " VALUES (?, ?, NULL, ?, ?)",
                    (cid, new_key, granted_by, now_iso),
                )
            conn.execute(
                "DELETE FROM company_modules WHERE company_id=? AND module_key='cards'",
                (cid,),
            )
        # Миграция permissions у пользователей.
        user_rows = conn.execute("SELECT id, permissions FROM users").fetchall()
        for ur in user_rows:
            try:
                perms = json.loads(ur[1] or "[]")
            except Exception:
                perms = []
            if "cards" in perms:
                perms = [p for p in perms if p != "cards"]
                if "nodes" not in perms:
                    perms.append("nodes")
                if "product_cards" not in perms:
                    perms.append("product_cards")
                conn.execute(
                    "UPDATE users SET permissions=? WHERE id=?",
                    (json.dumps(perms), ur[0]),
                )
        conn.execute(
            "INSERT OR IGNORE INTO company_settings (company_id, key, value)"
            " VALUES ('', 'cards_split_done', ?)",
            (now_iso,),
        )

    conn.commit()


def _connect() -> sqlite3.Connection:
    USERS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(USERS_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _row_to_user(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["permissions"] = json.loads(d.get("permissions") or "[]")
    except Exception:
        d["permissions"] = []
    d["is_admin"] = bool(d.get("is_admin", 0))
    d.setdefault("company_id", None)
    d.setdefault("role", "member")
    return d


# ── users ────────────────────────────────────────────────────────────────────


def get_all_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, username, role, is_admin, permissions, created_at, company_id FROM users ORDER BY created_at"
        ).fetchall()
    return [_row_to_user(r) for r in rows]


def get_user_by_username(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_id(user_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row else None


def create_user(
    username: str,
    password: str,
    is_admin: bool = False,
    permissions: list[str] | None = None,
) -> dict:
    user_id = str(uuid.uuid4())
    password_hash = _pwd.hash(password)
    now = datetime.now(UTC).isoformat()
    perms = json.dumps(permissions or [])
    role = "platform_admin" if is_admin else "member"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, is_admin, permissions, created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, username, password_hash, role, int(is_admin), perms, now),
        )
        conn.commit()
    return {
        "id": user_id,
        "username": username,
        "role": role,
        "is_admin": is_admin,
        "permissions": permissions or [],
        "created_at": now,
        "company_id": None,
    }


def update_user_permissions(user_id: str, permissions: list[str], is_admin: bool) -> None:
    role = "platform_admin" if is_admin else "member"
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET permissions=?, is_admin=?, role=? WHERE id=?",
            (json.dumps(permissions), int(is_admin), role, user_id),
        )
        conn.commit()


def update_user_password(user_id: str, new_password: str) -> None:
    password_hash = _pwd.hash(new_password)
    with _connect() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()


def update_username(user_id: str, new_username: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET username=? WHERE id=?", (new_username, user_id))
        conn.commit()


def delete_user(user_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()


def verify_password(username: str, password: str) -> dict | None:
    user = get_user_by_username(username)
    if not user:
        _pwd.dummy_verify()
        return None
    if not _pwd.verify(password, user["password_hash"]):
        return None
    return user


def count_users() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


# ── sessions ─────────────────────────────────────────────────────────────────


def create_session(user_id: str, company_id: str | None = None) -> str:
    token = str(uuid.uuid4())
    expires_at = (datetime.now(UTC) + timedelta(days=SESSION_TTL_DAYS)).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at, company_id) VALUES (?,?,?,?)",
            (token, user_id, expires_at, company_id),
        )
        conn.commit()
    return token


def create_impersonation_session(
    platform_admin_id: str,
    target_user_id: str,
    company_id: str,
) -> str:
    token = str(uuid.uuid4())
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at, company_id, impersonated_by_user_id)"
            " VALUES (?,?,?,?,?)",
            (token, target_user_id, expires_at, company_id, platform_admin_id),
        )
        conn.commit()
    return token


def get_session_user(token: str) -> dict | None:
    if not token:
        return None
    with _connect() as conn:
        row = conn.execute(
            """SELECT u.id, u.username, u.role, u.is_admin, u.permissions,
                      u.company_id, s.company_id AS session_company_id,
                      s.impersonated_by_user_id
               FROM sessions s JOIN users u ON s.user_id = u.id
               WHERE s.token=? AND s.expires_at > ?""",
            (token, datetime.now(UTC).isoformat()),
        ).fetchone()
    if not row:
        return None
    user = _row_to_user(row)
    if row["session_company_id"]:
        user["company_id"] = row["session_company_id"]
    user["impersonated_by_user_id"] = row["impersonated_by_user_id"]
    return user


def delete_session(token: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


def delete_user_sessions(user_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.commit()


def get_user_by_yandex_id(yandex_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE yandex_id = ?", (yandex_id,)).fetchone()
    return _row_to_user(row) if row else None


def link_yandex_id(user_id: str, yandex_id: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET yandex_id=? WHERE id=?", (yandex_id, user_id))
        conn.commit()


def create_user_from_yandex(yandex_id: str, username: str) -> dict:
    user_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, is_admin, permissions, created_at, yandex_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (user_id, username, "", "member", 0, "[]", now, yandex_id),
        )
        conn.commit()
    return {
        "id": user_id,
        "username": username,
        "role": "member",
        "is_admin": False,
        "permissions": [],
        "created_at": now,
        "company_id": None,
    }


def purge_expired_sessions() -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE expires_at <= ?",
            (datetime.now(UTC).isoformat(),),
        )
        conn.commit()


# ── company_modules ──────────────────────────────────────────────────────────


def get_company_modules(company_id: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT module_key FROM company_modules WHERE company_id=?"
            " AND (expires_at IS NULL OR expires_at > ?)",
            (company_id, datetime.now(UTC).isoformat()),
        ).fetchall()
    return [r[0] for r in rows]


def grant_module(
    company_id: str,
    module_key: str,
    granted_by: str,
    expires_at: str | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO company_modules"
            " (company_id, module_key, expires_at, granted_by, granted_at)"
            " VALUES (?,?,?,?,?)",
            (company_id, module_key, expires_at, granted_by, now),
        )
        conn.commit()


def revoke_module(company_id: str, module_key: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM company_modules WHERE company_id=? AND module_key=?",
            (company_id, module_key),
        )
        conn.commit()


def get_all_companies() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, plan, created_at, owner_id FROM companies ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_company_by_id(company_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, plan, created_at, owner_id FROM companies WHERE id=?",
            (company_id,),
        ).fetchone()
    return dict(row) if row else None


# ── invitations ───────────────────────────────────────────────────────────────


def create_invitation(
    company_id: str,
    invited_by: str,
    permissions: list[str],
    role: str = "member",
    expires_days: int = 30,
) -> str:
    token = str(uuid.uuid4())
    expires_at = (datetime.now(UTC) + timedelta(days=expires_days)).isoformat()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO invitations (token, company_id, invited_by, permissions, invited_role, expires_at, status)"
            " VALUES (?,?,?,?,?,?,?)",
            (token, company_id, invited_by, json.dumps(permissions), role, expires_at, "pending"),
        )
        conn.commit()
    return token


def get_invitation(token: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """SELECT i.token, i.company_id, i.invited_by, i.permissions, i.invited_role,
                      i.expires_at, i.used_at, i.used_by_user_id, i.status,
                      c.name AS company_name,
                      u.username AS inviter_username
               FROM invitations i
               JOIN companies c ON c.id = i.company_id
               LEFT JOIN users u ON u.id = i.invited_by
               WHERE i.token = ?""",
            (token,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["permissions"] = json.loads(d.get("permissions") or "[]")
    except Exception:
        d["permissions"] = []
    return d


def accept_invitation(token: str, username: str, password: str) -> str:
    inv = get_invitation(token)
    if not inv:
        raise ValueError("not_found")
    if inv["status"] == "revoked":
        raise ValueError("revoked")
    if inv["status"] == "accepted" or inv["used_at"]:
        raise ValueError("already_used")
    now_dt = datetime.now(UTC)
    if inv["expires_at"] < now_dt.isoformat():
        raise ValueError("expired")
    user_id = str(uuid.uuid4())
    password_hash = _pwd.hash(password)
    now = now_dt.isoformat()
    with _connect() as conn:
        existing = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            raise ValueError("username_taken")
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, is_admin, permissions, created_at, company_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                user_id,
                username,
                password_hash,
                inv["invited_role"],
                0,
                json.dumps(inv["permissions"]),
                now,
                inv["company_id"],
            ),
        )
        conn.execute(
            "UPDATE invitations SET status='accepted', used_at=?, used_by_user_id=? WHERE token=?",
            (now, user_id, token),
        )
        conn.commit()
    return user_id


def revoke_invitation(token: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE invitations SET status='revoked' WHERE token=? AND status='pending'",
            (token,),
        )
        conn.commit()


def get_company_members(company_id: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, username, role, is_admin, permissions, created_at, company_id"
            " FROM users WHERE company_id=? ORDER BY created_at",
            (company_id,),
        ).fetchall()
    return [_row_to_user(r) for r in rows]


def get_company_invitations(company_id: str) -> list[dict]:
    now_iso = datetime.now(UTC).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT i.token, i.invited_by, i.permissions, i.invited_role, i.expires_at,
                      i.used_at, i.status, u.username AS inviter_username
               FROM invitations i
               LEFT JOIN users u ON u.id = i.invited_by
               WHERE i.company_id=? AND i.status='pending' AND i.expires_at > ?
               ORDER BY i.expires_at DESC""",
            (company_id, now_iso),
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        try:
            d["permissions"] = json.loads(d.get("permissions") or "[]")
        except Exception:
            d["permissions"] = []
        result.append(d)
    return result


def update_member_permissions(user_id: str, company_id: str, permissions: list[str]) -> None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT role FROM users WHERE id=? AND company_id=?", (user_id, company_id)
        ).fetchone()
        if not row or row[0] == "platform_admin":
            return
        conn.execute(
            "UPDATE users SET permissions=? WHERE id=? AND company_id=?",
            (json.dumps(permissions), user_id, company_id),
        )
        conn.commit()


def remove_member(user_id: str, company_id: str) -> None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT role FROM users WHERE id=? AND company_id=?", (user_id, company_id)
        ).fetchone()
        if not row or row[0] in ("platform_admin", "owner"):
            return
        conn.execute("DELETE FROM users WHERE id=? AND company_id=?", (user_id, company_id))
        conn.commit()
