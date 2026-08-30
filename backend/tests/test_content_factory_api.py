"""Content Factory HTTP surface (§74–76, §83).

What matters here is not that the handlers work — `test_content_factory_execution`
covers the behaviour — but that the *edges* hold: authentication is required,
permissions are enforced server-side rather than by a hidden button, errors come
back in the §76 envelope with a machine-readable code, and nothing that touches
a credential is ever serialized into a response.
"""

import pytest
from fastapi.testclient import TestClient

from backend import database, permissions
from backend.auth import active_sessions
from backend.content_factory import content as content_store
from backend.content_factory import schema, storage as storage_module
from backend.main import app


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "cf.db"))
    database._set_backend_for_tests(database.SQLiteBackend())
    database.init_db()
    schema.ensure_schema(force=True)
    storage_module.set_storage_for_tests(storage_module.LocalStorage(str(tmp_path / "assets")))

    client = TestClient(app)
    client.headers = {"Authorization": "Bearer cf-test-token"}
    active_sessions.add("cf-test-token")

    yield client

    active_sessions.discard("cf-test-token")
    storage_module.set_storage_for_tests(None)
    database._set_backend_for_tests(None)


def _as_role(monkeypatch, role: str) -> None:
    monkeypatch.setattr(permissions, "role_from_request", lambda request: role)


# ── Auth (§83) ───────────────────────────────────────────────────────────────

def test_endpoints_require_a_session():
    anonymous = TestClient(app)
    response = anonymous.get("/api/v1/content-factory/overview")
    assert response.status_code == 401


def test_viewer_cannot_edit_a_pipeline(api, monkeypatch):
    """A read-only role is refused server-side, not merely shown fewer buttons."""
    _as_role(monkeypatch, "viewer")
    response = api.post("/api/v1/content-factory/pipelines", json={"name": "X"})
    assert response.status_code == 403


def test_viewer_can_read(api, monkeypatch):
    _as_role(monkeypatch, "viewer")
    assert api.get("/api/v1/content-factory/pipelines").status_code == 200


def test_reviewer_may_approve_but_not_publish_config(api, monkeypatch):
    """§81: the REVIEWER rung approves content and does not manage channels."""
    _as_role(monkeypatch, "operator")
    assert api.get("/api/v1/content-factory/content").status_code == 200
    denied = api.post("/api/v1/content-factory/channels", json={"platform": "YOUTUBE", "accountName": "x"})
    assert denied.status_code == 403


# ── Pipelines ────────────────────────────────────────────────────────────────

def test_create_and_read_a_pipeline(api):
    created = api.post(
        "/api/v1/content-factory/pipelines",
        json={"name": "Crypto News Daily", "description": "ежедневный ролик"},
    )
    assert created.status_code == 200
    pipeline_id = created.json()["id"]

    fetched = api.get(f"/api/v1/content-factory/pipelines/{pipeline_id}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["name"] == "Crypto News Daily"
    assert body["status"] == "DRAFT"
    assert body["version"] is not None


def test_invalid_pipeline_returns_the_error_envelope(api):
    """§76: a code the frontend can switch on, not a sentence it has to parse."""
    response = api.post("/api/v1/content-factory/pipelines", json={"name": ""})
    assert response.status_code == 400
    error = response.json()["detail"]["error"]
    assert error["code"] == "VALIDATION_FAILED"
    assert error["retryable"] is False
    assert error["traceId"]


def test_saving_a_draft_returns_validation(api):
    pipeline_id = api.post("/api/v1/content-factory/pipelines", json={"name": "P"}).json()["id"]
    response = api.put(
        f"/api/v1/content-factory/pipelines/{pipeline_id}/draft",
        json={"nodes": [{"id": "n1", "type": "transform"}], "edges": []},
    )
    assert response.status_code == 200
    validation = response.json()["validation"]
    # No trigger node: an error, because a pipeline that cannot start must not
    # be activatable (§96).
    assert any(item["code"] == "NO_TRIGGER" for item in validation["errors"])


def test_activation_is_refused_while_validation_fails(api):
    pipeline_id = api.post("/api/v1/content-factory/pipelines", json={"name": "P"}).json()["id"]
    api.put(
        f"/api/v1/content-factory/pipelines/{pipeline_id}/draft",
        json={"nodes": [{"id": "n1", "type": "transform"}], "edges": []},
    )
    response = api.post(f"/api/v1/content-factory/pipelines/{pipeline_id}/activate")
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "VALIDATION_FAILED"


def test_a_valid_pipeline_activates(api):
    pipeline_id = api.post("/api/v1/content-factory/pipelines", json={"name": "P"}).json()["id"]
    api.put(
        f"/api/v1/content-factory/pipelines/{pipeline_id}/draft",
        json={
            "nodes": [
                {"id": "trigger", "type": "manual_trigger"},
                {"id": "shape", "type": "transform"},
            ],
            "edges": [{"source": "trigger", "target": "shape"}],
        },
    )
    response = api.post(f"/api/v1/content-factory/pipelines/{pipeline_id}/activate")
    assert response.status_code == 200
    assert response.json()["status"] == "ACTIVE"
    assert response.json()["active_version_id"]


def test_running_an_inactive_pipeline_is_refused(api):
    pipeline_id = api.post("/api/v1/content-factory/pipelines", json={"name": "P"}).json()["id"]
    response = api.post(f"/api/v1/content-factory/pipelines/{pipeline_id}/runs", json={})
    assert response.status_code == 400


# ── Templates (§97) ──────────────────────────────────────────────────────────

def test_templates_are_listed_without_their_builders(api):
    response = api.get("/api/v1/content-factory/templates")
    assert response.status_code == 200
    items = response.json()["items"]
    assert any(item["id"] == "ai_news_shorts" for item in items)
    assert all("build" not in item for item in items)


def test_template_instantiates_into_a_draft(api):
    """§97/§105: a template must never start producing on its own."""
    response = api.post(
        "/api/v1/content-factory/templates/ai_news_shorts/instantiate",
        json={"name": "AI News Daily"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pipeline"]["status"] == "DRAFT"
    # Publishing has no channel yet, so it cannot be activated by accident.
    assert any(item["code"] == "NO_CHANNEL" for item in body["validation"]["errors"])


def test_template_activation_failure_keeps_the_created_draft_visible(api):
    response = api.post(
        "/api/v1/content-factory/templates/ai_news_shorts/instantiate",
        json={"name": "Needs account", "activate": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pipeline"]["status"] == "DRAFT"
    assert body["activation"]["status"] == "BLOCKED"
    assert body["activation"]["error"]["code"] == "VALIDATION_FAILED"


def test_unknown_template_is_a_clean_400(api):
    response = api.post("/api/v1/content-factory/templates/nope/instantiate", json={})
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "VALIDATION_FAILED"


# ── Channels & secrets (§63, §89) ────────────────────────────────────────────

def test_channels_never_expose_their_credential_reference(api):
    content_store.create_channel(
        platform="YOUTUBE",
        account_name="Test",
        credentials_ref="cf:channel:secret-name",
    )
    response = api.get("/api/v1/content-factory/channels")
    assert response.status_code == 200
    body = response.text
    assert "credentials_ref" not in body
    assert "secret-name" not in body


def test_account_proxy_and_credentials_are_kept_out_of_sql_and_responses(api, monkeypatch):
    secrets = {}
    monkeypatch.setattr("backend.valkey_client.set_value", lambda key, value, ttl_seconds=None: secrets.__setitem__(key, value))
    monkeypatch.setattr("backend.valkey_client.get_value", lambda key: secrets.get(key))

    response = api.post("/api/v1/content-factory/channels", json={
        "platform": "YOUTUBE",
        "accountName": "Daily News",
        "credentials": {"refresh_token": "oauth-secret", "client_id": "client-1"},
        "proxyUrl": "http://proxy-user:proxy-pass@proxy.example:8080",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "CONNECTED"
    assert body["proxy_enabled"] == 1
    assert body["proxy_host"] == "proxy.example"
    assert "oauth-secret" not in response.text
    assert "proxy-pass" not in response.text
    stored = next(iter(secrets.values()))
    assert "oauth-secret" in stored and "proxy-pass" in stored


def test_invalid_account_proxy_is_rejected(api):
    response = api.post("/api/v1/content-factory/channels", json={
        "platform": "TIKTOK", "accountName": "Clips", "proxyUrl": "ftp://proxy.example",
    })
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "VALIDATION_FAILED"


def test_provider_listing_carries_no_keys(api):
    response = api.get("/api/v1/content-factory/providers")
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert not any("key" in field.lower() or "secret" in field.lower() for field in item)


# ── Assets (§46) ─────────────────────────────────────────────────────────────

def test_asset_link_is_time_limited(api):
    from backend.content_factory import db

    key = storage_module.build_key("p", "r", "RENDER", "final.mp4")
    storage_module.get_storage().put(key, b"data", mime_type="video/mp4")
    db.execute(
        """
        INSERT INTO cf_assets (id, workspace_id, type, storage_key, mime_type, size_bytes, created_at)
        VALUES ('asset-1', 'default', 'RENDER', ?, 'video/mp4', 4, ?)
        """,
        (key, db.now_iso()),
    )

    response = api.get("/api/v1/content-factory/assets/asset-1/url?expires_in=600")
    assert response.status_code == 200
    assert "signature=" in response.json()["url"]
    assert response.json()["expiresIn"] == 600


def test_unsigned_raw_asset_is_refused(api):
    response = api.get(
        "/api/v1/content-factory/assets/raw",
        params={"key": "content-factory/x", "expires": 99999999999, "signature": "forged"},
    )
    assert response.status_code == 403


# ── Misc ─────────────────────────────────────────────────────────────────────

def test_node_types_are_published_for_the_builder(api):
    response = api.get("/api/v1/content-factory/node-types")
    assert response.status_code == 200
    items = response.json()["items"]
    for expected in ("manual_trigger", "script", "video_composer", "publish"):
        assert expected in items


def test_overview_reports_zeroes_on_an_empty_install(api):
    response = api.get("/api/v1/content-factory/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["activePipelines"] == 0
    assert body["costToday"] == 0
    assert body["liveRuns"] == []


# ── Storage and integrations configuration (§62, §89) ────────────────────────

def test_a_viewer_cannot_switch_the_asset_store(api, monkeypatch):
    _as_role(monkeypatch, "viewer")
    response = api.put(
        "/api/v1/content-factory/storage/config",
        json={"backend": "local"},
    )
    assert response.status_code == 403


def test_storage_config_never_returns_a_password(api, monkeypatch, tmp_path):
    from backend.content_factory import integrations

    monkeypatch.setattr(integrations, "_valkey_get", lambda key: "")
    api.put(
        "/api/v1/content-factory/storage/config",
        json={
            "backend": "nextcloud",
            "nextcloud": {"url": "https://cloud.example.com", "username": "hermes"},
            "nextcloudPassword": "app-password-value",
        },
    )
    response = api.get("/api/v1/content-factory/storage/config")
    assert response.status_code == 200
    assert "app-password-value" not in response.text
    assert response.json()["secrets"]["nextcloudPassword"] == "stored"


def test_an_invalid_storage_configuration_comes_back_in_the_error_envelope(api):
    response = api.put(
        "/api/v1/content-factory/storage/config",
        json={"backend": "nextcloud", "nextcloud": {"url": "ftp://nope"}},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "VALIDATION_FAILED"


def test_the_provider_catalog_lists_what_can_be_connected(api):
    response = api.get("/api/v1/content-factory/integrations/providers")
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert {"stability", "runway", "elevenlabs"} <= ids


def test_connecting_a_provider_never_echoes_its_key(api, monkeypatch):
    from backend.content_factory import integrations

    monkeypatch.setattr(integrations, "_valkey_get", lambda key: "")
    response = api.put(
        "/api/v1/content-factory/integrations/providers/stability",
        json={"apiKey": "sk-must-not-come-back", "options": {"usdPerImage": 0.04}},
    )
    assert response.status_code == 200
    assert "sk-must-not-come-back" not in response.text
    assert response.json()["configured"] is True
    assert response.json()["options"]["usdPerImage"] == 0.04


def test_a_viewer_cannot_connect_a_provider(api, monkeypatch):
    _as_role(monkeypatch, "viewer")
    response = api.put(
        "/api/v1/content-factory/integrations/providers/stability", json={"apiKey": "sk-test"},
    )
    assert response.status_code == 403
