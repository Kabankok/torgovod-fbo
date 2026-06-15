"""OAuth2 token management for Ozon Performance API.

Token lives ~1 hour. Auto-refreshes 5 minutes before expiry.
Cache is per-company: data/token_cache_{company_id}.json
Credentials are loaded from DB (company_credentials) with fallback to .env.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

_BASE = "https://api-performance.ozon.ru"
_MARGIN = 300  # refresh 5 min before expiry


def _cache_path(company_id: str | None) -> Path:
    """Per-company token cache file."""
    if company_id:
        return Path(f"data/token_cache_{company_id}.json")
    # fallback: legacy single-file (used when company_id not available)
    return Path(os.getenv("PERFORMANCE_TOKEN_CACHE", "data/token_cache.json"))


def _load(company_id: str | None) -> dict | None:
    path = _cache_path(company_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(data: dict, company_id: str | None) -> None:
    path = _cache_path(company_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_perf_credential(company_id: str | None, key_name: str) -> str | None:
    """Try DB first, fall back to .env."""
    if company_id:
        try:
            from shared.auth.credentials import get_credential

            val = get_credential(company_id, "ozon_performance", key_name)
            if val:
                return val
        except Exception:
            pass
    env_map = {
        "client_id": "OZON_PERFORMANCE_CLIENT_ID",
        "client_secret": "OZON_PERFORMANCE_CLIENT_SECRET",
    }
    return os.getenv(env_map.get(key_name, ""))


def _fetch(company_id: str | None = None) -> dict:
    client_id = _get_perf_credential(company_id, "client_id")
    secret = _get_perf_credential(company_id, "client_secret")
    if not client_id or not secret:
        raise ValueError(
            "OZON_PERFORMANCE_CLIENT_ID / OZON_PERFORMANCE_CLIENT_SECRET not set in .env or DB"
        )
    resp = requests.post(
        f"{_BASE}/api/client/token",
        json={
            "client_id": client_id,
            "client_secret": secret,
            "grant_type": "client_credentials",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token") or data.get("token")
    if not token:
        raise RuntimeError(f"No access_token in response: {data}")
    result = {
        "access_token": token,
        "expires_at": time.time() + data.get("expires_in", 3600),
    }
    _save(result, company_id)
    return result


def get_token(company_id: str | None = None) -> str:
    cached = _load(company_id)
    if cached and (cached.get("expires_at", 0) - time.time()) > _MARGIN:
        return cached["access_token"]
    return _fetch(company_id)["access_token"]


def perf_headers(company_id: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_token(company_id)}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
