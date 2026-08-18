import pytest
from cryptography.fernet import Fernet

from backend import local_crypto


@pytest.fixture(autouse=True)
def fixed_key(monkeypatch):
    """Every test gets a stable key via the env-var path so no Valkey call is needed."""
    monkeypatch.setenv("MEMORY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(local_crypto, "_fernet", None)
    yield
    monkeypatch.setattr(local_crypto, "_fernet", None)


def test_round_trip():
    original = "Albert's OpenRouter key is sk-or-abc123, don't leak it."
    encrypted = local_crypto.encrypt_text(original)
    assert encrypted != original
    assert encrypted.startswith("enc:v1:")
    assert local_crypto.decrypt_text(encrypted) == original


def test_empty_and_none_pass_through():
    assert local_crypto.encrypt_text("") == ""
    assert local_crypto.encrypt_text(None) is None
    assert local_crypto.decrypt_text("") == ""
    assert local_crypto.decrypt_text(None) is None


def test_legacy_plaintext_passes_through_unchanged():
    """Rows written before this module existed have no enc:v1: prefix."""
    assert local_crypto.decrypt_text("some old plaintext memory value") == "some old plaintext memory value"


def test_different_keys_cannot_decrypt_each_other(monkeypatch):
    encrypted = local_crypto.encrypt_text("secret")
    monkeypatch.setenv("MEMORY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(local_crypto, "_fernet", None)
    # Decryption failure degrades to returning the ciphertext, never raises.
    assert local_crypto.decrypt_text(encrypted) == encrypted


def test_auto_generates_and_persists_key_via_valkey(monkeypatch):
    monkeypatch.delenv("MEMORY_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(local_crypto, "_fernet", None)
    store: dict[str, str] = {}
    monkeypatch.setattr("backend.valkey_client.get_value", lambda key: store.get(key))
    monkeypatch.setattr(
        "backend.valkey_client.set_value",
        lambda key, value, ttl_seconds=None: store.__setitem__(key, value),
    )

    encrypted = local_crypto.encrypt_text("hello")
    assert local_crypto._VALKEY_KEY in store

    # A second module "instance" (fresh _fernet) reusing the same Valkey store
    # must derive the same key and decrypt what the first one wrote.
    monkeypatch.setattr(local_crypto, "_fernet", None)
    assert local_crypto.decrypt_text(encrypted) == "hello"
