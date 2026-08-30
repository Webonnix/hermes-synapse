"""Account connection, proxy secrecy and analytics-facing channel metadata."""

import json

import pytest

from backend import database
from backend.content_factory import content, schema
from backend.content_factory.domain import ContentFactoryError


@pytest.fixture()
def channel_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "channels.db"))
    database._set_backend_for_tests(database.SQLiteBackend())
    schema.ensure_schema(force=True)
    secrets = {}
    monkeypatch.setattr("backend.valkey_client.set_value", lambda key, value, ttl_seconds=None: secrets.__setitem__(key, value))
    monkeypatch.setattr("backend.valkey_client.get_value", lambda key: secrets.get(key))
    yield secrets
    database._set_backend_for_tests(None)


def test_proxy_credentials_only_live_in_secret_store(channel_db):
    channel = content.create_channel(
        platform="youtube", account_name="Daily News",
        credentials={"refresh_token": "oauth-secret"},
        proxy_url="http://proxy-user:proxy-pass@proxy.example:8080",
    )
    public = content.list_channels()[0]

    assert channel["status"] == "CONNECTED"
    assert public["proxy_host"] == "proxy.example"
    assert public["proxy_enabled"] == 1
    assert "credentials_ref" not in public
    assert "proxy-pass" not in json.dumps(public)
    assert "proxy-pass" in next(iter(channel_db.values()))


def test_proxy_can_be_rotated_or_disabled(channel_db):
    channel = content.create_channel(platform="tiktok", account_name="Clips")
    enabled = content.update_channel_connection(
        channel["id"], credentials={"refresh_token": "token"},
        proxy_url="https://new.proxy.example:9443",
    )
    disabled = content.update_channel_connection(channel["id"], proxy_url="")

    assert enabled["status"] == "CONNECTED"
    assert enabled["proxy_host"] == "new.proxy.example"
    assert disabled["proxy_enabled"] == 0
    assert "proxy_url" not in next(iter(channel_db.values()))


def test_unsupported_proxy_scheme_is_rejected(channel_db):
    with pytest.raises(ContentFactoryError):
        content.create_channel(platform="youtube", account_name="News", proxy_url="socks5://host:1080")
