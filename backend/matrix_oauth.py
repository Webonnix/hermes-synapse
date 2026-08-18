"""OAuth 2.0 device-code login for MAS-backed Matrix homeservers.

Modern homeservers (matrix.senla.eu, every Element-hosted/etke.cc server, and
anything running MSC3861) delegate authentication to a Matrix Authentication
Service. MAS issues `mat_`-prefixed access tokens that expire *by design* within
minutes — the access token Element shows under Settings → Help & About →
Advanced is one of those, so pasting it into any other client buys roughly five
minutes of uptime before `M_UNKNOWN_TOKEN` kills the session. That is the whole
reason a hand-pasted Matrix binding "keeps falling off".

The fix is to hold a *refreshable* credential instead of a snapshot of one. This
module implements the same login Element itself uses on such servers — RFC 8628
device authorization grant, on a client registered on the fly via RFC 7591 —
which hands back an access_token + refresh_token pair we can rotate forever
without the owner ever pasting a token again.

Everything here is deliberately plain httpx: matrix-nio (0.26) has no OAuth or
refresh-token support at all.
"""

from __future__ import annotations

import json
import logging
import random
import string
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("hermes.matrix_oauth")

# MSC2967 scopes: full client-API access, bound to one device id we own.
_API_SCOPE = "urn:matrix:org.matrix.msc2967.client:api:*"
_DEVICE_SCOPE_PREFIX = "urn:matrix:org.matrix.msc2967.client:device:"

_CLIENT_NAME = "Hermes Agent Bridge"
_CLIENT_URI = "https://hermes.webonnix.net/"

_FLOW_KEY_PREFIX = "matrix_device_login:"
_FLOW_TTL_SECONDS = 1800
_CLIENT_KEY_PREFIX = "matrix_oauth_client:"

_HTTP_TIMEOUT = 15.0
# Renew this many seconds before the server's stated expiry, so a slow refresh
# never races the token actually dying mid-sync.
REFRESH_MARGIN_SECONDS = 60


def _new_device_id() -> str:
    """Element-style 10-char device id — MAS scopes the grant to exactly this one."""
    return "".join(random.choices(string.ascii_uppercase, k=10))


def resolve_homeserver_base_url(homeserver_url: str) -> str:
    """Follows .well-known delegation, so "senla.eu" (what the user id says) ends
    up at https://matrix.senla.eu (where the API actually lives). Falls back to
    the given value whenever the server publishes no delegation."""
    raw = (homeserver_url or "").strip().rstrip("/")
    if not raw:
        raise ValueError("Homeserver URL пуст")
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            response = client.get(f"{raw}/.well-known/matrix/client")
        if response.status_code == 200:
            base_url = (response.json().get("m.homeserver") or {}).get("base_url")
            if base_url:
                return str(base_url).rstrip("/")
    except Exception:
        logger.debug("No .well-known delegation for %s", raw, exc_info=True)
    return raw


def discover_auth_metadata(homeserver_url: str) -> Optional[dict[str, Any]]:
    """Returns the homeserver's OAuth metadata, or None if it is not MAS-backed
    (i.e. a classic Synapse where password login is the only option)."""
    base = homeserver_url.rstrip("/")
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT, follow_redirects=True) as client:
            for path in (
                "/_matrix/client/v1/auth_metadata",
                "/_matrix/client/unstable/org.matrix.msc2965/auth_metadata",
            ):
                response = client.get(f"{base}{path}")
                if response.status_code == 200:
                    metadata = response.json()
                    if metadata.get("token_endpoint"):
                        return metadata
    except Exception:
        logger.exception("Matrix OAuth discovery failed for %s", homeserver_url)
    return None


def _register_client(metadata: dict[str, Any], homeserver_url: str) -> str:
    """RFC 7591 dynamic registration, cached per homeserver — MAS has no
    pre-shared client id for third-party bridges, exactly like Element itself
    registers itself on first launch."""
    from backend.valkey_client import get_value, set_value

    host = urlparse(homeserver_url.rstrip("/")).netloc or homeserver_url
    cache_key = f"{_CLIENT_KEY_PREFIX}{host}"
    cached = get_value(cache_key)
    if cached:
        return cached

    registration_endpoint = metadata.get("registration_endpoint")
    if not registration_endpoint:
        raise ValueError("This homeserver does not allow dynamic client registration")
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        response = client.post(
            registration_endpoint,
            json={
                "client_name": _CLIENT_NAME,
                "client_uri": _CLIENT_URI,
                "application_type": "native",
                "token_endpoint_auth_method": "none",
                "grant_types": ["urn:ietf:params:oauth:grant-type:device_code", "refresh_token"],
                "response_types": [],
            },
        )
    if response.status_code not in (200, 201):
        raise ValueError(f"Homeserver refused client registration ({response.status_code}): {response.text[:200]}")
    client_id = response.json().get("client_id")
    if not client_id:
        raise ValueError("Homeserver returned no client_id")
    set_value(cache_key, client_id, ttl_seconds=None)
    return client_id


def start_device_login(homeserver_url: str) -> dict[str, Any]:
    """Kicks off the device grant. Returns the code/URL the owner has to confirm
    in a browser, plus a flow_id to poll with — the device_code and client_id
    stay server-side in Valkey and never reach the browser."""
    homeserver_url = resolve_homeserver_base_url(homeserver_url)
    metadata = discover_auth_metadata(homeserver_url)
    if not metadata:
        raise ValueError(
            "Этот сервер не использует Matrix Authentication Service — войдите по логину и паролю."
        )
    device_authorization_endpoint = metadata.get("device_authorization_endpoint")
    if not device_authorization_endpoint:
        raise ValueError("Этот сервер не поддерживает вход по коду устройства (device grant)")

    client_id = _register_client(metadata, homeserver_url)
    device_id = _new_device_id()
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        response = client.post(
            device_authorization_endpoint,
            data={"client_id": client_id, "scope": f"{_API_SCOPE} {_DEVICE_SCOPE_PREFIX}{device_id}"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code not in (200, 201):
        raise ValueError(f"Не удалось начать вход ({response.status_code}): {response.text[:200]}")
    payload = response.json()

    flow_id = f"mxdev-{uuid.uuid4().hex[:12]}"
    verification_uri = payload.get("verification_uri", "")
    user_code = payload.get("user_code", "")
    flow = {
        "flow_id": flow_id,
        "homeserver_url": homeserver_url.rstrip("/"),
        "client_id": client_id,
        "device_id": device_id,
        "device_code": payload["device_code"],
        "token_endpoint": metadata["token_endpoint"],
        "interval": int(payload.get("interval", 5)),
        "expires_at": time.time() + int(payload.get("expires_in", 900)),
        # Kept so the code can be shown again on every poll. It used to live only
        # in the one HTTP response that started the flow, which meant a reloaded
        # or scrolled-away page lost the code for good with no way to recover it.
        "user_code": user_code,
        "verification_uri": verification_uri,
        "verification_uri_complete": payload.get("verification_uri_complete")
        or (f"{verification_uri}?code={user_code}" if verification_uri else ""),
    }
    from backend.valkey_client import set_value

    set_value(f"{_FLOW_KEY_PREFIX}{flow_id}", json.dumps(flow), ttl_seconds=_FLOW_TTL_SECONDS)
    return {
        "flow_id": flow_id,
        **public_flow_details(flow),
        "interval": flow["interval"],
        "expires_in": int(payload.get("expires_in", 900)),
    }


def public_flow_details(flow: dict[str, Any]) -> dict[str, Any]:
    """The parts of a pending flow that are safe to hand to the browser — the
    code the owner types and where to type it. Never the device_code, which is
    the bearer secret that redeems the login."""
    return {
        "user_code": flow.get("user_code", ""),
        "verification_uri": flow.get("verification_uri", ""),
        "verification_uri_complete": flow.get("verification_uri_complete", ""),
        "expires_in": max(0, round(float(flow.get("expires_at", 0)) - time.time())),
    }


def describe_flow(flow_id: str) -> dict[str, Any]:
    """Re-reads a pending flow so the dashboard can redisplay its code."""
    return public_flow_details(_load_flow(flow_id))


def _load_flow(flow_id: str) -> dict[str, Any]:
    from backend.valkey_client import get_value

    raw = get_value(f"{_FLOW_KEY_PREFIX}{flow_id}")
    if not raw:
        raise ValueError("Сессия входа истекла — начните вход заново")
    return json.loads(raw)


def poll_device_login(flow_id: str) -> Optional[dict[str, Any]]:
    """One poll of the token endpoint. Returns None while the owner has not
    confirmed yet, or the finished credentials dict once they have."""
    flow = _load_flow(flow_id)
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        response = client.post(
            flow["token_endpoint"],
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": flow["device_code"],
                "client_id": flow["client_id"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code in (400, 401, 403):
        error = ""
        try:
            error = response.json().get("error", "")
        except Exception:
            error = response.text[:200]
        if error in ("authorization_pending", "slow_down"):
            return None
        if error == "expired_token":
            raise ValueError("Код подтверждения истёк — начните вход заново")
        if error == "access_denied":
            raise ValueError("Вход отклонён в браузере")
        raise ValueError(f"Сервер отклонил вход: {error}")
    if response.status_code != 200:
        raise ValueError(f"Сервер вернул {response.status_code}: {response.text[:200]}")

    payload = response.json()
    credentials = {
        "homeserver_url": flow["homeserver_url"],
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", ""),
        "auth_kind": "oauth",
        "oauth_client_id": flow["client_id"],
        "token_endpoint": flow["token_endpoint"],
        "device_id": flow["device_id"],
        "expires_at": time.time() + int(payload.get("expires_in", 300)),
    }
    credentials["user_id"] = whoami(flow["homeserver_url"], credentials["access_token"])

    from backend.valkey_client import delete_value

    delete_value(f"{_FLOW_KEY_PREFIX}{flow_id}")
    if not credentials["refresh_token"]:
        # Nothing to rotate later — better to say so now than to have the binding
        # die silently in five minutes like a pasted token would.
        raise ValueError("Сервер не выдал refresh_token — автопродление невозможно")
    return credentials


def whoami(homeserver_url: str, access_token: str) -> str:
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        response = client.get(
            f"{homeserver_url.rstrip('/')}/_matrix/client/v3/account/whoami",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if response.status_code != 200:
        raise ValueError(f"Matrix отклонил токен доступа: {response.text[:200]}")
    return response.json()["user_id"]


def password_login(homeserver_url: str, user_id: str, password: str) -> dict[str, Any]:
    """Legacy/compat login, but asking for a refresh token (`refresh_token: true`)
    — matrix-nio's own login() never does, which is why a password-based binding
    used to die just as fast as a pasted token on a MAS homeserver."""
    homeserver_url = resolve_homeserver_base_url(homeserver_url)
    localpart = user_id[1:].split(":")[0] if user_id.startswith("@") else user_id
    with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
        response = client.post(
            f"{homeserver_url.rstrip('/')}/_matrix/client/v3/login",
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": localpart},
                "password": password,
                "initial_device_display_name": _CLIENT_NAME,
                "refresh_token": True,
            },
        )
    if response.status_code != 200:
        detail = response.text[:200]
        try:
            detail = response.json().get("error", detail)
        except Exception:
            pass
        raise ValueError(f"Matrix login failed: {detail}")
    payload = response.json()
    credentials: dict[str, Any] = {
        "homeserver_url": homeserver_url.rstrip("/"),
        "user_id": payload["user_id"],
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", ""),
        "auth_kind": "password",
        "device_id": payload.get("device_id", ""),
    }
    if payload.get("expires_in_ms"):
        credentials["expires_at"] = time.time() + int(payload["expires_in_ms"]) / 1000
    return credentials


def refresh_credentials(credentials: dict[str, Any]) -> dict[str, Any]:
    """Exchanges the stored refresh_token for a fresh access/refresh pair.

    Two different endpoints, because there are two different kinds of credential:
    an OAuth device-grant token renews at the MAS token endpoint, a compat-login
    token at Matrix's own /refresh. Both rotate the refresh token, so the result
    must always be persisted — losing it costs the binding its session.
    """
    refresh_token = (credentials or {}).get("refresh_token")
    if not refresh_token:
        raise ValueError("У этого подключения нет refresh_token")

    updated = dict(credentials)
    if credentials.get("auth_kind") == "oauth":
        token_endpoint = credentials.get("token_endpoint")
        client_id = credentials.get("oauth_client_id")
        if not token_endpoint or not client_id:
            raise ValueError("OAuth-подключение без token_endpoint/client_id")
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.post(
                token_endpoint,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    else:
        homeserver_url = credentials.get("homeserver_url", "")
        if not homeserver_url:
            raise ValueError("Подключение без homeserver_url")
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            response = client.post(
                f"{homeserver_url.rstrip('/')}/_matrix/client/v3/refresh",
                json={"refresh_token": refresh_token},
            )
    if response.status_code != 200:
        raise ValueError(f"Обновление токена отклонено ({response.status_code}): {response.text[:200]}")

    payload = response.json()
    updated["access_token"] = payload["access_token"]
    updated["refresh_token"] = payload.get("refresh_token", refresh_token)
    if payload.get("expires_in_ms"):
        updated["expires_at"] = time.time() + int(payload["expires_in_ms"]) / 1000
    elif payload.get("expires_in"):
        updated["expires_at"] = time.time() + int(payload["expires_in"])
    else:
        updated.pop("expires_at", None)
    return updated


def seconds_until_refresh(credentials: dict[str, Any]) -> Optional[float]:
    """How long a live client may keep using this access token before renewing,
    or None when the credential carries no expiry (classic Synapse token)."""
    expires_at = (credentials or {}).get("expires_at")
    if not expires_at or not credentials.get("refresh_token"):
        return None
    return max(5.0, float(expires_at) - time.time() - REFRESH_MARGIN_SECONDS)
