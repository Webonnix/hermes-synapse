"""Configuring the asset store and the media providers from the dashboard.

Two things are worth pinning here and neither is "the setter sets":

* a secret typed on a screen must never end up in `app_settings` — that is the
  one mistake in this module that would be invisible until a database dump
  leaked a Nextcloud password;
* the environment must keep working. A deployment that configures everything in
  `.env` and never opens these screens has to behave exactly as it did before
  the screens existed.

Valkey is not running in the test environment, so every secret write lands in
the `api_keys` fallback — which is itself the path a developer machine takes and
therefore the one worth exercising.
"""

import json

import pytest

from backend import database
from backend.content_factory import integrations, storage as storage_module
from backend.content_factory.domain import ContentFactoryError
from backend.content_factory.providers.registry import set_registry_for_tests


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "cf.db"))
    database._set_backend_for_tests(database.SQLiteBackend())
    database.init_db()
    # No Valkey in tests: every read/write falls through to the api_keys table.
    monkeypatch.setattr(integrations, "_valkey_get", lambda key: "")
    yield
    storage_module.set_storage_for_tests(None)
    set_registry_for_tests(None)
    database._set_backend_for_tests(None)


# ── Secrets ──────────────────────────────────────────────────────────────────

def test_environment_wins_over_a_stored_secret(db, monkeypatch):
    integrations.write_secret("STABILITY_API_KEY", "stored-key")
    monkeypatch.setenv("STABILITY_API_KEY", "env-key")
    assert integrations.read_secret("STABILITY_API_KEY") == "env-key"
    assert integrations.secret_source("STABILITY_API_KEY") == "env"


def test_placeholder_env_value_counts_as_unset(db, monkeypatch):
    monkeypatch.setenv("STABILITY_API_KEY", "your_key_here")
    assert integrations.read_secret("STABILITY_API_KEY") == ""
    assert integrations.secret_source("STABILITY_API_KEY") == "none"


def test_a_stored_secret_never_reaches_app_settings(db):
    integrations.save_storage_config({
        "backend": "nextcloud",
        "nextcloud": {"url": "https://cloud.example.com", "username": "hermes"},
        "nextcloudPassword": "app-password-value",
    })
    document = database.get_setting(integrations.STORAGE_SETTING_KEY) or ""
    assert "app-password-value" not in document
    assert integrations.read_secret("CONTENT_FACTORY_NEXTCLOUD_PASSWORD") == "app-password-value"


def test_public_config_reports_sources_not_values(db):
    integrations.save_storage_config({
        "backend": "nextcloud",
        "nextcloud": {"url": "https://cloud.example.com", "username": "hermes"},
        "nextcloudPassword": "hunter2-app-password",
    })
    public = integrations.storage_config_public()
    assert public["secrets"]["nextcloudPassword"] == "stored"
    assert "hunter2-app-password" not in json.dumps(public)


# ── Storage configuration ────────────────────────────────────────────────────

def test_nextcloud_requires_url_user_and_password(db):
    with pytest.raises(ContentFactoryError):
        integrations.save_storage_config({"backend": "nextcloud", "nextcloud": {"url": "not-a-url"}})
    with pytest.raises(ContentFactoryError):
        integrations.save_storage_config({
            "backend": "nextcloud", "nextcloud": {"url": "https://cloud.example.com"},
        })


def test_a_lan_nextcloud_over_plain_http_is_accepted(db):
    """The AI server's own Nextcloud is on the LAN; rejecting http would make
    the screen useless on exactly the deployment it was written for."""
    saved = integrations.save_storage_config({
        "backend": "nextcloud",
        "nextcloud": {"url": "http://192.168.0.200:8080", "username": "hermes"},
        "nextcloudPassword": "app-password",
    })
    assert saved["backend"] == "nextcloud"
    assert saved["nextcloud"]["url"] == "http://192.168.0.200:8080"


def test_saving_a_secret_does_not_wipe_it_on_the_next_save(db):
    integrations.save_storage_config({
        "backend": "nextcloud",
        "nextcloud": {"url": "https://cloud.example.com", "username": "hermes"},
        "nextcloudPassword": "app-password",
    })
    integrations.save_storage_config({"nextcloud": {"root": "factory"}})
    assert integrations.read_secret("CONTENT_FACTORY_NEXTCLOUD_PASSWORD") == "app-password"
    assert integrations.get_storage_config()["nextcloud"]["root"] == "factory"


def test_an_explicit_null_deletes_the_secret(db):
    integrations.save_storage_config({
        "backend": "local", "nextcloudPassword": "app-password",
    })
    assert integrations.secret_source("CONTENT_FACTORY_NEXTCLOUD_PASSWORD") == "stored"
    integrations.save_storage_config({"backend": "local", "nextcloudPassword": None})
    assert integrations.secret_source("CONTENT_FACTORY_NEXTCLOUD_PASSWORD") == "none"


def test_the_saved_choice_is_what_build_storage_returns(db, tmp_path, monkeypatch):
    """The point of the screen: a choice made here beats the environment and
    applies without a restart."""
    monkeypatch.setenv("CONTENT_FACTORY_NEXTCLOUD_URL", "https://env-cloud.example.com")
    integrations.save_storage_config({
        "backend": "local", "local": {"root": str(tmp_path / "assets")},
    })
    built = storage_module.build_storage()
    assert isinstance(built, storage_module.LocalStorage)
    assert str(built.root) == str(tmp_path / "assets")


def test_backend_env_hands_the_decision_back(db, monkeypatch, tmp_path):
    monkeypatch.setenv("CONTENT_FACTORY_LOCAL_STORAGE", str(tmp_path / "from-env"))
    integrations.save_storage_config({"backend": "env"})
    assert integrations.build_configured_storage() is None
    assert str(storage_module.build_storage().root) == str(tmp_path / "from-env")


def test_saving_drops_the_storage_singleton(db, tmp_path):
    storage_module.set_storage_for_tests(storage_module.LocalStorage(str(tmp_path / "old")))
    integrations.save_storage_config({"backend": "local", "local": {"root": str(tmp_path / "new")}})
    assert str(storage_module.get_storage().root) == str(tmp_path / "new")


# ── Media providers ──────────────────────────────────────────────────────────

def test_connecting_a_provider_registers_it(db):
    integrations.save_media_provider("stability", {"apiKey": "sk-test", "options": {"usdPerImage": 0.05}})
    resolved = integrations.resolve_media_provider("stability")
    assert resolved is not None
    assert resolved.api_key == "sk-test"
    assert resolved.config["usdPerImage"] == 0.05

    from backend.content_factory.providers.image import build_image_providers

    assert [provider.id for provider in build_image_providers()] == ["stability"]


def test_a_disabled_provider_disappears_from_the_registry(db):
    integrations.save_media_provider("stability", {"apiKey": "sk-test"})
    integrations.save_media_provider("stability", {"enabled": False})
    assert integrations.resolve_media_provider("stability") is None

    from backend.content_factory.providers.image import build_image_providers

    assert build_image_providers() == []


def test_an_environment_key_still_configures_a_provider(db, monkeypatch):
    monkeypatch.setenv("RUNWAY_API_KEY", "env-runway")
    monkeypatch.setenv("RUNWAY_USD_PER_SECOND", "0.25")
    resolved = integrations.resolve_media_provider("runway")
    assert resolved is not None
    assert resolved.api_key == "env-runway"
    assert resolved.config["usdPerSecond"] == 0.25


def test_kling_carries_its_access_key_pair(db):
    integrations.save_media_provider("kling", {"apiKey": "secret-half", "accessKey": "access-half"})
    resolved = integrations.resolve_media_provider("kling")
    assert resolved is not None
    assert resolved.api_key == "secret-half"
    assert resolved.config["accessKey"] == "access-half"


def test_the_catalog_lists_unconnected_providers_too(db):
    rows = {row["id"]: row for row in integrations.list_media_providers()}
    assert rows["stability"]["configured"] is False
    assert rows["stability"]["category"] == "IMAGE"
    # The card knows what to ask for even before anything is connected.
    assert any(field["type"] == "secret" for field in rows["stability"]["fields"])


def test_no_secret_appears_in_the_catalog(db):
    integrations.save_media_provider("stability", {"apiKey": "sk-should-never-be-returned"})
    assert "sk-should-never-be-returned" not in json.dumps(integrations.list_media_providers())


def test_deleting_a_provider_forgets_its_key(db):
    integrations.save_media_provider("stability", {"apiKey": "sk-test"})
    integrations.delete_media_provider("stability")
    assert integrations.resolve_media_provider("stability") is None
    assert integrations.secret_source("STABILITY_API_KEY") == "none"


def test_an_unknown_provider_is_refused(db):
    with pytest.raises(ContentFactoryError):
        integrations.save_media_provider("not-a-vendor", {"apiKey": "x"})


# ── Category defaults ────────────────────────────────────────────────────────

def test_the_chosen_default_wins_over_registration_order(db):
    integrations.save_media_provider("stability", {"apiKey": "sk-a"})
    integrations.save_media_provider("openai-image", {"apiKey": "sk-b"})
    integrations.set_category_default("IMAGE", "openai-image")

    from backend.content_factory.providers.registry import build_registry

    assert build_registry().default_for("IMAGE").id == "openai-image"


def test_a_default_from_the_wrong_category_is_refused(db):
    integrations.save_media_provider("stability", {"apiKey": "sk-a"})
    with pytest.raises(ContentFactoryError):
        integrations.set_category_default("VIDEO", "stability")


def test_a_default_naming_an_unconfigured_provider_is_ignored(db):
    integrations.save_media_provider("stability", {"apiKey": "sk-a"})
    integrations.set_category_default("IMAGE", "openai-image")  # configured...
    integrations.delete_media_provider("openai-image")  # ...then disconnected

    from backend.content_factory.providers.registry import build_registry

    default = build_registry().default_for("IMAGE")
    assert default is not None and default.id == "stability"


# ── The LLM category default ─────────────────────────────────────────────────
# Media vendors come from CATALOG; language models do not — `local-llm` plus one
# provider per approved provider_bindings row, whose ids are generated at
# binding time. Validating only against CATALOG made "use the external model by
# default" unreachable: the LLM category could never be pointed anywhere but
# whatever _load_llm registered first, which is always the local model.

@pytest.fixture()
def approved_llm_binding(db, monkeypatch):
    """One active openai_compatible binding, credentials resolvable."""
    from backend import provider_governance

    monkeypatch.setattr(provider_governance, "DB_PATH", database.DB_PATH)
    provider_governance._init_schema()
    store: dict[str, str] = {}
    monkeypatch.setattr("backend.valkey_client.set_value", lambda k, v, ttl_seconds=None: store.__setitem__(k, v))
    monkeypatch.setattr("backend.valkey_client.get_value", lambda k: store.get(k))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda k: store.pop(k, None))
    monkeypatch.setattr("backend.control_plane.create_review_task", lambda **kw: {"id": "task-1"})

    proposal = provider_governance.create_binding_proposal(
        "omniroute", "openai_compatible", "http://omniroute:20128/v1", "sk-router",
        default_model="ag/gemini-3.7-flash-high",
    )
    provider_governance._set_status(proposal["id"], "active")
    return proposal["id"]


def test_an_llm_binding_can_be_made_the_default(approved_llm_binding):
    integrations.set_category_default("LLM", approved_llm_binding)

    from backend.content_factory.providers.registry import build_registry

    default = build_registry().default_for("LLM")
    assert default is not None and default.id == approved_llm_binding
    assert default.model == "ag/gemini-3.7-flash-high"


def test_the_local_model_remains_selectable_as_the_default(approved_llm_binding):
    from backend.content_factory.providers.llm import LOCAL_PROVIDER_ID

    integrations.set_category_default("LLM", approved_llm_binding)
    integrations.set_category_default("LLM", LOCAL_PROVIDER_ID)

    from backend.content_factory.providers.registry import build_registry

    assert build_registry().default_for("LLM").id == LOCAL_PROVIDER_ID


def test_an_llm_binding_is_still_refused_for_a_media_category(approved_llm_binding):
    with pytest.raises(ContentFactoryError):
        integrations.set_category_default("IMAGE", approved_llm_binding)


def test_an_id_that_is_neither_a_vendor_nor_a_binding_is_refused(db):
    with pytest.raises(ContentFactoryError):
        integrations.set_category_default("LLM", "prov-does-not-exist")


def test_setting_the_model_on_an_existing_binding(approved_llm_binding):
    integrations.set_llm_endpoint_model(approved_llm_binding, "ds/deepseek-chat")

    from backend.content_factory.providers.registry import build_registry

    provider = build_registry().get(approved_llm_binding)
    assert provider is not None and provider.model == "ds/deepseek-chat"


def test_setting_the_model_on_a_missing_binding_is_refused(db):
    with pytest.raises(ContentFactoryError):
        integrations.set_llm_endpoint_model("prov-nope", "m")


def test_deleting_an_llm_endpoint_clears_its_default(approved_llm_binding):
    integrations.set_category_default("LLM", approved_llm_binding)
    integrations.delete_llm_endpoint(approved_llm_binding)
    assert "LLM" not in integrations.category_defaults()


# ── OneDrive sign-in ─────────────────────────────────────────────────────────
# Device code rather than a redirect, for the same reason matrix_oauth.py chose
# it: no fixed public HTTPS origin Microsoft would accept as a redirect URI on
# every deployment. What matters here is that a still-pending sign-in is a
# *status* and not an exception — the UI polls this every five seconds, and a
# raise would surface as an error banner over a flow that is working fine.

def _onedrive_configured(monkeypatch):
    integrations.save_storage_config({"backend": "onedrive", "onedrive": {"clientId": "app-id"}})


def test_starting_a_login_without_a_client_id_is_refused(db):
    with pytest.raises(ContentFactoryError):
        integrations.start_onedrive_login()


def test_a_pending_sign_in_is_a_status_not_an_error(db, monkeypatch):
    import httpx

    _onedrive_configured(monkeypatch)
    integrations.write_secret("CONTENT_FACTORY_ONEDRIVE_DEVICE_CODE", "dc-1")
    monkeypatch.setattr(
        httpx, "post",
        lambda url, data=None, timeout=None: httpx.Response(400, json={"error": "authorization_pending"}),
    )
    assert integrations.finish_onedrive_login()["status"] == "pending"


def test_a_completed_sign_in_stores_only_the_refresh_token(db, monkeypatch):
    import httpx

    _onedrive_configured(monkeypatch)
    integrations.write_secret("CONTENT_FACTORY_ONEDRIVE_DEVICE_CODE", "dc-1")
    monkeypatch.setattr(
        httpx, "post",
        lambda url, data=None, timeout=None: httpx.Response(200, json={
            "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
        }),
    )
    assert integrations.finish_onedrive_login()["status"] == "connected"
    assert integrations.read_secret("CONTENT_FACTORY_ONEDRIVE_REFRESH_TOKEN") == "rt-1"
    # The short-lived access token is never persisted, and the spent device code
    # is cleared so a second poll cannot replay it.
    assert integrations.read_secret("CONTENT_FACTORY_ONEDRIVE_DEVICE_CODE") == ""
    stored = json.loads(database.get_setting(integrations.STORAGE_SETTING_KEY) or "{}")
    assert "rt-1" not in json.dumps(stored)


def test_a_sign_in_without_offline_access_is_refused(db, monkeypatch):
    """No refresh token means the store works for an hour and then silently
    dies — better to fail the sign-in and say why."""
    import httpx

    _onedrive_configured(monkeypatch)
    integrations.write_secret("CONTENT_FACTORY_ONEDRIVE_DEVICE_CODE", "dc-1")
    monkeypatch.setattr(
        httpx, "post",
        lambda url, data=None, timeout=None: httpx.Response(200, json={"access_token": "at-1"}),
    )
    with pytest.raises(ContentFactoryError):
        integrations.finish_onedrive_login()


def test_an_expired_device_code_reports_expired_and_clears_itself(db, monkeypatch):
    import httpx

    _onedrive_configured(monkeypatch)
    integrations.write_secret("CONTENT_FACTORY_ONEDRIVE_DEVICE_CODE", "dc-1")
    monkeypatch.setattr(
        httpx, "post",
        lambda url, data=None, timeout=None: httpx.Response(400, json={"error": "expired_token"}),
    )
    assert integrations.finish_onedrive_login()["status"] == "expired"
    assert integrations.read_secret("CONTENT_FACTORY_ONEDRIVE_DEVICE_CODE") == ""


def test_signing_out_forgets_the_stored_token(db):
    integrations.write_secret("CONTENT_FACTORY_ONEDRIVE_REFRESH_TOKEN", "rt-1")
    integrations.onedrive_sign_out()
    assert integrations.read_secret("CONTENT_FACTORY_ONEDRIVE_REFRESH_TOKEN") == ""


def test_onedrive_is_a_selectable_storage_backend(db):
    assert "onedrive" in integrations.STORAGE_BACKENDS
    config = integrations.save_storage_config(
        {"backend": "onedrive", "onedrive": {"clientId": "app-id", "tenant": "consumers", "root": "hermes"}}
    )
    assert config["backend"] == "onedrive"
    assert config["onedrive"]["clientId"] == "app-id"


# ── A borrowed credential ────────────────────────────────────────────────────
# The OmniRoute image provider authenticates with ROUTER_API_KEY, which belongs to
# the AI Router and is shared with agent routing and the tier chain. Clearing it
# because somebody disconnected image generation would take all of that down.

def test_disconnecting_the_router_image_provider_keeps_the_shared_key(db):
    integrations.write_secret("ROUTER_API_KEY", "sk-router")
    integrations.save_media_provider("router-image", {"model": "gemini/gemini-2.5-flash-image"})
    integrations.delete_media_provider("router-image")

    assert integrations.read_secret("ROUTER_API_KEY") == "sk-router"
    assert "router-image" not in integrations.get_provider_document()["providers"]


def test_the_shared_key_cannot_be_overwritten_from_this_screen(db):
    integrations.write_secret("ROUTER_API_KEY", "sk-router")
    integrations.save_media_provider("router-image", {"apiKey": "sk-typed-here"})
    assert integrations.read_secret("ROUTER_API_KEY") == "sk-router"


def test_the_router_image_provider_configures_itself_from_the_stored_key(db):
    integrations.write_secret("ROUTER_API_KEY", "sk-router")
    resolved = integrations.resolve_media_provider("router-image")
    assert resolved is not None
    assert resolved.api_key == "sk-router"
    # Nano Banana is the reason to have this at all; the default must be an
    # image model, not whatever the LLM side happens to use.
    assert "image" in resolved.model
