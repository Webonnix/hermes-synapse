"""The MAS (OAuth-delegated) Matrix login and its token renewal.

Everything here exists because a `mat_`-prefixed access token pasted out of
Element dies within minutes — the "постоянные отвалы" of the Element/Matrix
channel. These tests pin the two halves of the fix: getting a *renewable*
credential in the first place, and actually renewing it before it dies.
"""

import asyncio
import json
import os
import time

import httpx
import pytest
import respx

from backend import matrix_oauth

HOMESERVER = "https://matrix.example.org"
TOKEN_ENDPOINT = "https://matrix.example.org/auth/oauth2/token"


@pytest.fixture()
def fake_valkey(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(
        "backend.valkey_client.set_value",
        lambda key, value, ttl_seconds=None: store.__setitem__(key, value),
    )
    monkeypatch.setattr("backend.valkey_client.get_value", lambda key: store.get(key))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda key: store.pop(key, None))
    return store


def _mock_discovery() -> None:
    respx.get(f"{HOMESERVER}/.well-known/matrix/client").mock(
        return_value=httpx.Response(200, json={"m.homeserver": {"base_url": HOMESERVER}})
    )
    respx.get(f"{HOMESERVER}/_matrix/client/v1/auth_metadata").mock(
        return_value=httpx.Response(
            200,
            json={
                "issuer": f"{HOMESERVER}/auth/",
                "token_endpoint": TOKEN_ENDPOINT,
                "registration_endpoint": f"{HOMESERVER}/auth/oauth2/registration",
                "device_authorization_endpoint": f"{HOMESERVER}/auth/oauth2/device",
            },
        )
    )
    respx.post(f"{HOMESERVER}/auth/oauth2/registration").mock(
        return_value=httpx.Response(200, json={"client_id": "client-123"})
    )


@respx.mock
def test_device_login_start_then_poll_yields_refreshable_credentials(fake_valkey):
    _mock_discovery()
    respx.post(f"{HOMESERVER}/auth/oauth2/device").mock(
        return_value=httpx.Response(200, json={
            "device_code": "dev-code", "user_code": "ABCDEF",
            "verification_uri": f"{HOMESERVER}/auth/link", "expires_in": 1200, "interval": 5,
        })
    )
    token_route = respx.post(TOKEN_ENDPOINT).mock(
        side_effect=[
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(200, json={
                "access_token": "mat_new", "refresh_token": "mar_new", "expires_in": 300,
            }),
        ]
    )
    respx.get(f"{HOMESERVER}/_matrix/client/v3/account/whoami").mock(
        return_value=httpx.Response(200, json={"user_id": "@albert:example.org"})
    )

    started = matrix_oauth.start_device_login(HOMESERVER)
    assert started["user_code"] == "ABCDEF"
    # The browser is told the code, never the device_code that could redeem it.
    assert "device_code" not in started

    assert matrix_oauth.poll_device_login(started["flow_id"]) is None  # still pending
    credentials = matrix_oauth.poll_device_login(started["flow_id"])
    assert credentials["access_token"] == "mat_new"
    assert credentials["refresh_token"] == "mar_new"
    assert credentials["auth_kind"] == "oauth"
    assert credentials["oauth_client_id"] == "client-123"
    assert credentials["user_id"] == "@albert:example.org"
    assert token_route.call_count == 2


@respx.mock
def test_pending_poll_repeats_the_code_but_never_the_device_code(fake_valkey):
    """The code has to survive a page reload: it used to exist only in the one
    response that started the flow, so a dashboard that lost it left the owner
    with an unfillable form and no way to recover the code."""
    from backend import agent_messenger_governance

    _mock_discovery()
    respx.post(f"{HOMESERVER}/auth/oauth2/device").mock(
        return_value=httpx.Response(200, json={
            "device_code": "dev-code-secret", "user_code": "ABCDEF",
            "verification_uri": f"{HOMESERVER}/auth/link", "expires_in": 1200, "interval": 5,
        })
    )
    respx.post(TOKEN_ENDPOINT).mock(return_value=httpx.Response(400, json={"error": "authorization_pending"}))

    started = matrix_oauth.start_device_login(HOMESERVER)
    pending = agent_messenger_governance.poll_matrix_device_login(started["flow_id"])
    assert pending["status"] == "pending"
    assert pending["user_code"] == "ABCDEF"
    assert pending["verification_uri_complete"].endswith("?code=ABCDEF")
    assert 0 < pending["expires_in"] <= 1200
    assert "dev-code-secret" not in json.dumps(pending)


@respx.mock
def test_device_login_without_refresh_token_is_rejected(fake_valkey):
    """A credential that cannot be renewed is exactly the failure mode this whole
    flow exists to remove — better to fail at connect time than in five minutes."""
    _mock_discovery()
    respx.post(f"{HOMESERVER}/auth/oauth2/device").mock(
        return_value=httpx.Response(200, json={
            "device_code": "dev-code", "user_code": "ABCDEF",
            "verification_uri": f"{HOMESERVER}/auth/link", "expires_in": 1200, "interval": 5,
        })
    )
    respx.post(TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"access_token": "mat_new", "expires_in": 300})
    )
    respx.get(f"{HOMESERVER}/_matrix/client/v3/account/whoami").mock(
        return_value=httpx.Response(200, json={"user_id": "@albert:example.org"})
    )

    started = matrix_oauth.start_device_login(HOMESERVER)
    with pytest.raises(ValueError, match="refresh_token"):
        matrix_oauth.poll_device_login(started["flow_id"])


@respx.mock
def test_refresh_uses_oauth_token_endpoint_and_rotates_both_tokens():
    route = respx.post(TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(200, json={
            "access_token": "mat_second", "refresh_token": "mar_second", "expires_in": 300,
        })
    )
    updated = matrix_oauth.refresh_credentials({
        "homeserver_url": HOMESERVER, "user_id": "@albert:example.org",
        "access_token": "mat_first", "refresh_token": "mar_first",
        "auth_kind": "oauth", "oauth_client_id": "client-123", "token_endpoint": TOKEN_ENDPOINT,
    })
    assert updated["access_token"] == "mat_second"
    assert updated["refresh_token"] == "mar_second"
    assert updated["expires_at"] > time.time()
    sent = dict(pair.split("=") for pair in route.calls[0].request.content.decode().split("&"))
    assert sent["grant_type"] == "refresh_token"
    assert sent["client_id"] == "client-123"


@respx.mock
def test_refresh_falls_back_to_matrix_refresh_for_compat_logins():
    """A password/compat login has no OAuth client — it renews at Matrix's own
    /refresh endpoint instead. Sending it to the OAuth endpoint would 404."""
    route = respx.post(f"{HOMESERVER}/_matrix/client/v3/refresh").mock(
        return_value=httpx.Response(200, json={
            "access_token": "syt_second", "refresh_token": "syr_second", "expires_in_ms": 300000,
        })
    )
    updated = matrix_oauth.refresh_credentials({
        "homeserver_url": HOMESERVER, "user_id": "@albert:example.org",
        "access_token": "syt_first", "refresh_token": "syr_first", "auth_kind": "password",
    })
    assert route.called
    assert updated["access_token"] == "syt_second"
    assert updated["refresh_token"] == "syr_second"


@respx.mock
def test_password_login_asks_for_a_refresh_token():
    """nio's own login() never sets refresh_token=true, which is why password
    bindings used to die as fast as pasted ones on a MAS homeserver."""
    respx.get(f"{HOMESERVER}/.well-known/matrix/client").mock(return_value=httpx.Response(404))
    route = respx.post(f"{HOMESERVER}/_matrix/client/v3/login").mock(
        return_value=httpx.Response(200, json={
            "user_id": "@albert:example.org", "access_token": "syt_x", "refresh_token": "syr_x",
            "device_id": "ABCD", "expires_in_ms": 300000,
        })
    )
    credentials = matrix_oauth.password_login(HOMESERVER, "@albert:example.org", "hunter2")
    assert json.loads(route.calls[0].request.read())["refresh_token"] is True
    assert credentials["refresh_token"] == "syr_x"
    assert credentials["expires_at"] > time.time()


def test_seconds_until_refresh_pre_empts_expiry():
    assert matrix_oauth.seconds_until_refresh({"refresh_token": "r", "expires_at": time.time() + 300}) == pytest.approx(240, abs=5)
    # Already expired -> renew now (but never a negative sleep).
    assert matrix_oauth.seconds_until_refresh({"refresh_token": "r", "expires_at": time.time() - 10}) == 5.0
    # Classic Synapse token: no expiry, nothing to pre-empt.
    assert matrix_oauth.seconds_until_refresh({"access_token": "syt_x"}) is None


@respx.mock
def test_bot_manager_hands_the_renewed_token_to_the_live_client(monkeypatch):
    """The whole point of renewing: the running sync loop keeps its client and
    just gets a new token, instead of the binding being marked failed."""
    from backend import agent_matrix_bot

    respx.post(TOKEN_ENDPOINT).mock(
        return_value=httpx.Response(200, json={
            "access_token": "mat_second", "refresh_token": "mar_second", "expires_in": 300,
        })
    )

    class FakeClient:
        access_token = "mat_first"

    persisted: dict = {}
    monkeypatch.setattr(
        "backend.agent_messenger_governance.update_matrix_binding_credentials",
        lambda binding_id, credentials: persisted.update({binding_id: credentials}),
    )
    monkeypatch.setattr("backend.channel_activity.record", lambda *args, **kwargs: None)

    manager = agent_matrix_bot.AgentMatrixBotManager()
    client = FakeClient()
    manager._clients["b1"] = client  # type: ignore[assignment]
    manager._credentials["b1"] = {
        "homeserver_url": HOMESERVER, "user_id": "@albert:example.org",
        "access_token": "mat_first", "refresh_token": "mar_first",
        "auth_kind": "oauth", "oauth_client_id": "client-123", "token_endpoint": TOKEN_ENDPOINT,
    }

    assert asyncio.run(manager.refresh_binding_token("b1")) is True
    assert client.access_token == "mat_second"
    assert manager._credentials["b1"]["refresh_token"] == "mar_second"
    assert persisted["b1"]["access_token"] == "mat_second"


def test_store_path_is_per_binding_and_per_device(tmp_path, monkeypatch):
    """Keyed by device too: a reconnect mints a new Matrix device, and mixing its
    keys into the previous device's store is how an olm session gets corrupted."""
    from backend import agent_matrix_bot

    monkeypatch.setattr(agent_matrix_bot, "MATRIX_STORE_ROOT", str(tmp_path / "store"))
    first = agent_matrix_bot._store_path("b1", "AAAAAAAAAA")
    second = agent_matrix_bot._store_path("b1", "BBBBBBBBBB")
    assert first != second
    assert os.path.isdir(first) and os.path.isdir(second)

    agent_matrix_bot.forget_store("b1")
    assert not os.path.exists(first)


def test_store_path_returns_none_when_directory_is_unwritable(tmp_path, monkeypatch):
    """Encryption is optional plumbing — an unwritable data dir must not take the
    whole channel down with it."""
    from backend import agent_matrix_bot

    monkeypatch.setattr(agent_matrix_bot, "MATRIX_STORE_ROOT", str(tmp_path / "store"))
    monkeypatch.setattr(agent_matrix_bot.os, "makedirs", _raise_oserror)
    assert agent_matrix_bot._store_path("b1", "AAAAAAAAAA") is None


def _raise_oserror(*args, **kwargs):
    raise OSError("read-only file system")


def test_send_text_refuses_when_the_bot_is_not_running():
    """Every outbound message goes through the live client, which owns the olm
    store. Falling back to an ad-hoc client would post plaintext into an
    encrypted room — visibly broken, and unencrypted on the wire."""
    from backend import agent_matrix_bot

    manager = agent_matrix_bot.AgentMatrixBotManager()
    with pytest.raises(RuntimeError, match="не запущен"):
        asyncio.run(manager.send_text("b1", "!room:example.org", "привет"))


def test_send_text_uses_the_live_client_and_tolerates_unverified_devices():
    from backend import agent_matrix_bot

    sent = []

    class FakeClient:
        async def room_send(self, room_id, message_type, content, ignore_unverified_devices=False):
            sent.append((room_id, content["body"], ignore_unverified_devices))

    manager = agent_matrix_bot.AgentMatrixBotManager()
    manager._clients["b1"] = FakeClient()  # type: ignore[assignment]
    asyncio.run(manager.send_text("b1", "!room:example.org", "привет"))
    assert sent == [("!room:example.org", "привет", True)]


def test_bot_manager_refresh_is_a_no_op_without_a_refresh_token():
    from backend import agent_matrix_bot

    manager = agent_matrix_bot.AgentMatrixBotManager()
    manager._clients["b1"] = object()  # type: ignore[assignment]
    manager._credentials["b1"] = {"homeserver_url": HOMESERVER, "access_token": "mat_first"}
    assert asyncio.run(manager.refresh_binding_token("b1")) is False


# ── Human-takeover pause: the owner replying personally silences the bot ──────

def test_owner_message_starts_a_pause_and_extends_it(monkeypatch):
    """Same account = the binding's own identity, so a message from that user
    id in a DM room can only be the owner typing personally from Element/another
    device — nothing else can send as that user id."""
    from backend import agent_matrix_bot

    monkeypatch.setattr("backend.channel_activity.record", lambda *a, **k: None)
    manager = agent_matrix_bot.AgentMatrixBotManager()
    manager.set_human_takeover_pause("b1", 60)

    assert manager._human_takeover_active("b1", "!room:example.org") is False
    manager._note_human_takeover("b1", "!room:example.org")
    assert manager._human_takeover_active("b1", "!room:example.org") is True

    first_deadline = manager._human_active_until["b1"]["!room:example.org"]
    manager._note_human_takeover("b1", "!room:example.org")
    assert manager._human_active_until["b1"]["!room:example.org"] >= first_deadline

    # A different room on the same binding is tracked independently.
    assert manager._human_takeover_active("b1", "!other:example.org") is False


def test_pause_set_to_zero_disables_the_feature(monkeypatch):
    from backend import agent_matrix_bot

    monkeypatch.setattr("backend.channel_activity.record", lambda *a, **k: None)
    manager = agent_matrix_bot.AgentMatrixBotManager()
    manager.set_human_takeover_pause("b1", 0)
    manager._note_human_takeover("b1", "!room:example.org")
    assert manager._human_takeover_active("b1", "!room:example.org") is False


def test_incoming_message_is_skipped_while_owner_is_active():
    """The full callback, not just the bookkeeping: a message from the *other*
    party during an active takeover must never reach bot_access_gate/the LLM —
    that's both the point (don't interrupt) and a cost saving."""
    from backend import agent_matrix_bot

    activity = []
    import backend.channel_activity as channel_activity
    orig_record = channel_activity.record
    channel_activity.record = lambda *a, **k: activity.append(a[2] if len(a) > 2 else k.get("kind"))
    try:
        manager = agent_matrix_bot.AgentMatrixBotManager()
        manager.set_human_takeover_pause("b1", 60)

        class FakeClient:
            user_id = "@albert:senla.eu"

        manager._clients["b1"] = FakeClient()  # type: ignore[assignment]
        manager._human_active_until["b1"] = {"!room:example.org": time.time() + 3600}
        callback = manager._make_callback("b1", "devops")

        class FakeRoom:
            room_id = "!room:example.org"
            is_group = True
            member_count = 2

        class FakeEvent:
            sender = "@sergei:senla.eu"
            body = "Привет"

        import asyncio
        asyncio.run(callback(FakeRoom(), FakeEvent()))
        assert "ignored_owner_active" in activity
    finally:
        channel_activity.record = orig_record

