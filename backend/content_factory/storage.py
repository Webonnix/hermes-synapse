"""Asset storage (§44–46).

Binary media never goes into the database and never streams through the API
(§46): the backend hands out time-limited URLs and the bytes travel directly
between the client (or a platform's uploader) and the store.

Three implementations behind one interface:

* `NextcloudStorage` — the production store on this deployment. WebDAV for
  reads and writes, and the OCS share API for the time-limited public links a
  platform needs in order to pull a finished video.
* `S3Storage` — any S3-compatible endpoint (AWS, Backblaze B2, Cloudflare R2),
  kept because the interface is identical and moving to one is then a
  configuration change rather than a rewrite.
* `LocalStorage` — a directory on disk, used by tests and by a developer
  machine with nothing configured. It signs URLs with an HMAC of the key and an
  expiry so the *shape* of the flow (request a URL, use it, watch it expire) is
  identical in all three, and a bug in that flow shows up in tests rather than
  only in production.

Two honest differences between Nextcloud and S3, both handled rather than
hidden:

* Nextcloud has no pre-signed *upload*. Browser uploads therefore go through
  the backend's own signed endpoint (as with `LocalStorage`) instead of
  directly to the store — a deviation from §46 that only affects operator
  uploads, not the pipeline, which writes server-side.
* A public share link is a real object on the server, not a signature. They are
  created once per asset, reused, and given an expiry; the share is what an
  external platform fetches, so the Nextcloud instance has to be reachable from
  the internet for YouTube/TikTok publishing to work at all.

Keys follow the §44 convention exactly:

    content-factory/projects/{projectId}/runs/{runId}/{kind}/{filename}

so everything one run produced can be listed, archived or deleted by prefix,
and an object's path alone says which run paid for it.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from backend.content_factory.domain import ContentFactoryError, ErrorCode

logger = logging.getLogger("hermes.content_factory.storage")

ROOT_PREFIX = "content-factory"

# Delegated Graph permissions the OneDrive backend needs. `offline_access` is
# what makes a refresh token possible at all — without it the sign-in is good
# for one hour and the store dies silently after that.
ONEDRIVE_SCOPES = "offline_access Files.ReadWrite"
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")

# Asset type → the §44 folder it lives in.
_FOLDERS = {
    "DOCUMENT": "research",
    "IMAGE": "images",
    "VIDEO": "clips",
    "AUDIO": "audio",
    "VOICE": "audio",
    "MUSIC": "audio",
    "SUBTITLE": "subtitles",
    "THUMBNAIL": "thumbnails",
    "RENDER": "renders",
}


def build_key(project_id: str, run_id: str, asset_type: str, filename: str) -> str:
    folder = _FOLDERS.get(asset_type.upper(), "misc")
    return "/".join(
        [
            ROOT_PREFIX,
            "projects",
            _segment(project_id or "unassigned"),
            "runs",
            _segment(run_id or "adhoc"),
            folder,
            _segment(filename or "asset.bin"),
        ]
    )


def _segment(value: str) -> str:
    """One path segment: no traversal, no separators, never empty."""
    cleaned = _SAFE_SEGMENT.sub("-", str(value).strip()).strip("-.")
    return cleaned[:120] or "unnamed"


@dataclass
class StoredObject:
    key: str
    size_bytes: int
    checksum: str
    mime_type: str = ""


class AssetStorage:
    """Interface both backends implement."""

    name = ""

    def put(self, key: str, data: bytes, *, mime_type: str = "") -> StoredObject:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def presigned_get(self, key: str, *, expires_in: int = 3600) -> str:
        raise NotImplementedError

    def presigned_put(self, key: str, *, expires_in: int = 3600, mime_type: str = "") -> dict[str, Any]:
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        return {"storage": self.name, "status": "unknown"}


class NextcloudStorage(AssetStorage):
    """WebDAV for bytes, OCS share links for external access.

    Credentials must be a Nextcloud *app password*, not the account password:
    an app password can be revoked on its own and cannot be used to log into
    the web UI, which is the difference between one compromised integration and
    one compromised account.
    """

    name = "nextcloud"

    def __init__(
        self,
        base_url: str,
        *,
        username: str,
        password: str,
        root: str = "",
        verify_tls: bool = True,
        timeout: float = 120.0,
        share_expire_days: int = 7,
        public_base_url: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self._password = password
        self.root = root.strip("/")
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.share_expire_days = max(1, int(share_expire_days))
        # What an external platform should use, when the instance is reached
        # under a different name from outside (reverse proxy, split DNS).
        self.public_base_url = (public_base_url or base_url).rstrip("/")
        self._known_collections: set[str] = set()
        self._share_cache: dict[str, tuple[str, float]] = {}

    # ── WebDAV ───────────────────────────────────────────────────────────
    def _dav_url(self, key: str) -> str:
        path = "/".join(part for part in [self.root, key] if part)
        return f"{self.base_url}/remote.php/dav/files/{quote(self.username)}/{quote(path)}"

    def _client(self):
        import httpx

        return httpx.Client(
            auth=(self.username, self._password),
            timeout=self.timeout,
            verify=self.verify_tls,
            follow_redirects=True,
        )

    def _ensure_collections(self, client: Any, key: str) -> None:
        """WebDAV refuses a PUT into a collection that does not exist, and has
        no `mkdir -p`, so each level is created in turn. Already-created levels
        are remembered for the process's lifetime — a run writes dozens of
        assets under the same prefix."""
        segments = [part for part in "/".join([self.root, key]).split("/")[:-1] if part]
        path = ""
        for segment in segments:
            path = f"{path}/{segment}" if path else segment
            if path in self._known_collections:
                continue
            url = f"{self.base_url}/remote.php/dav/files/{quote(self.username)}/{quote(path)}"
            response = client.request("MKCOL", url)
            # 201 created, 405 already there — both mean the collection exists.
            if response.status_code not in (201, 405):
                raise _storage_error(
                    RuntimeError(f"MKCOL {path} returned {response.status_code}")
                )
            self._known_collections.add(path)

    def put(self, key: str, data: bytes, *, mime_type: str = "") -> StoredObject:
        checksum = hashlib.sha256(data).hexdigest()
        try:
            with self._client() as client:
                self._ensure_collections(client, key)
                response = client.put(
                    self._dav_url(key),
                    content=data,
                    headers={"Content-Type": mime_type or "application/octet-stream"},
                )
                if response.status_code not in (200, 201, 204):
                    raise RuntimeError(f"PUT returned {response.status_code}")
        except Exception as exc:
            raise _storage_error(exc) from exc
        return StoredObject(key=key, size_bytes=len(data), checksum=checksum, mime_type=mime_type)

    def get(self, key: str) -> bytes:
        try:
            with self._client() as client:
                response = client.get(self._dav_url(key))
                if response.status_code == 404:
                    raise ContentFactoryError(
                        ErrorCode.VALIDATION_FAILED, f"No such asset: {key}", retryable=False
                    )
                response.raise_for_status()
                return response.content
        except ContentFactoryError:
            raise
        except Exception as exc:
            raise _storage_error(exc) from exc

    def delete(self, key: str) -> None:
        try:
            with self._client() as client:
                response = client.delete(self._dav_url(key))
                if response.status_code not in (200, 204, 404):
                    raise RuntimeError(f"DELETE returned {response.status_code}")
        except Exception as exc:
            raise _storage_error(exc) from exc
        self._share_cache.pop(key, None)

    def exists(self, key: str) -> bool:
        try:
            with self._client() as client:
                return client.head(self._dav_url(key)).status_code < 400
        except Exception:
            return False

    # ── OCS public shares ────────────────────────────────────────────────
    def presigned_get(self, key: str, *, expires_in: int = 3600) -> str:
        """A public download link, created once per asset and reused.

        Nextcloud expiry is per-day, so `expires_in` is rounded up to whole
        days and never below one: a link that expires the same hour it is
        created would break a platform that fetches the file minutes later.
        """
        cached = self._share_cache.get(key)
        if cached and cached[1] > time.time() + 300:
            return cached[0]

        days = max(1, min(self.share_expire_days, int((expires_in + 86399) // 86400)))
        share_url = self._create_share(key, days)
        expires_at = time.time() + days * 86400
        self._share_cache[key] = (share_url, expires_at)
        return share_url

    def _create_share(self, key: str, days: int) -> str:
        from datetime import date, timedelta

        path = "/" + "/".join(part for part in [self.root, key] if part)
        payload = {
            "path": path,
            "shareType": 3,  # public link
            "permissions": 1,  # read only
            "expireDate": (date.today() + timedelta(days=days)).isoformat(),
        }
        try:
            with self._client() as client:
                response = client.post(
                    f"{self.base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares",
                    data=payload,
                    headers={"OCS-APIRequest": "true", "Accept": "application/json"},
                )
                response.raise_for_status()
                body = response.json()
        except Exception as exc:
            raise _storage_error(exc) from exc

        data = ((body.get("ocs") or {}).get("data")) or {}
        url = str(data.get("url") or "")
        if not url:
            meta = (body.get("ocs") or {}).get("meta") or {}
            raise ContentFactoryError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                f"Nextcloud refused to create a share link: {meta.get('message', 'unknown error')}",
                retryable=False,
            )
        if self.public_base_url and url.startswith(self.base_url):
            url = self.public_base_url + url[len(self.base_url):]
        # `/download` is what turns a share *page* into the file itself, which
        # is what an uploader on the other end needs.
        return f"{url.rstrip('/')}/download"

    def presigned_put(self, key: str, *, expires_in: int = 3600, mime_type: str = "") -> dict[str, Any]:
        """Nextcloud has no pre-signed upload, so browser uploads are proxied
        through the backend's own signed endpoint. The pipeline itself never
        uses this path — it writes with `put()` server-side."""
        expires_at = int(time.time()) + expires_in
        signature = hmac.new(
            _upload_secret(), f"{key}:{expires_at}".encode(), hashlib.sha256
        ).hexdigest()
        return {
            "url": (
                f"/api/v1/content-factory/assets/upload?key={quote(key)}"
                f"&expires={expires_at}&signature={signature}"
            ),
            "method": "PUT",
            "key": key,
            "proxied": True,
            "headers": {"Content-Type": mime_type or "application/octet-stream"},
        }

    def health(self) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.request(
                    "PROPFIND",
                    f"{self.base_url}/remote.php/dav/files/{quote(self.username)}/",
                    headers={"Depth": "0"},
                )
            connected = response.status_code < 400
            return {
                "storage": self.name,
                "status": "connected" if connected else "error",
                "baseUrl": self.base_url,
                "root": self.root,
            }
        except Exception as exc:
            return {"storage": self.name, "status": "error", "detail": type(exc).__name__}


class S3Storage(AssetStorage):
    name = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str = "",
        region: str = "",
        access_key: str = "",
        secret_key: str = "",
        public_base_url: str = "",
    ):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # optional dependency group
            raise ContentFactoryError(
                ErrorCode.INTERNAL,
                "S3 storage requires boto3 (uv sync --group storage)",
                retryable=False,
            ) from exc

        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region or None,
            aws_access_key_id=access_key or None,
            aws_secret_access_key=secret_key or None,
            # Path-style addressing works on every S3-compatible endpoint,
            # including ones whose wildcard TLS certificate does not cover
            # bucket-name subdomains.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def put(self, key: str, data: bytes, *, mime_type: str = "") -> StoredObject:
        checksum = hashlib.sha256(data).hexdigest()
        try:
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=mime_type or "application/octet-stream",
                Metadata={"sha256": checksum},
            )
        except Exception as exc:
            raise _storage_error(exc) from exc
        return StoredObject(key=key, size_bytes=len(data), checksum=checksum, mime_type=mime_type)

    def get(self, key: str) -> bytes:
        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except Exception as exc:
            raise _storage_error(exc) from exc

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise _storage_error(exc) from exc

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def presigned_get(self, key: str, *, expires_in: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_in
        )

    def presigned_put(self, key: str, *, expires_in: int = 3600, mime_type: str = "") -> dict[str, Any]:
        url = self._client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentType": mime_type or "application/octet-stream",
            },
            ExpiresIn=expires_in,
        )
        return {"url": url, "method": "PUT", "key": key, "headers": {"Content-Type": mime_type or "application/octet-stream"}}

    def health(self) -> dict[str, Any]:
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return {"storage": self.name, "status": "connected", "bucket": self.bucket}
        except Exception as exc:
            return {"storage": self.name, "status": "error", "detail": type(exc).__name__}


class OneDriveStorage(AssetStorage):
    """Microsoft Graph, personal or work OneDrive.

    Two things make it different from the other three, and both are handled
    here rather than pushed onto the caller:

    * **The credential is an OAuth refresh token, not a password.** Microsoft
      issues short-lived access tokens (about an hour) and a rotating refresh
      token; a token pasted into a settings field would stop working the same
      day. So the Storage screen runs a device-code login once
      (`integrations.start_onedrive_login`), the refresh token is what gets
      stored, and this class exchanges it for an access token on demand and
      writes the rotated one back. That is the same shape
      `matrix_oauth.py` already uses for the Matrix account, for the same
      reason.
    * **There is no pre-signed *upload*, but there is a pre-signed download.**
      An item's `@microsoft.graph.downloadUrl` is a direct, already-authorised,
      short-lived URL — exactly what `presigned_get` is for, and better than a
      sharing link, which is a *page* an uploader cannot fetch bytes from and
      which needs share permissions the account may not have. Uploads go
      through the backend's own signed endpoint, as with Nextcloud.
    """

    name = "onedrive"

    _GRAPH = "https://graph.microsoft.com/v1.0"
    # Above this, Graph rejects a simple content PUT and wants a session.
    _SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024

    def __init__(
        self,
        *,
        client_id: str,
        refresh_token: str,
        tenant: str = "consumers",
        root: str = "",
        timeout: float = 120.0,
        on_token_refreshed: Optional[Any] = None,
    ):
        if not client_id:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED,
                "OneDrive needs the client id of an Entra app registration",
                retryable=False,
            )
        if not refresh_token:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_AUTH,
                "OneDrive is not signed in — run the device-code login on the Storage screen",
                retryable=False,
            )
        self.client_id = client_id
        self.tenant = tenant or "consumers"
        self._refresh_token = refresh_token
        self.root = root.strip("/")
        self.timeout = timeout
        # Lets the caller persist a rotated refresh token. Without it the stored
        # one goes stale the first time Microsoft rotates it and the next
        # restart cannot sign in at all.
        self._on_token_refreshed = on_token_refreshed
        self._access_token = ""
        self._access_expires_at = 0.0

    # ── OAuth ────────────────────────────────────────────────────────────
    def _token(self) -> str:
        # 60s of slack so a request never races a token that is valid when
        # checked and expired when Graph sees it.
        if self._access_token and self._access_expires_at - 60 > time.time():
            return self._access_token

        import httpx

        try:
            response = httpx.post(
                f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token",
                data={
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "scope": ONEDRIVE_SCOPES,
                },
                timeout=30.0,
            )
        except Exception as exc:
            raise _storage_error(exc) from exc
        if response.status_code != 200:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_AUTH,
                "OneDrive refused the stored sign-in — sign in again on the Storage screen",
                retryable=False,
            )
        body = response.json()
        self._access_token = str(body.get("access_token") or "")
        self._access_expires_at = time.time() + float(body.get("expires_in") or 3600)
        rotated = str(body.get("refresh_token") or "")
        if rotated and rotated != self._refresh_token:
            self._refresh_token = rotated
            if self._on_token_refreshed:
                try:
                    self._on_token_refreshed(rotated)
                except Exception:
                    logger.exception("Could not persist the rotated OneDrive refresh token")
        if not self._access_token:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_AUTH, "OneDrive returned no access token", retryable=False
            )
        return self._access_token

    def _client(self):
        import httpx

        return httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"Authorization": f"Bearer {self._token()}"},
        )

    def _item_url(self, key: str, suffix: str = "") -> str:
        """Graph addresses an item by path as `/me/drive/root:/a/b/c:` and then
        appends the action, except for the bare item itself where the trailing
        colon must go."""
        path = "/".join(part for part in [self.root, key] if part)
        encoded = quote(path)
        return f"{self._GRAPH}/me/drive/root:/{encoded}:{suffix}" if suffix \
            else f"{self._GRAPH}/me/drive/root:/{encoded}"

    # ── AssetStorage ─────────────────────────────────────────────────────
    def put(self, key: str, data: bytes, *, mime_type: str = "") -> StoredObject:
        checksum = hashlib.sha256(data).hexdigest()
        try:
            with self._client() as client:
                if len(data) <= self._SIMPLE_UPLOAD_LIMIT:
                    response = client.put(
                        self._item_url(key, "/content"),
                        content=data,
                        headers={"Content-Type": mime_type or "application/octet-stream"},
                    )
                    if response.status_code not in (200, 201):
                        raise RuntimeError(f"PUT returned {response.status_code}")
                else:
                    self._upload_session(client, key, data, mime_type)
        except Exception as exc:
            raise _storage_error(exc) from exc
        return StoredObject(key=key, size_bytes=len(data), checksum=checksum, mime_type=mime_type)

    def _upload_session(self, client: Any, key: str, data: bytes, mime_type: str) -> None:
        """Graph's resumable upload. Rendered video is routinely tens of
        megabytes, so this is the normal path for a finished asset, not an
        exotic one."""
        response = client.post(
            self._item_url(key, "/createUploadSession"),
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        response.raise_for_status()
        upload_url = str(response.json().get("uploadUrl") or "")
        if not upload_url:
            raise RuntimeError("createUploadSession returned no uploadUrl")

        # Graph requires every chunk except the last to be a multiple of 320 KiB.
        chunk_size = 10 * 320 * 1024
        total = len(data)
        for start in range(0, total, chunk_size):
            chunk = data[start:start + chunk_size]
            end = start + len(chunk) - 1
            # No Authorization header: the session URL carries its own.
            import httpx

            chunk_response = httpx.put(
                upload_url,
                content=chunk,
                headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                    "Content-Type": mime_type or "application/octet-stream",
                },
                timeout=self.timeout,
            )
            if chunk_response.status_code not in (200, 201, 202):
                raise RuntimeError(
                    f"Upload chunk {start}-{end} returned {chunk_response.status_code}"
                )

    def get(self, key: str) -> bytes:
        try:
            with self._client() as client:
                response = client.get(self._item_url(key, "/content"))
                if response.status_code == 404:
                    raise ContentFactoryError(
                        ErrorCode.VALIDATION_FAILED, f"No such asset: {key}", retryable=False
                    )
                response.raise_for_status()
                return response.content
        except ContentFactoryError:
            raise
        except Exception as exc:
            raise _storage_error(exc) from exc

    def delete(self, key: str) -> None:
        try:
            with self._client() as client:
                response = client.delete(self._item_url(key))
                if response.status_code not in (200, 204, 404):
                    raise RuntimeError(f"DELETE returned {response.status_code}")
        except Exception as exc:
            raise _storage_error(exc) from exc

    def exists(self, key: str) -> bool:
        try:
            with self._client() as client:
                return client.get(self._item_url(key)).status_code < 400
        except Exception:
            return False

    def presigned_get(self, key: str, *, expires_in: int = 3600) -> str:
        """Graph's own pre-authorised download URL. It is not cached: unlike a
        Nextcloud share it is not an object that can be reused, it is a
        signature Microsoft mints per request and expires on its own."""
        try:
            with self._client() as client:
                response = client.get(self._item_url(key), params={"select": "id,@microsoft.graph.downloadUrl"})
                if response.status_code == 404:
                    raise ContentFactoryError(
                        ErrorCode.VALIDATION_FAILED, f"No such asset: {key}", retryable=False
                    )
                response.raise_for_status()
                url = str(response.json().get("@microsoft.graph.downloadUrl") or "")
        except ContentFactoryError:
            raise
        except Exception as exc:
            raise _storage_error(exc) from exc
        if not url:
            raise ContentFactoryError(
                ErrorCode.PROVIDER_UNAVAILABLE,
                "OneDrive returned no download URL for this asset",
                retryable=False,
            )
        return url

    def presigned_put(self, key: str, *, expires_in: int = 3600, mime_type: str = "") -> dict[str, Any]:
        """Same proxied path as Nextcloud: Graph's upload session URL is
        unauthenticated and long-lived, which is not something to hand to a
        browser, and the pipeline writes server-side with `put()` anyway."""
        expires_at = int(time.time()) + expires_in
        signature = hmac.new(
            _upload_secret(), f"{key}:{expires_at}".encode(), hashlib.sha256
        ).hexdigest()
        return {
            "url": (
                f"/api/v1/content-factory/assets/upload?key={quote(key)}"
                f"&expires={expires_at}&signature={signature}"
            ),
            "method": "PUT",
            "key": key,
            "proxied": True,
            "headers": {"Content-Type": mime_type or "application/octet-stream"},
        }

    def health(self) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.get(f"{self._GRAPH}/me/drive", params={"select": "id,name,owner,quota"})
            if response.status_code >= 400:
                return {"storage": self.name, "status": "error", "detail": f"HTTP {response.status_code}"}
            body = response.json()
            owner = ((body.get("owner") or {}).get("user") or {}).get("displayName", "")
            quota = body.get("quota") or {}
            return {
                "storage": self.name,
                "status": "connected",
                "account": owner,
                "root": self.root,
                "remainingBytes": quota.get("remaining"),
            }
        except ContentFactoryError as exc:
            return {"storage": self.name, "status": "error", "detail": exc.code}
        except Exception as exc:
            return {"storage": self.name, "status": "error", "detail": type(exc).__name__}


class LocalStorage(AssetStorage):
    """Filesystem stand-in. Signed URLs point at the backend's own asset
    endpoint and carry an HMAC + expiry, so an unsigned or stale link is
    rejected here exactly as S3 would reject it."""

    name = "local"

    def __init__(self, root: str, *, secret: str = "", base_url: str = "/api/v1/content-factory/assets/raw"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._secret = (secret or os.environ.get("CONTENT_FACTORY_STORAGE_SECRET", "") or "local-dev").encode()
        self.base_url = base_url

    def _path(self, key: str) -> Path:
        # `key` is built by build_key(), whose segments are sanitized — but this
        # is the boundary where a crafted key would become a filesystem path, so
        # it is re-checked here rather than trusted.
        candidate = (self.root / key).resolve()
        if not str(candidate).startswith(str(self.root.resolve())):
            raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, "Invalid storage key", retryable=False)
        return candidate

    def put(self, key: str, data: bytes, *, mime_type: str = "") -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoredObject(
            key=key, size_bytes=len(data), checksum=hashlib.sha256(data).hexdigest(), mime_type=mime_type
        )

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise ContentFactoryError(ErrorCode.VALIDATION_FAILED, f"No such asset: {key}", retryable=False)
        return path.read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def sign(self, key: str, expires_at: int) -> str:
        message = f"{key}:{expires_at}".encode()
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def verify(self, key: str, expires_at: int, signature: str) -> bool:
        if expires_at < int(time.time()):
            return False
        return hmac.compare_digest(self.sign(key, expires_at), signature)

    def presigned_get(self, key: str, *, expires_in: int = 3600) -> str:
        expires_at = int(time.time()) + expires_in
        return (
            f"{self.base_url}?key={quote(key)}&expires={expires_at}"
            f"&signature={self.sign(key, expires_at)}"
        )

    def presigned_put(self, key: str, *, expires_in: int = 3600, mime_type: str = "") -> dict[str, Any]:
        expires_at = int(time.time()) + expires_in
        return {
            "url": (
                f"{self.base_url}?key={quote(key)}&expires={expires_at}"
                f"&signature={self.sign(key, expires_at)}"
            ),
            "method": "PUT",
            "key": key,
            "headers": {"Content-Type": mime_type or "application/octet-stream"},
        }

    def health(self) -> dict[str, Any]:
        return {"storage": self.name, "status": "connected", "root": str(self.root)}


_storage: Optional[AssetStorage] = None


def get_storage() -> AssetStorage:
    global _storage
    if _storage is None:
        _storage = build_storage()
    return _storage


def set_storage_for_tests(storage: Optional[AssetStorage]) -> None:
    global _storage
    _storage = storage


def reset_storage() -> None:
    """Drop the singleton so the next `get_storage()` rebuilds from the current
    configuration. This is what makes switching the store on the Storage screen
    take effect on the next asset write instead of at the next restart."""
    global _storage
    _storage = None


def default_local_root() -> str:
    """Where LocalStorage writes when nothing names a directory."""
    return os.environ.get("CONTENT_FACTORY_LOCAL_STORAGE") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "content-factory"
    )


def build_storage() -> AssetStorage:
    """What the owner chose on the Storage screen; failing that, Nextcloud when
    the environment configures one, then S3, then the local directory.

    Falling back rather than failing is deliberate: a developer with nothing
    configured still gets a working pipeline. Production is protected by
    `CONTENT_FACTORY_REQUIRE_REMOTE=true`, which turns a missing or broken
    remote store into a loud startup error instead of a silent local write that
    nobody notices until a video cannot be published.

    The saved choice comes first because it is the more recent, more deliberate
    statement of intent — somebody opened the screen and picked it. It is also
    the only one that can be changed without a redeploy, which is the whole
    reason the screen exists; "env" remains selectable there and hands the
    decision straight back to the block below.
    """
    require_remote = os.environ.get(
        "CONTENT_FACTORY_REQUIRE_REMOTE",
        os.environ.get("CONTENT_FACTORY_REQUIRE_S3", ""),
    ).strip().lower() in {"1", "true", "yes"}

    try:
        from backend.content_factory.integrations import build_configured_storage

        configured = build_configured_storage()
        if configured is not None:
            return configured
    except Exception:
        # A broken saved configuration must not leave the factory with no store
        # at all: it is logged and the environment chain below still runs, so a
        # typo in a Nextcloud URL degrades to the previous behaviour instead of
        # taking every asset write down with it.
        logger.exception("Configured asset storage could not be initialized")
        if require_remote:
            raise

    nextcloud_url = os.environ.get("CONTENT_FACTORY_NEXTCLOUD_URL", "").strip()
    if nextcloud_url:
        try:
            return NextcloudStorage(
                nextcloud_url,
                username=os.environ.get("CONTENT_FACTORY_NEXTCLOUD_USER", "").strip(),
                password=_secret("CONTENT_FACTORY_NEXTCLOUD_PASSWORD"),
                # Keys already begin with the §44 "content-factory/" prefix, so the
                # remote root is an optional *extra* folder for a shared instance,
                # not a second copy of that prefix.
                root=os.environ.get("CONTENT_FACTORY_NEXTCLOUD_ROOT", "").strip(),
                verify_tls=os.environ.get(
                    "CONTENT_FACTORY_NEXTCLOUD_VERIFY_TLS", "true"
                ).strip().lower() not in {"0", "false", "no"},
                share_expire_days=int(os.environ.get("CONTENT_FACTORY_NEXTCLOUD_SHARE_DAYS", "7") or 7),
                public_base_url=os.environ.get("CONTENT_FACTORY_NEXTCLOUD_PUBLIC_URL", "").strip(),
            )
        except Exception:
            logger.exception("Nextcloud storage could not be initialized")
            if require_remote:
                raise

    onedrive_client_id = os.environ.get("CONTENT_FACTORY_ONEDRIVE_CLIENT_ID", "").strip()
    if onedrive_client_id:
        try:
            return OneDriveStorage(
                client_id=onedrive_client_id,
                refresh_token=_secret("CONTENT_FACTORY_ONEDRIVE_REFRESH_TOKEN"),
                tenant=os.environ.get("CONTENT_FACTORY_ONEDRIVE_TENANT", "consumers").strip() or "consumers",
                root=os.environ.get("CONTENT_FACTORY_ONEDRIVE_ROOT", "").strip(),
            )
        except Exception:
            logger.exception("OneDrive storage could not be initialized")
            if require_remote:
                raise

    bucket = os.environ.get("CONTENT_FACTORY_S3_BUCKET", "").strip()
    require_s3 = require_remote

    if bucket:
        try:
            return S3Storage(
                bucket,
                endpoint_url=os.environ.get("CONTENT_FACTORY_S3_ENDPOINT", "").strip(),
                region=os.environ.get("CONTENT_FACTORY_S3_REGION", "").strip(),
                access_key=_secret("CONTENT_FACTORY_S3_ACCESS_KEY"),
                secret_key=_secret("CONTENT_FACTORY_S3_SECRET_KEY"),
                public_base_url=os.environ.get("CONTENT_FACTORY_S3_PUBLIC_URL", "").strip(),
            )
        except Exception:
            logger.exception("S3 storage could not be initialized")
            if require_s3:
                raise
    elif require_remote:
        raise ContentFactoryError(
            ErrorCode.INTERNAL,
            "CONTENT_FACTORY_REQUIRE_REMOTE is set but neither Nextcloud nor a bucket is configured",
            retryable=False,
        )

    root = default_local_root()
    logger.warning("Content Factory: using local asset storage at %s", root)
    return LocalStorage(root)


def _upload_secret() -> bytes:
    """Key for signing the backend-proxied upload URLs. Falls back to a
    development constant so tests work, which is safe only because the endpoint
    that verifies it is authenticated anyway."""
    return (
        os.environ.get("CONTENT_FACTORY_STORAGE_SECRET", "").strip() or "local-dev"
    ).encode()


def verify_upload_signature(key: str, expires_at: int, signature: str) -> bool:
    """Checks a proxied-upload URL: unexpired, and signed by us."""
    if expires_at < int(time.time()):
        return False
    expected = hmac.new(_upload_secret(), f"{key}:{expires_at}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _secret(env_key: str) -> str:
    """Credentials come from the environment, from Valkey when the deployment
    keeps them out of the container's env (§63), or from the dashboard's own
    secret store when they were typed on the Storage screen."""
    try:
        from backend.content_factory.integrations import read_secret

        return read_secret(env_key)
    except Exception:
        return os.environ.get(env_key, "").strip()


def _storage_error(exc: Exception) -> ContentFactoryError:
    name = type(exc).__name__
    if "NoSuchKey" in name or "404" in str(exc):
        return ContentFactoryError(ErrorCode.VALIDATION_FAILED, "Asset not found", retryable=False)
    if "AccessDenied" in name or "AccessDenied" in str(exc):
        return ContentFactoryError(ErrorCode.PROVIDER_AUTH, "Storage rejected the credentials", retryable=False)
    return ContentFactoryError(ErrorCode.PROVIDER_UNAVAILABLE, "Object storage is unavailable")
