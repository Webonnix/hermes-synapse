"""Asset storage: key convention, local signing, and the Nextcloud backend.

Nextcloud is exercised against an httpx MockTransport rather than a live
instance: what needs pinning is the request *shape* (collections created before
the PUT, share created with an expiry, `/download` appended), which is exactly
what breaks silently when someone edits the adapter.
"""

import time

import httpx
import pytest

from backend.content_factory import storage as storage_module
from backend.content_factory.domain import ContentFactoryError


# ── Keys (§44) ───────────────────────────────────────────────────────────────

def test_key_follows_the_spec_convention():
    key = storage_module.build_key("proj-1", "run-9", "RENDER", "final.mp4")
    assert key == "content-factory/projects/proj-1/runs/run-9/renders/final.mp4"


def test_asset_types_map_to_their_folders():
    for asset_type, folder in [("IMAGE", "images"), ("VOICE", "audio"), ("SUBTITLE", "subtitles")]:
        assert f"/{folder}/" in storage_module.build_key("p", "r", asset_type, "x.bin")


def test_key_segments_cannot_traverse():
    key = storage_module.build_key("../../etc", "run/../..", "RENDER", "../passwd")
    assert ".." not in key
    assert key.count("/") == 6


# ── LocalStorage ─────────────────────────────────────────────────────────────

@pytest.fixture()
def local(tmp_path):
    return storage_module.LocalStorage(str(tmp_path / "assets"), secret="test-secret")


def test_local_roundtrip(local):
    stored = local.put("content-factory/projects/p/runs/r/renders/a.mp4", b"hello", mime_type="video/mp4")
    assert stored.size_bytes == 5
    assert local.get(stored.key) == b"hello"
    assert local.exists(stored.key)
    local.delete(stored.key)
    assert not local.exists(stored.key)


def test_local_signed_url_expires(local):
    key = "content-factory/projects/p/runs/r/renders/a.mp4"
    expired = int(time.time()) - 10
    assert local.verify(key, expired, local.sign(key, expired)) is False
    fresh = int(time.time()) + 60
    assert local.verify(key, fresh, local.sign(key, fresh)) is True


def test_local_rejects_a_tampered_signature(local):
    key = "content-factory/projects/p/runs/r/renders/a.mp4"
    expires = int(time.time()) + 60
    assert local.verify(key, expires, "deadbeef") is False


def test_local_refuses_a_key_outside_its_root(local):
    with pytest.raises(ContentFactoryError):
        local.put("../../escape.txt", b"x")


# ── Nextcloud ────────────────────────────────────────────────────────────────

def _nextcloud(handler) -> storage_module.NextcloudStorage:
    store = storage_module.NextcloudStorage(
        "https://cloud.example.com", username="hermes", password="app-password"
    )
    transport = httpx.MockTransport(handler)
    store._client = lambda: httpx.Client(transport=transport, base_url="")  # type: ignore[method-assign]
    return store


def test_nextcloud_put_creates_collections_then_uploads():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "MKCOL":
            return httpx.Response(201)
        if request.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(404)

    store = _nextcloud(handler)
    key = storage_module.build_key("p1", "r1", "RENDER", "final.mp4")
    stored = store.put(key, b"video-bytes", mime_type="video/mp4")

    assert stored.size_bytes == len(b"video-bytes")
    methods = [method for method, _ in seen]
    # Every parent collection is created before the file is written.
    assert methods.count("MKCOL") == 6
    assert methods[-1] == "PUT"
    assert seen[-1][1].endswith("/content-factory/projects/p1/runs/r1/renders/final.mp4")


def test_nextcloud_tolerates_existing_collections():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "MKCOL":
            return httpx.Response(405)  # already exists
        return httpx.Response(201)

    store = _nextcloud(handler)
    assert store.put("content-factory/projects/p/runs/r/images/a.png", b"x").size_bytes == 1


def test_nextcloud_missing_asset_is_not_retryable():
    store = _nextcloud(lambda request: httpx.Response(404))
    with pytest.raises(ContentFactoryError) as excinfo:
        store.get("content-factory/projects/p/runs/r/images/missing.png")
    assert excinfo.value.retryable is False


def test_nextcloud_share_link_is_a_direct_download():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "files_sharing" in request.url.path:
            calls.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(
                200,
                json={"ocs": {"meta": {"status": "ok"}, "data": {"url": "https://cloud.example.com/s/AbC123"}}},
            )
        return httpx.Response(201)

    store = _nextcloud(handler)
    url = store.presigned_get("content-factory/projects/p/runs/r/renders/final.mp4", expires_in=3600)

    assert url == "https://cloud.example.com/s/AbC123/download"
    assert calls[0]["shareType"] == "3"
    assert calls[0]["permissions"] == "1"  # read-only
    assert calls[0]["expireDate"]  # §50: links are never open-ended


def test_nextcloud_reuses_one_share_per_asset():
    """A share is a real object on the server; creating one per request would
    leave hundreds behind for a single video."""
    created = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "files_sharing" in request.url.path:
            created.append(1)
            return httpx.Response(
                200, json={"ocs": {"data": {"url": "https://cloud.example.com/s/tok"}}}
            )
        return httpx.Response(201)

    store = _nextcloud(handler)
    key = "content-factory/projects/p/runs/r/renders/final.mp4"
    first = store.presigned_get(key)
    second = store.presigned_get(key)

    assert first == second
    assert len(created) == 1


def test_nextcloud_reports_a_refused_share():
    def handler(request: httpx.Request) -> httpx.Response:
        if "files_sharing" in request.url.path:
            return httpx.Response(
                200,
                json={"ocs": {"meta": {"status": "failure", "message": "Public link sharing is disabled"},
                              "data": {}}},
            )
        return httpx.Response(201)

    store = _nextcloud(handler)
    with pytest.raises(ContentFactoryError) as excinfo:
        store.presigned_get("content-factory/projects/p/runs/r/renders/final.mp4")
    assert "disabled" in excinfo.value.message


def test_nextcloud_rewrites_share_url_to_the_public_host():
    """Split DNS: the backend reaches Nextcloud internally, but a platform
    fetching the file must be given the externally resolvable name."""
    store = storage_module.NextcloudStorage(
        "http://nextcloud:8080",
        username="hermes",
        password="pw",
        public_base_url="https://cloud.example.com",
    )
    store._client = lambda: httpx.Client(  # type: ignore[method-assign]
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"ocs": {"data": {"url": "http://nextcloud:8080/s/tok"}}})
        )
    )
    assert store.presigned_get("k") == "https://cloud.example.com/s/tok/download"


def test_nextcloud_proxied_upload_url_is_signed(monkeypatch):
    monkeypatch.setenv("CONTENT_FACTORY_STORAGE_SECRET", "unit-test")
    store = storage_module.NextcloudStorage("https://cloud.example.com", username="u", password="p")
    grant = store.presigned_put("content-factory/projects/p/runs/r/images/a.png", expires_in=600)

    assert grant["proxied"] is True
    params = httpx.QueryParams(grant["url"].split("?", 1)[1])
    assert storage_module.verify_upload_signature(
        params["key"], int(params["expires"]), params["signature"]
    )
    assert not storage_module.verify_upload_signature(params["key"], int(params["expires"]), "nope")


# ── Selection ────────────────────────────────────────────────────────────────

def test_nextcloud_wins_when_configured(monkeypatch):
    monkeypatch.setenv("CONTENT_FACTORY_NEXTCLOUD_URL", "https://cloud.example.com")
    monkeypatch.setenv("CONTENT_FACTORY_NEXTCLOUD_USER", "hermes")
    monkeypatch.setenv("CONTENT_FACTORY_S3_BUCKET", "some-bucket")
    assert storage_module.build_storage().name == "nextcloud"


def test_local_is_the_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTENT_FACTORY_NEXTCLOUD_URL", raising=False)
    monkeypatch.delenv("CONTENT_FACTORY_S3_BUCKET", raising=False)
    monkeypatch.setenv("CONTENT_FACTORY_LOCAL_STORAGE", str(tmp_path))
    assert storage_module.build_storage().name == "local"


def test_require_remote_refuses_to_fall_back(monkeypatch):
    monkeypatch.delenv("CONTENT_FACTORY_NEXTCLOUD_URL", raising=False)
    monkeypatch.delenv("CONTENT_FACTORY_S3_BUCKET", raising=False)
    monkeypatch.setenv("CONTENT_FACTORY_REQUIRE_REMOTE", "true")
    with pytest.raises(ContentFactoryError):
        storage_module.build_storage()


# ── OneDrive ─────────────────────────────────────────────────────────────────
# Same MockTransport approach as Nextcloud, for the same reason: what breaks
# silently here is the request shape, not the happy path. Two things are
# specific to this backend and worth pinning — the access token is fetched from
# the refresh token rather than configured, and a rotated refresh token must be
# handed back to the caller so the next restart can still sign in.

def _onedrive(handler, **kwargs) -> storage_module.OneDriveStorage:
    store = storage_module.OneDriveStorage(
        client_id="app-id", refresh_token="rt-1", **kwargs,
    )
    transport = httpx.MockTransport(handler)
    store._client = lambda: httpx.Client(transport=transport)  # type: ignore[method-assign]
    store._access_token = "at-1"
    store._access_expires_at = time.time() + 3600
    return store


def test_onedrive_needs_a_client_id():
    with pytest.raises(ContentFactoryError):
        storage_module.OneDriveStorage(client_id="", refresh_token="rt")


def test_onedrive_needs_a_sign_in():
    with pytest.raises(ContentFactoryError):
        storage_module.OneDriveStorage(client_id="app-id", refresh_token="")


def test_onedrive_small_file_uses_a_simple_content_put():
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(201, json={"id": "item-1"})

    store = _onedrive(handler)
    key = storage_module.build_key("p1", "r1", "IMAGE", "a.png")
    stored = store.put(key, b"png-bytes", mime_type="image/png")

    assert stored.size_bytes == len(b"png-bytes")
    assert seen == [("PUT", f"/v1.0/me/drive/root:/{key}:/content")]


def test_onedrive_presigned_get_returns_graphs_own_download_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "item-1",
            "@microsoft.graph.downloadUrl": "https://public.example/download/abc",
        })

    store = _onedrive(handler)
    url = store.presigned_get(storage_module.build_key("p1", "r1", "RENDER", "v.mp4"))
    assert url == "https://public.example/download/abc"


def test_onedrive_presigned_get_without_a_url_is_an_error_not_an_empty_string():
    store = _onedrive(lambda request: httpx.Response(200, json={"id": "item-1"}))
    with pytest.raises(ContentFactoryError):
        store.presigned_get("content-factory/x")


def test_onedrive_missing_asset_is_a_validation_error():
    store = _onedrive(lambda request: httpx.Response(404, json={}))
    with pytest.raises(ContentFactoryError) as excinfo:
        store.get("content-factory/missing")
    assert excinfo.value.retryable is False


def test_onedrive_upload_is_proxied_not_pre_signed():
    store = _onedrive(lambda request: httpx.Response(200, json={}))
    upload = store.presigned_put("content-factory/x", mime_type="video/mp4")
    assert upload["proxied"] is True
    assert upload["url"].startswith("/api/v1/content-factory/assets/upload")


def test_onedrive_root_folder_prefixes_the_key():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(201, json={})

    store = _onedrive(handler, root="hermes")
    store.put("content-factory/a.png", b"x", mime_type="image/png")
    assert seen[0] == "/v1.0/me/drive/root:/hermes/content-factory/a.png:/content"


def test_onedrive_persists_a_rotated_refresh_token(monkeypatch):
    """Microsoft rotates the refresh token on most exchanges. Not writing the
    new one back is a store that works until the next restart and then cannot
    sign in at all — the exact failure this callback exists to prevent."""
    saved: list[str] = []
    store = storage_module.OneDriveStorage(
        client_id="app-id", refresh_token="rt-1", on_token_refreshed=saved.append,
    )

    def fake_post(url, data=None, timeout=None):
        assert data["grant_type"] == "refresh_token"
        return httpx.Response(200, json={
            "access_token": "at-2", "expires_in": 3600, "refresh_token": "rt-2",
        })

    monkeypatch.setattr(httpx, "post", fake_post)
    assert store._token() == "at-2"
    assert saved == ["rt-2"]


def test_onedrive_rejected_refresh_token_is_an_auth_error(monkeypatch):
    store = storage_module.OneDriveStorage(client_id="app-id", refresh_token="rt-stale")
    monkeypatch.setattr(
        httpx, "post", lambda url, data=None, timeout=None: httpx.Response(400, json={"error": "invalid_grant"})
    )
    with pytest.raises(ContentFactoryError) as excinfo:
        store._token()
    assert excinfo.value.retryable is False
