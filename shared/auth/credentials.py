"""Encrypted credential storage: save / retrieve / mask API keys per company.

Keys are never returned in plaintext from get_credentials_masked().
get_credential() is intentionally NOT exposed to HTTP GET handlers.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shared.auth.models import _connect
from shared.crypto import decrypt, encrypt, mask

# Supported services and their expected key_names
CREDENTIAL_TYPES: dict[str, list[str]] = {
    "ozon_seller": ["client_id", "api_key"],
    "ozon_performance": ["client_id", "client_secret"],
    "wb": ["wb_api_key"],
    "claude": ["api_key"],
    "telegram": ["bot_token"],
}

SERVICE_LABELS: dict[str, str] = {
    "ozon_seller": "Ozon Seller API",
    "ozon_performance": "Ozon Performance API",
    "wb": "Wildberries Seller API",
    "claude": "Claude API",
    "telegram": "Telegram Bot",
}


def save_credential(company_id: str, service: str, key_name: str, value: str) -> None:
    enc = encrypt(company_id, value)
    now = datetime.now(UTC).isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO company_credentials
               (company_id, service, key_name, enc_value, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (company_id, service, key_name, enc, now),
        )
        conn.commit()


def get_credential(company_id: str, service: str, key_name: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT enc_value FROM company_credentials WHERE company_id=? AND service=? AND key_name=?",
            (company_id, service, key_name),
        ).fetchone()
    if not row:
        return None
    try:
        return decrypt(company_id, row[0])
    except Exception:
        return None


def get_credentials_masked(company_id: str) -> dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT service, key_name, enc_value FROM company_credentials WHERE company_id=?",
            (company_id,),
        ).fetchall()

    stored: dict[str, dict[str, str]] = {}
    for service, key_name, enc_value in rows:
        stored.setdefault(service, {})
        try:
            plaintext = decrypt(company_id, enc_value)
            stored[service][key_name] = mask(plaintext)
        except Exception:
            stored[service][key_name] = chr(0x2022) * 8 + "?????"

    result = {}
    for service, keys in CREDENTIAL_TYPES.items():
        result[service] = {
            "label": SERVICE_LABELS.get(service, service),
            "keys": {key_name: stored.get(service, {}).get(key_name, "") for key_name in keys},
            "configured": all(bool(stored.get(service, {}).get(k)) for k in keys),
        }
    return result
