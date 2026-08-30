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
