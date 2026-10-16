"""get_setting() must survive app_settings' two incompatible writers.

set_setting() stores a raw string; save_app_settings() json.dumps() everything.
A bulk rewrite through the latter on 2026-09-06 left admin_username as
'"albert"', and /api/auth/login compares it verbatim — so password login
rejected the correct credentials until this was fixed.
"""

import json

import pytest

from backend import database


@pytest.fixture(autouse=True)
def _db(tmp_path):
    original_path, original_dir = database.DB_PATH, database.DB_DIR
    database.DB_PATH = str(tmp_path / "settings.db")
    database.DB_DIR = str(tmp_path)
    database.init_db()
    yield
    database.DB_PATH, database.DB_DIR = original_path, original_dir


def test_plain_value_round_trips_unchanged():
    database.set_setting("admin_username", "albert")
    assert database.get_setting("admin_username") == "albert"


def test_json_encoded_string_is_decoded_back():
    # Exactly what save_app_settings leaves behind.
    database.save_app_settings({"admin_username": "albert"})
    assert database.get_setting("admin_username") == "albert"


def test_a_hash_written_raw_then_bulk_rewritten_still_matches():
    from backend.auth import hash_password, verify_password

    hashed, salt = hash_password("correct horse battery staple")
    database.set_setting("admin_password_hash", hashed)
    database.set_setting("admin_password_salt", salt)
    # The 2026-09-06 event: every row rewritten json-encoded.
    database.save_app_settings({"admin_password_hash": hashed, "admin_password_salt": salt})

    assert verify_password(
        "correct horse battery staple",
        database.get_setting("admin_password_hash"),
        database.get_setting("admin_password_salt"),
    )


def test_a_json_document_is_left_as_text_for_its_own_parser():
    # permissions.py and content_factory store JSON through set_setting and
    # json.loads it themselves — they must keep receiving a string.
    document = json.dumps({"roles": ["owner", "guest"]})
    database.set_setting("custom_roles", document)
    raw = database.get_setting("custom_roles")
    assert isinstance(raw, str)
    assert json.loads(raw) == {"roles": ["owner", "guest"]}


def test_a_json_list_is_also_left_as_text():
    database.set_setting("some_list", json.dumps(["a", "b"]))
    assert json.loads(database.get_setting("some_list")) == ["a", "b"]


def test_a_value_that_merely_starts_with_a_quote_is_not_mangled():
    database.set_setting("quote_ish", '"unterminated')
    assert database.get_setting("quote_ish") == '"unterminated'


def test_missing_key_is_none():
    assert database.get_setting("never_set") is None
