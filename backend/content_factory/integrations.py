"""Connectable integrations: the asset store and the media providers, configured
from the dashboard instead of from the container's environment.

Until this module existed both were environment-only: switching from local disk
to Nextcloud, or adding a Stability key, meant editing `.env` on the host and
restarting the backend — which is not something the owner of a running factory
should have to do, and is impossible from the web UI at all. The Integrations
and Storage screens now write here.

Three rules it keeps, all of them inherited from how the rest of HERMES already
treats configuration:

* **Secrets never enter `app_settings`.** A key goes to Valkey (the same store
  `provider_governance` uses for LLM bindings, under the `content_factory:`
  namespace `storage._secret` already reads), and falls back to the
  dashboard-managed `api_keys` table when Valkey is unreachable — which is what
  a developer machine and the test suite get. Only *names* and non-secret
  fields (a URL, a bucket, a model id) are stored as settings JSON.
* **The environment still wins.** A key set in `.env` keeps working and is
  reported as coming from the environment, so a deployment that configures
  everything through docker-compose does not change behaviour by upgrading. The
  UI only *adds* a second, editable source for what the env did not provide.
  The one exception is the storage backend itself: an explicit choice made in
  the UI is what the owner just asked for, so it takes precedence over
  `CONTENT_FACTORY_*` — and "env" stays available as the choice that hands the
  decision back.
* **Nothing is applied by restart.** Saving resets the storage singleton and the
  provider registry, so the next pipeline node uses the new configuration.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

from backend.content_factory.domain import ContentFactoryError, ErrorCode

logger = logging.getLogger("hermes.content_factory.integrations")

STORAGE_SETTING_KEY = "content_factory_storage"
PROVIDER_SETTING_KEY = "content_factory_providers"

# Valkey namespace. `storage._secret()` already looks secrets up under exactly
# this prefix, so a password written here is found by the storage layer even on
# a code path that never imports this module.
SECRET_NAMESPACE = "content_factory:"


# ── Secret store ─────────────────────────────────────────────────────────────

def read_secret(name: str) -> str:
    """Environment first, then Valkey, then the dashboard `api_keys` table.

    The order matters: a deployment that ships keys in its env must not be
    silently overridden by something typed into the UI months earlier, and a
    placeholder left in `.env.example` style (`your_key_here`) counts as unset.
    """
    direct = os.environ.get(name, "").strip()
    if direct and not direct.startswith("your_"):
        return direct
    stored = _valkey_get(f"{SECRET_NAMESPACE}{name.lower()}")
    if stored:
        return stored
    try:
        from backend.database import get_api_key

        return (get_api_key(name) or "").strip()
    except Exception:
        return ""


def secret_source(name: str) -> str:
    """Where `read_secret` would get this one from: env | stored | none."""
    direct = os.environ.get(name, "").strip()
    if direct and not direct.startswith("your_"):
        return "env"
    if _valkey_get(f"{SECRET_NAMESPACE}{name.lower()}"):
        return "stored"
    try:
        from backend.database import get_api_key

        if (get_api_key(name) or "").strip():
            return "stored"
    except Exception:
        pass
    return "none"


def write_secret(name: str, value: str) -> str:
    """Valkey when it answers, the `api_keys` table otherwise. Returns which."""
    clean = str(value or "").strip()
    if not clean:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, f"{name} is empty", retryable=False)
    try:
        from backend.valkey_client import set_value

        set_value(f"{SECRET_NAMESPACE}{name.lower()}", clean)
        return "valkey"
    except Exception:
        logger.warning("Valkey unavailable — %s stored in the api_keys table instead", name)
    from backend.database import set_api_key

    set_api_key(name, clean)
    return "database"


def clear_secret(name: str) -> None:
    try:
        from backend.valkey_client import delete_value

        delete_value(f"{SECRET_NAMESPACE}{name.lower()}")
    except Exception:
        pass
    try:
        from backend.database import delete_api_key

        delete_api_key(name)
    except Exception:
        pass


def _valkey_get(key: str) -> str:
    try:
        from backend.valkey_client import get_value

        return (get_value(key) or "").strip()
    except Exception:
        return ""


# ── Asset storage ────────────────────────────────────────────────────────────

STORAGE_BACKENDS = ("env", "local", "nextcloud", "s3", "onedrive")

STORAGE_DEFAULTS: dict[str, Any] = {
    # "env" means "keep following CONTENT_FACTORY_* as before" — the value a
    # deployment that never opens this screen has.
    "backend": "env",
    "nextcloud": {
        "url": "",
        "username": "",
        "root": "",
        "publicUrl": "",
        "verifyTls": True,
        "shareDays": 7,
    },
    "s3": {"bucket": "", "endpoint": "", "region": "", "publicUrl": ""},
    # `clientId` is the one field the owner has to bring from outside: OneDrive
    # has no password to type, only an app registration to sign in through. The
    # refresh token that sign-in produces is a secret and lives in the secret
    # store, never here.
    "onedrive": {"clientId": "", "tenant": "consumers", "root": ""},
    "local": {"root": ""},
}

# UI field name → the secret it is stored under. The names are the existing env
# variables, so a value typed here and a value in `.env` are the same setting.
STORAGE_SECRETS = {
    "nextcloudPassword": "CONTENT_FACTORY_NEXTCLOUD_PASSWORD",
    "s3AccessKey": "CONTENT_FACTORY_S3_ACCESS_KEY",
    "s3SecretKey": "CONTENT_FACTORY_S3_SECRET_KEY",
    "onedriveRefreshToken": "CONTENT_FACTORY_ONEDRIVE_REFRESH_TOKEN",
}


def get_storage_config() -> dict[str, Any]:
    """Stored config merged over the defaults (never the secrets)."""
    merged = copy.deepcopy(STORAGE_DEFAULTS)
    for section, values in _load_document(STORAGE_SETTING_KEY).items():
        if section == "backend" and isinstance(values, str):
            merged["backend"] = values
        elif section in merged and isinstance(values, dict):
            merged[section].update({k: v for k, v in values.items() if k in merged[section]})
    if merged["backend"] not in STORAGE_BACKENDS:
        merged["backend"] = "env"
    return merged


def storage_config_public() -> dict[str, Any]:
    """What the Storage screen renders: the config, which secrets exist and
    where they came from, and what the environment would give on its own — so
    "env" is a visible choice with a known consequence rather than a mystery."""
    config = get_storage_config()
    return {
        **config,
        "secrets": {field: secret_source(name) for field, name in STORAGE_SECRETS.items()},
        "envBackend": _env_backend(),
        "backends": list(STORAGE_BACKENDS),
        "requireRemote": _require_remote(),
    }


def save_storage_config(patch: dict[str, Any]) -> dict[str, Any]:
    """Validate, store the non-secret fields, move the secrets into the secret
    store, and drop the storage singleton so the next asset write uses it.

    A secret field is three-valued on purpose: absent or `""` leaves the stored
    one alone (the UI never receives it back, so it cannot echo it), and `null`
    deletes it. Without that distinction, saving an unrelated field would wipe
    the password.
    """
    config = get_storage_config()
    patch = patch or {}

    backend = str(patch.get("backend") or config["backend"]).strip().lower()
    if backend not in STORAGE_BACKENDS:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            f"Unknown storage backend: {backend}",
            retryable=False,
        )
    config["backend"] = backend

    for section in ("nextcloud", "s3", "onedrive", "local"):
        values = patch.get(section)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if key not in config[section]:
                continue
            if key == "verifyTls":
                config[section][key] = bool(value)
            elif key == "shareDays":
                config[section][key] = max(1, min(365, int(value or 7)))
            else:
                config[section][key] = str(value or "").strip()

    for field_name, secret_name in STORAGE_SECRETS.items():
        if field_name not in patch:
            continue
        value = patch[field_name]
        if value is None:
            clear_secret(secret_name)
        elif str(value).strip():
            write_secret(secret_name, str(value))

    _validate_storage(config)
    _save_document(STORAGE_SETTING_KEY, config)
    reset_storage()
    return storage_config_public()


def _validate_storage(config: dict[str, Any]) -> None:
    backend = config["backend"]
    if backend == "nextcloud":
        url = config["nextcloud"]["url"]
        _require_url(url, "nextcloud.url")
        if config["nextcloud"]["publicUrl"]:
            _require_url(config["nextcloud"]["publicUrl"], "nextcloud.publicUrl")
        if not config["nextcloud"]["username"]:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, "Nextcloud needs a user name", retryable=False
            )
        if secret_source(STORAGE_SECRETS["nextcloudPassword"]) == "none":
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED,
                "Nextcloud needs an app password — create one in Nextcloud → Security",
                retryable=False,
            )
    elif backend == "s3":
        if not config["s3"]["bucket"]:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, "S3 needs a bucket name", retryable=False
            )
        if config["s3"]["endpoint"]:
            _require_url(config["s3"]["endpoint"], "s3.endpoint")


def _require_url(value: str, label: str) -> None:
    """http(s) with a host. Deliberately *not* the SSRF guard used for MCP
    servers: a Nextcloud on the LAN (`http://192.168.0.200`) is the normal case
    for this deployment, and rejecting it would make the whole screen useless."""
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            f"{label} must be a http(s) URL, for example https://cloud.example.com",
            retryable=False,
        )


def build_configured_storage():
    """The storage the saved config asks for, or None to follow the environment.

    Returning None rather than a LocalStorage keeps `storage.build_storage()`
    the single place that knows the env fallback order.
    """
    from backend.content_factory import storage as storage_module

    config = get_storage_config()
    backend = config["backend"]
    if backend == "env":
        return None
    return storage_from_config(config)


def storage_from_config(config: dict[str, Any]):
    """Config document → a storage instance. Shared by the live path and by the
    "test connection" button, so what is tested is what will be used."""
    from backend.content_factory import storage as storage_module

    backend = config.get("backend") or "local"
    if backend == "nextcloud":
        section = config.get("nextcloud") or {}
        return storage_module.NextcloudStorage(
            str(section.get("url") or ""),
            username=str(section.get("username") or ""),
            password=str(section.get("password") or "") or read_secret(STORAGE_SECRETS["nextcloudPassword"]),
            root=str(section.get("root") or ""),
            verify_tls=bool(section.get("verifyTls", True)),
            share_expire_days=int(section.get("shareDays") or 7),
            public_base_url=str(section.get("publicUrl") or ""),
        )
    if backend == "onedrive":
        section = config.get("onedrive") or {}
        return storage_module.OneDriveStorage(
            client_id=str(section.get("clientId") or "").strip()
            or os.environ.get("CONTENT_FACTORY_ONEDRIVE_CLIENT_ID", "").strip(),
            refresh_token=str(section.get("refreshToken") or "")
            or read_secret(STORAGE_SECRETS["onedriveRefreshToken"]),
            tenant=str(section.get("tenant") or "consumers").strip() or "consumers",
            root=str(section.get("root") or ""),
            # Microsoft rotates the refresh token on most exchanges; persisting
            # the new one is what keeps the store alive across restarts.
            on_token_refreshed=lambda token: write_secret(
                STORAGE_SECRETS["onedriveRefreshToken"], token
            ),
        )
    if backend == "s3":
        section = config.get("s3") or {}
        return storage_module.S3Storage(
            str(section.get("bucket") or ""),
            endpoint_url=str(section.get("endpoint") or ""),
            region=str(section.get("region") or ""),
            access_key=str(section.get("accessKey") or "") or read_secret(STORAGE_SECRETS["s3AccessKey"]),
            secret_key=str(section.get("secretKey") or "") or read_secret(STORAGE_SECRETS["s3SecretKey"]),
            public_base_url=str(section.get("publicUrl") or ""),
        )
    root = str((config.get("local") or {}).get("root") or "").strip()
    return storage_module.LocalStorage(root or storage_module.default_local_root())


# ── OneDrive sign-in ─────────────────────────────────────────────────────────
#
# Device code rather than a redirect: the dashboard has no fixed public HTTPS
# origin Microsoft would accept as a redirect URI on every deployment, and the
# owner signing in is already sitting in front of the machine. The same reason
# matrix_oauth.py chose it, and the same failure it prevents — a pasted token
# that expires within the hour (see [[matrix_mas_device_login]]).

# The pending device code is itself a bearer-equivalent for the length of the
# flow, so it is stored under a secret name like every other credential here.
_ONEDRIVE_DEVICE_CODE_SECRET = "CONTENT_FACTORY_ONEDRIVE_DEVICE_CODE"


def _onedrive_client_id() -> str:
    config = get_storage_config()
    return (
        str((config.get("onedrive") or {}).get("clientId") or "").strip()
        or os.environ.get("CONTENT_FACTORY_ONEDRIVE_CLIENT_ID", "").strip()
    )


def _onedrive_tenant() -> str:
    config = get_storage_config()
    return str((config.get("onedrive") or {}).get("tenant") or "consumers").strip() or "consumers"


def start_onedrive_login() -> dict[str, Any]:
    """Ask Microsoft for a device code. Returns what the owner has to do next:
    open `verificationUri` and type `userCode`."""
    import httpx

    from backend.content_factory.storage import ONEDRIVE_SCOPES

    client_id = _onedrive_client_id()
    if not client_id:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            "Сначала укажите Client ID приложения Entra и сохраните настройки",
            retryable=False,
        )
    try:
        response = httpx.post(
            f"https://login.microsoftonline.com/{_onedrive_tenant()}/oauth2/v2.0/devicecode",
            data={"client_id": client_id, "scope": ONEDRIVE_SCOPES},
            timeout=30.0,
        )
    except Exception as exc:
        raise ContentFactoryError(
            ErrorCode.PROVIDER_UNAVAILABLE, "Microsoft недоступен", retryable=True
        ) from exc
    if response.status_code != 200:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            f"Microsoft отклонил Client ID: {response.text[:200]}",
            retryable=False,
        )
    body = response.json()
    # The device code itself is a bearer-equivalent for the duration of the
    # flow, so it goes to the secret store rather than into the settings JSON.
    write_secret(_ONEDRIVE_DEVICE_CODE_SECRET, str(body.get("device_code") or ""))
    return {
        "userCode": body.get("user_code"),
        "verificationUri": body.get("verification_uri"),
        "expiresIn": body.get("expires_in"),
        "interval": body.get("interval", 5),
    }


def finish_onedrive_login() -> dict[str, Any]:
    """Exchange the pending device code for tokens. Called on a timer by the UI
    while the owner completes the sign-in in their browser.

    `authorization_pending` is the normal answer until they do, so it is
    reported as a status rather than raised — a polling caller must be able to
    tell "not yet" from "this failed".
    """
    import httpx

    device_code = read_secret(_ONEDRIVE_DEVICE_CODE_SECRET)
    if not device_code:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "Вход не начат", retryable=False
        )
    try:
        response = httpx.post(
            f"https://login.microsoftonline.com/{_onedrive_tenant()}/oauth2/v2.0/token",
            data={
                "client_id": _onedrive_client_id(),
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
            },
            timeout=30.0,
        )
    except Exception as exc:
        raise ContentFactoryError(
            ErrorCode.PROVIDER_UNAVAILABLE, "Microsoft недоступен", retryable=True
        ) from exc

    body = response.json() if response.content else {}
    if response.status_code == 200:
        refresh_token = str(body.get("refresh_token") or "")
        if not refresh_token:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_AUTH,
                "Microsoft не выдал refresh token — в приложении Entra не разрешён offline_access",
                retryable=False,
            )
        write_secret(STORAGE_SECRETS["onedriveRefreshToken"], refresh_token)
        clear_secret(_ONEDRIVE_DEVICE_CODE_SECRET)
        reset_storage()
        return {"status": "connected"}

    error = str(body.get("error") or "")
    if error in {"authorization_pending", "slow_down"}:
        return {"status": "pending", "slowDown": error == "slow_down"}
    clear_secret(_ONEDRIVE_DEVICE_CODE_SECRET)
    if error == "expired_token":
        return {"status": "expired"}
    raise ContentFactoryError(
        ErrorCode.PROVIDER_AUTH,
        str(body.get("error_description") or error or "Microsoft отклонил вход")[:300],
        retryable=False,
    )


def onedrive_sign_out() -> None:
    """Forget the stored sign-in. The app registration stays; only this
    deployment's access to the drive goes away."""
    clear_secret(STORAGE_SECRETS["onedriveRefreshToken"])
    clear_secret(_ONEDRIVE_DEVICE_CODE_SECRET)
    reset_storage()


def test_storage_config(patch: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Health-check a candidate configuration without saving it.

    The draft is merged over what is stored, so the owner can test a changed
    URL against the password that is already there without re-typing it.
    """
    config = get_storage_config()
    patch = patch or {}
    backend = str(patch.get("backend") or config["backend"]).strip().lower()
    if backend == "env":
        from backend.content_factory import storage as storage_module

        return storage_module.get_storage().health()
    config["backend"] = backend
    for section in ("nextcloud", "s3", "onedrive", "local"):
        values = patch.get(section)
        if isinstance(values, dict):
            config[section] = {**config[section], **values}
    # A password typed but not yet saved is used for the test only.
    if str(patch.get("nextcloudPassword") or "").strip():
        config["nextcloud"]["password"] = str(patch["nextcloudPassword"]).strip()
    if str(patch.get("s3AccessKey") or "").strip():
        config["s3"]["accessKey"] = str(patch["s3AccessKey"]).strip()
    if str(patch.get("s3SecretKey") or "").strip():
        config["s3"]["secretKey"] = str(patch["s3SecretKey"]).strip()

    try:
        _validate_storage(config)
        return storage_from_config(config).health()
    except ContentFactoryError:
        raise
    except Exception as exc:
        logger.warning("Storage test failed: %s", exc)
        return {"storage": backend, "status": "error", "detail": type(exc).__name__}


def reset_storage() -> None:
    from backend.content_factory import storage as storage_module

    storage_module.reset_storage()


def _env_backend() -> str:
    if os.environ.get("CONTENT_FACTORY_NEXTCLOUD_URL", "").strip():
        return "nextcloud"
    if os.environ.get("CONTENT_FACTORY_S3_BUCKET", "").strip():
        return "s3"
    return "local"


def _require_remote() -> bool:
    return os.environ.get(
        "CONTENT_FACTORY_REQUIRE_REMOTE", os.environ.get("CONTENT_FACTORY_REQUIRE_S3", "")
    ).strip().lower() in {"1", "true", "yes"}


# ── Media providers ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProviderField:
    """One input on the provider card. `type` is what the UI renders and what
    the API accepts: a `secret` is write-only and never sent back."""

    name: str
    label: tuple[str, str]
    type: str = "text"  # text | secret | number
    required: bool = False
    placeholder: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    category: str
    label: tuple[str, str]
    secret_name: str
    docs: str = ""
    model_env: str = ""
    default_model: str = ""
    hint: tuple[str, str] = ("", "")
    fields: tuple[ProviderField, ...] = ()
    extra_secrets: dict[str, str] = field(default_factory=dict)
    # Option → the env var that used to carry it, so a deployment that priced a
    # vendor in `.env` keeps that price until somebody overrides it here.
    option_env: dict[str, str] = field(default_factory=dict)
    # True when `secret_name` is a credential this provider borrows rather than
    # owns — ROUTER_API_KEY is the AI Router's key, shared with agent routing
    # and the tier chain. Disconnecting the provider must then forget the
    # provider, not the key: clearing it here would silently take down
    # everything else that routes through the same sidecar.
    shared_secret: bool = False


_API_KEY_FIELD = ProviderField("apiKey", ("API-ключ", "API key"), "secret", required=True)
_MODEL_FIELD = ProviderField("model", ("Модель", "Model"))

CATALOG: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        "stability", "IMAGE", ("Stability AI", "Stability AI"), "STABILITY_API_KEY",
        docs="https://platform.stability.ai/account/keys",
        hint=("Stable Image Core, ~$0.03 за изображение", "Stable Image Core, ~$0.03 per image"),
        fields=(_API_KEY_FIELD, ProviderField("usdPerImage", ("Цена за изображение, $", "USD per image"), "number")),
    ),
    ProviderSpec(
        "openai-image", "IMAGE", ("OpenAI Images", "OpenAI Images"), "OPENAI_API_KEY",
        docs="https://platform.openai.com/api-keys", model_env="OPENAI_IMAGE_MODEL",
        fields=(_API_KEY_FIELD, _MODEL_FIELD,
                ProviderField("usdPerImage", ("Цена за изображение, $", "USD per image"), "number")),
    ),
    ProviderSpec(
        "replicate-image", "IMAGE", ("Replicate (изображения)", "Replicate (images)"), "REPLICATE_API_KEY",
        docs="https://replicate.com/account/api-tokens", model_env="REPLICATE_IMAGE_VERSION",
        hint=("В поле модели — версия модели Replicate", "The model field takes a Replicate version id"),
        fields=(_API_KEY_FIELD, _MODEL_FIELD,
                ProviderField("usdPerImage", ("Цена за изображение, $", "USD per image"), "number")),
    ),
    ProviderSpec(
        "router-image", "IMAGE", ("9Router (изображения)", "9Router (images)"), "ROUTER_API_KEY",
        docs="https://router.webonnix.net", model_env="ROUTER_IMAGE_MODEL",
        default_model="gemini/gemini-2.5-flash-image", shared_secret=True,
        hint=("Картинки через уже подключённые в 9Router аккаунты: "
              "gemini/gemini-3-pro-image-preview (Nano Banana Pro), gemini/gemini-2.5-flash-image. "
              "Ключ берётся из «Настройки → API-ключи → AI Router».",
              "Images through the accounts already connected in 9Router: "
              "gemini/gemini-3-pro-image-preview (Nano Banana Pro), gemini/gemini-2.5-flash-image. "
              "The key comes from Settings → API keys → AI Router."),
        fields=(_MODEL_FIELD,
                ProviderField("apiBase", ("Адрес 9Router", "9Router base URL"),
                              placeholder="http://9router:20128"),
                ProviderField("usdPerImage", ("Цена за изображение, $", "USD per image"), "number")),
    ),
    ProviderSpec(
        "runway", "VIDEO", ("Runway", "Runway"), "RUNWAY_API_KEY",
        docs="https://dev.runwayml.com", model_env="RUNWAY_MODEL",
        fields=(_API_KEY_FIELD, _MODEL_FIELD,
                ProviderField("usdPerSecond", ("Цена за секунду, $", "USD per second"), "number")),
        option_env={"usdPerSecond": "RUNWAY_USD_PER_SECOND"},
    ),
    ProviderSpec(
        "kling", "VIDEO", ("Kling", "Kling"), "KLING_API_KEY",
        docs="https://klingai.com", model_env="KLING_MODEL",
        hint=("Kling подписывает запросы парой access key + secret",
              "Kling signs requests with an access key + secret pair"),
        fields=(
            ProviderField("accessKey", ("Access key ID", "Access key ID"), "secret", required=True),
            ProviderField("apiKey", ("Secret key", "Secret key"), "secret", required=True),
            _MODEL_FIELD,
            ProviderField("usdPerSecond", ("Цена за секунду, $", "USD per second"), "number"),
        ),
        extra_secrets={"accessKey": "KLING_ACCESS_KEY"},
        option_env={"usdPerSecond": "KLING_USD_PER_SECOND"},
    ),
    ProviderSpec(
        "pika", "VIDEO", ("Pika", "Pika"), "PIKA_API_KEY",
        docs="https://pika.art", model_env="PIKA_MODEL",
        fields=(_API_KEY_FIELD, _MODEL_FIELD,
                ProviderField("usdPerSecond", ("Цена за секунду, $", "USD per second"), "number")),
        option_env={"usdPerSecond": "PIKA_USD_PER_SECOND"},
    ),
    ProviderSpec(
        "replicate", "VIDEO", ("Replicate (видео)", "Replicate (video)"), "REPLICATE_API_KEY",
        docs="https://replicate.com/account/api-tokens", model_env="REPLICATE_MODEL",
        fields=(_API_KEY_FIELD, _MODEL_FIELD,
                ProviderField("usdPerSecond", ("Цена за секунду, $", "USD per second"), "number")),
        option_env={"usdPerSecond": "REPLICATE_USD_PER_SECOND"},
    ),
    ProviderSpec(
        "elevenlabs", "VOICE", ("ElevenLabs", "ElevenLabs"), "ELEVENLABS_API_KEY",
        docs="https://elevenlabs.io/app/settings/api-keys", model_env="ELEVENLABS_MODEL",
        default_model="eleven_multilingual_v2",
        hint=("Локальный xtts работает и без ключа", "The local xtts voice works without a key"),
        fields=(_API_KEY_FIELD, _MODEL_FIELD,
                ProviderField("voiceId", ("Voice ID", "Voice ID")),
                ProviderField("usdPer1kChars", ("Цена за 1000 символов, $", "USD per 1k chars"), "number")),
        option_env={"usdPer1kChars": "ELEVENLABS_USD_PER_1K_CHARS"},
    ),
)

CATALOG_BY_ID = {spec.id: spec for spec in CATALOG}

_NUMERIC_OPTIONS = {"usdPerImage", "usdPerSecond", "usdPer1kChars", "concurrency", "timeout"}


@dataclass
class ResolvedProvider:
    api_key: str
    model: str
    config: dict[str, Any]


def get_provider_document() -> dict[str, Any]:
    stored = _load_document(PROVIDER_SETTING_KEY)
    providers = stored.get("providers")
    defaults = stored.get("defaults")
    return {
        "providers": providers if isinstance(providers, dict) else {},
        "defaults": defaults if isinstance(defaults, dict) else {},
    }


def provider_entry(provider_id: str) -> dict[str, Any]:
    entry = get_provider_document()["providers"].get(provider_id)
    return entry if isinstance(entry, dict) else {}


def resolve_media_provider(provider_id: str) -> Optional[ResolvedProvider]:
    """Key, model and options for a provider, or None when it cannot be used.

    None means exactly one thing to the caller — "do not register this provider"
    — and covers both "no key anywhere" and "the owner switched it off". A
    provider that is off must disappear from the registry rather than stay
    registered and fail at the node: a pipeline's fallback chain then skips to
    the next rung instead of burning an attempt.
    """
    spec = CATALOG_BY_ID.get(provider_id)
    if spec is None:
        return None
    entry = provider_entry(provider_id)
    if entry.get("enabled") is False:
        return None

    api_key = read_secret(spec.secret_name)
    if not api_key:
        return None

    model = str(entry.get("model") or "").strip()
    if not model and spec.model_env:
        model = os.environ.get(spec.model_env, "").strip()
    if not model:
        model = spec.default_model

    config: dict[str, Any] = {}
    for key, value in (entry.get("options") or {}).items():
        if key in _NUMERIC_OPTIONS:
            try:
                config[key] = float(value)
            except (TypeError, ValueError):
                continue
        elif str(value).strip():
            config[key] = str(value).strip()
    for option, env_key in spec.option_env.items():
        if option in config:
            continue
        raw = os.environ.get(env_key, "").strip()
        if not raw:
            continue
        try:
            config[option] = float(raw)
        except ValueError:
            logger.warning("Ignoring non-numeric %s=%r", env_key, raw)
    for field_name, secret_name in spec.extra_secrets.items():
        secret = read_secret(secret_name)
        if secret:
            config[field_name] = secret
    return ResolvedProvider(api_key=api_key, model=model, config=config)


def list_media_providers() -> list[dict[str, Any]]:
    """One row per connectable provider — connected or not (§62). No secret is
    ever in it; a key is reported only as where it came from."""
    from backend.content_factory.providers.registry import get_registry

    registry = get_registry()
    document = get_provider_document()
    rows: list[dict[str, Any]] = []
    for spec in CATALOG:
        entry = document["providers"].get(spec.id) or {}
        sources = {"apiKey": secret_source(spec.secret_name)}
        for field_name, secret_name in spec.extra_secrets.items():
            sources[field_name] = secret_source(secret_name)
        rows.append(
            {
                "id": spec.id,
                "category": spec.category,
                "label": {"ru": spec.label[0], "en": spec.label[1]},
                "hint": {"ru": spec.hint[0], "en": spec.hint[1]},
                "docs": spec.docs,
                "fields": [
                    {
                        "name": item.name,
                        "label": {"ru": item.label[0], "en": item.label[1]},
                        "type": item.type,
                        "required": item.required,
                        "placeholder": item.placeholder,
                    }
                    for item in spec.fields
                ],
                "enabled": entry.get("enabled", True),
                "model": entry.get("model") or "",
                "options": entry.get("options") or {},
                "secrets": sources,
                "configured": sources["apiKey"] != "none",
                # Whether it is in the registry *right now*, which is what a
                # pipeline would actually get.
                "active": registry.get(spec.id) is not None,
                "isDefault": (registry.default_for(spec.category).id if registry.default_for(spec.category) else "")
                == spec.id,
            }
        )
    return rows


def save_media_provider(provider_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Store one provider's key, model and options, then rebuild the registry."""
    spec = CATALOG_BY_ID.get(provider_id)
    if spec is None:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"Unknown provider: {provider_id}", retryable=False
        )
    patch = patch or {}
    document = get_provider_document()
    entry = dict(document["providers"].get(provider_id) or {})

    if "enabled" in patch:
        entry["enabled"] = bool(patch["enabled"])
    if "model" in patch:
        entry["model"] = str(patch.get("model") or "").strip()

    options = dict(entry.get("options") or {})
    known_options = {item.name for item in spec.fields if item.name not in {"apiKey", *spec.extra_secrets}}
    for key, value in (patch.get("options") or {}).items():
        if key not in known_options and key not in _NUMERIC_OPTIONS:
            continue
        if value in (None, ""):
            options.pop(key, None)
        elif key in _NUMERIC_OPTIONS:
            try:
                options[key] = float(value)
            except (TypeError, ValueError):
                raise ContentFactoryError(
                    ErrorCode.VALIDATION_FAILED, f"{key} must be a number", retryable=False
                ) from None
        else:
            options[key] = str(value).strip()
    entry["options"] = options
    entry.setdefault("enabled", True)

    # A borrowed key is read-only from here for the same reason it is not
    # cleared on delete: this screen is not where the AI Router's own key is
    # managed (Настройки → API-ключи is), and letting it be overwritten from
    # two places is how the two silently disagree.
    if "apiKey" in patch and not spec.shared_secret:
        value = patch["apiKey"]
        if value is None:
            clear_secret(spec.secret_name)
        elif str(value).strip():
            write_secret(spec.secret_name, str(value))
    for field_name, secret_name in spec.extra_secrets.items():
        if field_name not in patch:
            continue
        value = patch[field_name]
        if value is None:
            clear_secret(secret_name)
        elif str(value).strip():
            write_secret(secret_name, str(value))

    document["providers"][provider_id] = entry
    _save_document(PROVIDER_SETTING_KEY, document)
    reload_registry()
    return next(row for row in list_media_providers() if row["id"] == provider_id)


def delete_media_provider(provider_id: str) -> None:
    """Forget the key and the configuration. An env-provided key survives this
    (it is not ours to delete) and the row stays in the catalog as connectable.

    A `shared_secret` provider keeps its key too: it never owned it. Clearing
    ROUTER_API_KEY because somebody disconnected image generation would take the
    agent fallback chain down with it.
    """
    spec = CATALOG_BY_ID.get(provider_id)
    if spec is None:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"Unknown provider: {provider_id}", retryable=False
        )
    if not spec.shared_secret:
        clear_secret(spec.secret_name)
        for secret_name in spec.extra_secrets.values():
            clear_secret(secret_name)
    document = get_provider_document()
    document["providers"].pop(provider_id, None)
    for category, current in list(document["defaults"].items()):
        if current == provider_id:
            document["defaults"].pop(category)
    _save_document(PROVIDER_SETTING_KEY, document)
    reload_registry()


def set_category_default(category: str, provider_id: str) -> dict[str, Any]:
    """Which provider a node of this type gets when it does not name one.

    Two kinds of id are valid, because two kinds of provider exist. Media
    vendors come from `CATALOG` — a fixed list this module owns. Language
    models do not: `local-llm` plus one provider per approved
    `provider_bindings` row, whose ids are generated at binding time. Checking
    only `CATALOG` (which is what this did) silently made "use the external
    model by default" unreachable — the LLM category could never be pointed at
    anything but whatever `_load_llm` happened to register first, i.e. always
    the local model. So an id that is not in the catalog is accepted when the
    live registry has it under the same category.
    """
    document = get_provider_document()
    if provider_id:
        spec = CATALOG_BY_ID.get(provider_id)
        if spec is not None and spec.category != category:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED,
                f"{provider_id} is not a {category} provider",
                retryable=False,
            )
        if spec is None:
            from backend.content_factory.providers.registry import get_registry

            provider = get_registry().get(provider_id)
            if provider is None or provider.category != category:
                raise ContentFactoryError(
                    ErrorCode.VALIDATION_FAILED,
                    f"{provider_id} is not a {category} provider",
                    retryable=False,
                )
        document["defaults"][category] = provider_id
    else:
        document["defaults"].pop(category, None)
    _save_document(PROVIDER_SETTING_KEY, document)
    reload_registry()
    return document["defaults"]


def category_defaults() -> dict[str, str]:
    return {
        str(category): str(provider_id)
        for category, provider_id in get_provider_document()["defaults"].items()
        if provider_id
    }


async def test_media_provider(provider_id: str) -> dict[str, Any]:
    """The provider's own health check — credential resolution, never a billable
    generation (§90). A provider that is not registered says so rather than
    raising, because "not connected" is the answer the screen is asking for."""
    from backend.content_factory.providers.registry import get_registry

    provider = get_registry().get(provider_id)
    if provider is None:
        return {"provider": provider_id, "status": "not_configured"}
    try:
        return await provider.health()
    except Exception as exc:
        return {"provider": provider_id, "status": "error", "detail": type(exc).__name__}


def reload_registry() -> None:
    from backend.content_factory.providers.registry import set_registry_for_tests

    set_registry_for_tests(None)


# ── External LLM endpoints (governed, §63) ───────────────────────────────────

_BINDING_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


def list_llm_endpoints() -> dict[str, Any]:
    """Local model + every `provider_bindings` row, with its approval state.

    External LLMs stay in `provider_governance`: a key there is written to
    Valkey under a TTL and only becomes permanent when an owner approves the
    R3 task. This screen shows that state and starts the flow; it does not
    become a second, ungoverned way to activate a model.
    """
    from backend.content_factory.providers.llm import LOCAL_PROVIDER_ID

    local = {
        "id": LOCAL_PROVIDER_ID,
        "name": "local-llm",
        "api_base": (
            os.environ.get("LLAMACPP_BASE_URL") or "http://hermes-llama:8080"
        ).rstrip("/"),
        "default_model": os.environ.get("LLM_MODEL", "qwen3.8-27b"),
        "status": "active",
        "local": True,
    }
    try:
        from backend.provider_governance import list_bindings

        bindings = [{**binding, "local": False} for binding in list_bindings()]
    except Exception:
        logger.warning("provider_governance unavailable — external LLM list is empty")
        bindings = []
    return {"local": local, "bindings": bindings}


def create_llm_endpoint(
    name: str,
    api_base: str,
    api_key: str,
    *,
    default_model: str = "",
    cost_per_1m_input: Optional[float] = None,
    cost_per_1m_output: Optional[float] = None,
) -> dict[str, Any]:
    """Propose an external OpenAI-compatible endpoint. Returns the binding and
    the control task that has to be approved before it can be used."""
    if not _BINDING_NAME.fullmatch(str(name or "").strip().lower()):
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            "Имя провайдера: строчные латинские буквы, цифры, дефис или подчёркивание",
            retryable=False,
        )
    try:
        from backend.provider_governance import create_binding_proposal

        proposal = create_binding_proposal(
            name, "openai_compatible", api_base, api_key, cost_per_1m_input, cost_per_1m_output,
            default_model=str(default_model or "").strip(),
        )
    except ValueError as exc:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, str(exc), retryable=False) from exc

    reload_registry()
    return proposal


async def list_llm_endpoint_models(binding_id: str) -> dict[str, Any]:
    """`GET {api_base}/models` on an approved binding, as the OpenAI-compatible
    contract defines it — which is what 9Router, OpenRouter, DeepSeek, vLLM and
    llama.cpp all answer.

    Never raises for an endpoint that simply does not answer: the caller is a
    picker, and "I could not list them" has to degrade to typing the name by
    hand rather than to an error dialog over a settings screen.
    """
    from backend.provider_governance import resolve_binding_credentials

    resolved = resolve_binding_credentials(binding_id)
    if not resolved:
        raise ContentFactoryError(
            ErrorCode.PROVIDER_AUTH,
            "Провайдер не активен — модели недоступны",
            retryable=False,
            details={"providerId": binding_id},
        )
    api_base, api_key = resolved

    import httpx

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(
                f"{api_base.rstrip('/')}/models",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
    except httpx.HTTPError as exc:
        logger.warning("Model list for binding %s failed: %s", binding_id, exc)
        return {"models": [], "error": "endpoint unreachable"}

    if response.status_code != 200:
        detail = "invalid API key" if response.status_code in (401, 403) else f"HTTP {response.status_code}"
        return {"models": [], "error": f"GET /models failed: {detail}"}

    try:
        entries = response.json().get("data") or []
    except ValueError:
        return {"models": [], "error": "endpoint returned a non-JSON model list"}

    models: list[str] = []
    for entry in entries:
        model_id = entry.get("id") if isinstance(entry, dict) else None
        if model_id and model_id not in models:
            models.append(str(model_id))
    models.sort()
    return {"models": models, "error": None}


def set_llm_endpoint_model(binding_id: str, model: str) -> dict[str, Any]:
    """Change which model an already-approved endpoint answers with.

    Separate from creation because the two are separate in practice: a binding
    can arrive from anywhere (POST /api/router/bind-self, the agent admin form,
    an older build that had no model field at all) and then needs a model named
    before Content Factory can route a node to it.
    """
    from backend.provider_governance import update_binding_model

    try:
        binding = update_binding_model(binding_id, model)
    except KeyError as exc:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "Провайдер не найден", retryable=False
        ) from exc
    except ValueError as exc:
        raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, str(exc), retryable=False) from exc
    reload_registry()
    return binding


def delete_llm_endpoint(binding_id: str) -> None:
    from backend.provider_governance import delete_binding

    try:
        delete_binding(binding_id)
    except KeyError as exc:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "Провайдер не найден", retryable=False
        ) from exc
    # Same cleanup delete_provider does for a media vendor: a default naming a
    # binding that no longer exists is ignored at load time anyway, but leaving
    # it stored means the screen keeps reporting a default that cannot answer.
    document = get_provider_document()
    if any(current == binding_id for current in document["defaults"].values()):
        for category, current in list(document["defaults"].items()):
            if current == binding_id:
                document["defaults"].pop(category)
        _save_document(PROVIDER_SETTING_KEY, document)
    reload_registry()


# ── Settings document plumbing ───────────────────────────────────────────────

def _load_document(key: str) -> dict[str, Any]:
    from backend.database import get_setting

    try:
        raw = get_setting(key)
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except (TypeError, ValueError):
        logger.warning("%s is unreadable; falling back to defaults", key)
    return {}


def _save_document(key: str, document: dict[str, Any]) -> None:
    from backend.database import set_setting

    set_setting(key, json.dumps(document, ensure_ascii=False))
