"""Per-tenant encryption: HKDF(SHA256) + Fernet.

Master key is loaded once from FERNET_MASTER_KEY env var.
Each company gets its own derived key so a leaked DB alone is insufficient.
"""

from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def _master_key() -> bytes:
    raw = os.environ.get("FERNET_MASTER_KEY", "")
    if not raw:
        raise RuntimeError("FERNET_MASTER_KEY not set in environment")
    return base64.urlsafe_b64decode(raw)


def derive_company_key(company_id: str) -> bytes:
    """Derive a 32-byte key for a specific company via HKDF."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"company_{company_id}".encode(),
    )
    return hkdf.derive(_master_key())


def _fernet(company_id: str) -> Fernet:
    derived = derive_company_key(company_id)
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(company_id: str, plaintext: str) -> str:
    return _fernet(company_id).encrypt(plaintext.encode()).decode()


def decrypt(company_id: str, ciphertext: str) -> str:
    return _fernet(company_id).decrypt(ciphertext.encode()).decode()


def mask(plaintext: str) -> str:
    """Return ••••••••XXXX where XXXX is the last 4 chars."""
    if not plaintext:
        return ""
    suffix = plaintext[-4:] if len(plaintext) >= 4 else plaintext
    return "••••••••" + suffix
