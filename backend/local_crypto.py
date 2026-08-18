"""At-rest encryption for Hermes's own persistent memory (user_memory, subagent_memory).

Nothing in the codebase encrypted anything before this — `cryptography` was only a
transitive dependency lock entry, never imported. This is the local model's own
memory staying local in a real sense: even a raw copy of the SQLite file doesn't
hand over the facts it has learned about the user.

Key resolution order:
1. `MEMORY_ENCRYPTION_KEY` env var (a urlsafe-base64 Fernet key) — set this in
   `.env` for a real deployment so the key survives a fresh container/volume and
   can be backed up/rotated deliberately.
2. Otherwise, a key is generated once and persisted permanently in Valkey
   (`memory_encryption_key`) so a fresh install still gets real encryption with
   zero configuration. Losing the Valkey volume without a backed-up env key means
   existing encrypted rows become unreadable — same tradeoff every local-secret
   design in this codebase already accepts for provider API keys.

Ciphertext is tagged (`enc:v1:...`) so rows written before this module existed
(plaintext) are still read correctly — `decrypt_text` returns untagged input
unchanged rather than failing.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("hermes.crypto")

_PREFIX = "enc:v1:"
_VALKEY_KEY = "memory_encryption_key"

_fernet = None  # type: ignore[var-annotated]


def _load_or_create_key() -> bytes:
    env_key = os.getenv("MEMORY_ENCRYPTION_KEY", "").strip()
    if env_key:
        return env_key.encode("utf-8")

    from backend.valkey_client import get_value, set_value
    from cryptography.fernet import Fernet

    stored = get_value(_VALKEY_KEY)
    if stored:
        return stored.encode("utf-8")

    generated = Fernet.generate_key()
    set_value(_VALKEY_KEY, generated.decode("utf-8"), ttl_seconds=None)
    logger.warning(
        "MEMORY_ENCRYPTION_KEY not set — generated a random key and persisted it "
        "in Valkey. Set MEMORY_ENCRYPTION_KEY in .env for a deployment you intend "
        "to back up or restore, otherwise losing the Valkey volume makes existing "
        "encrypted memory unreadable."
    )
    return generated


def _get_fernet():
    global _fernet
    if _fernet is None:
        from cryptography.fernet import Fernet

        _fernet = Fernet(_load_or_create_key())
    return _fernet


def encrypt_text(plaintext: Optional[str]) -> Optional[str]:
    """Encrypts a string for storage. None/empty pass through unchanged."""
    if not plaintext:
        return plaintext
    try:
        token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
        return _PREFIX + token
    except Exception:
        logger.exception("Encryption failed — storing value in plaintext as a last resort")
        return plaintext


def decrypt_text(value: Optional[str]) -> Optional[str]:
    """Decrypts a value previously produced by encrypt_text. Untagged (legacy
    plaintext, or empty) values pass through unchanged rather than raising."""
    if not value or not value.startswith(_PREFIX):
        return value
    try:
        token = value[len(_PREFIX):]
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        logger.error("Decryption failed for a stored memory value — returning as-is")
        return value
