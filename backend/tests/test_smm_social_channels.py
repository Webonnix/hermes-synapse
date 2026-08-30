"""SMM Factory: the four social channels, from credential check to reply.

Covers what is genuinely new here rather than re-testing the shared machinery:

* the generic proposal/approve/activate path used by all four platforms,
  driven end to end for VK against a respx-mocked VK API;
* the webhook receiver's authentication — Meta's verify-token handshake and
  the X-Hub-Signature-256 HMAC — since that endpoint is public and is the one
  place a stranger could otherwise put words into an agent;
* payload extraction for the three Meta shapes, including the echo of our own
  reply that would otherwise make the bot answer itself;
* draft mode on a social channel: nothing reaches the person until the owner
  sends it, and then it goes out on the right provider endpoint.

The transport boundary is mocked (respx), so the real verification, storage,
gate and turn code runs.
"""

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from httpx import Response

from backend import (
    agent_messenger_governance,
    agent_meta_channel,
    agent_tiers,
    agent_vk_bot,
    agent_whatsapp_channel,
    channel_replies,
    control_plane,
    database,
    social_webhooks,
)
from backend.agent import agent_instance

GRAPH = agent_messenger_governance.GRAPH_API_BASE
VK_TOKEN = "vk1." + "a" * 40
META_TOKEN = "EAAG" + "b" * 40
APP_SECRET = "app-secret-value"


@pytest.fixture()
def social_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "smm.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    monkeypatch.setattr(agent_tiers, "DB_PATH", db_path)
    monkeypatch.setattr(agent_messenger_governance, "DB_PATH", db_path)
    monkeypatch.setattr(channel_replies, "DB_PATH", db_path)
    database.init_db()
    agent_messenger_governance._init_schema()
    channel_replies._init_schema()

    stored_secrets: dict[str, str] = {}
    monkeypatch.setattr(
        "backend.valkey_client.set_value",
        lambda key, value, ttl_seconds=None: stored_secrets.__setitem__(key, value),
    )
    monkeypatch.setattr("backend.valkey_client.get_value", lambda key: stored_secrets.get(key))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda key: stored_secrets.pop(key, None))

    database.save_subagent(id="agent-1", name="Agent One", system_prompt="", model="llama3", skills="git_dev")
    return db_path


def _mock_vk_group(respx_mock, name="Моё сообщество", screen_name="mygroup", group_id=42):
    respx_mock.get("https://api.vk.com/method/groups.getById").mock(
        return_value=Response(200, json={"response": {"groups": [
            {"id": group_id, "name": name, "screen_name": screen_name},
        ]}})
    )


async def _activate(proposal: dict) -> dict:
    control_plane.approve_task(proposal["control_task_id"])
    return await agent_messenger_governance.execute_approved_social_binding(proposal["control_task_id"])


# ── credential validation ───────────────────────────────────────────────────

def test_missing_required_credential_is_rejected(social_db):
    with pytest.raises(ValueError, match="app_secret"):
        agent_messenger_governance._clean_social_credentials("whatsapp", {
            "access_token": META_TOKEN, "phone_number_id": "555",
        })


def test_webhook_platforms_get_a_generated_verify_token(social_db):
    clean = agent_messenger_governance._clean_social_credentials("facebook", {
        "access_token": META_TOKEN, "page_id": "123", "app_secret": APP_SECRET,
    })
    assert clean["verify_token"]
    # VK is polled, so there is nothing to hand Meta and nothing is invented.
    vk = agent_messenger_governance._clean_social_credentials("vk", {
        "access_token": VK_TOKEN, "group_id": "42",
    })
    assert "verify_token" not in vk


@respx.mock(assert_all_called=False)
def test_vk_rejects_a_dead_community_key(social_db, respx_mock):
    respx_mock.get("https://api.vk.com/method/groups.getById").mock(
        return_value=Response(200, json={"error": {"error_msg": "access_token has expired"}})
    )
    with pytest.raises(ValueError, match="access_token has expired"):
        agent_messenger_governance.verify_social_credentials(
            "vk", {"access_token": VK_TOKEN, "group_id": "42"}
        )


@respx.mock(assert_all_called=False)
def test_meta_error_message_is_surfaced_verbatim(social_db, respx_mock):
    respx_mock.get(f"{GRAPH}/555").mock(
        return_value=Response(200, json={"error": {"message": "Error validating access token"}})
    )
    with pytest.raises(ValueError, match="Error validating access token"):
        agent_messenger_governance.verify_social_credentials(
            "whatsapp", {"access_token": META_TOKEN, "phone_number_id": "555", "app_secret": APP_SECRET},
        )


# ── VK: proposal → approval → draft → send ──────────────────────────────────

@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_vk_binding_draft_mode_end_to_end(social_db, monkeypatch, respx_mock):
    _mock_vk_group(respx_mock)
    send_route = respx_mock.post("https://api.vk.com/method/messages.send").mock(
        return_value=Response(200, json={"response": 1})
    )
    # The polling loop is not driven from the test (same reasoning as
    # test_agent_bot_live_flow.py): the update handler is called directly.
    monkeypatch.setattr(agent_vk_bot.AgentVKBotManager, "_poll_loop", AsyncMock())

    proposal = agent_messenger_governance.create_social_binding_proposal(
        "vk", "agent-1", {"access_token": VK_TOKEN, "group_id": "42"}, response_mode="draft",
    )
    assert proposal["status"] == "awaiting_approval"
    assert proposal["external_account_id"] == "42"
    assert "mygroup" in proposal["bot_username"]

    try:
        result = await _activate(proposal)
        assert result["status"] == "active"

        monkeypatch.setattr(agent_instance, "_respond_as_subagent", AsyncMock(return_value="Здравствуйте!"))
        async with httpx.AsyncClient() as client:
            await agent_vk_bot.manager._handle_update(
                client, proposal["id"], "agent-1", VK_TOKEN,
                {"type": "message_new", "object": {"message": {"peer_id": 777, "from_id": 777, "text": "Привет"}}},
            )

        # Draft mode: nothing was sent to VK, a draft is waiting instead.
        assert not send_route.called
        pending = channel_replies.list_pending_replies("pending")
        assert len(pending) == 1
        assert pending[0]["platform"] == "vk"
        assert pending[0]["drafted_reply"] == "Здравствуйте!"

        sent = await channel_replies.send_pending_reply(pending[0]["id"])
        assert sent["status"] == "sent"
        assert send_route.called
        body = dict(pair.split("=", 1) for pair in send_route.calls.last.request.content.decode().split("&"))
        assert body["peer_id"] == "777"
    finally:
        await agent_vk_bot.manager.stop_all()


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_vk_ignores_the_communitys_own_messages(social_db, monkeypatch, respx_mock):
    _mock_vk_group(respx_mock)
    monkeypatch.setattr(agent_vk_bot.AgentVKBotManager, "_poll_loop", AsyncMock())
    proposal = agent_messenger_governance.create_social_binding_proposal(
        "vk", "agent-1", {"access_token": VK_TOKEN, "group_id": "42"}, response_mode="auto_labeled",
    )
    try:
        await _activate(proposal)
        responder = AsyncMock(return_value="не должно случиться")
        monkeypatch.setattr(agent_instance, "_respond_as_subagent", responder)
        async with httpx.AsyncClient() as client:
            await agent_vk_bot.manager._handle_update(
                client, proposal["id"], "agent-1", VK_TOKEN,
                # from_id < 0 is the community itself — its own outgoing replies
                # come back on the same long poll.
                {"type": "message_new", "object": {"message": {"peer_id": 777, "from_id": -42, "text": "эхо"}}},
            )
        responder.assert_not_awaited()
    finally:
        await agent_vk_bot.manager.stop_all()


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_social_binding_disable_enable_reconnect(social_db, monkeypatch, respx_mock):
    """The generic lifecycle buttons on a channel card have to work for the
    social platforms too — they route through the same platform-agnostic
    disable/enable/reconnect the messengers use, which each needed a new branch."""
    _mock_vk_group(respx_mock)
    monkeypatch.setattr(agent_vk_bot.AgentVKBotManager, "_poll_loop", AsyncMock())
    proposal = agent_messenger_governance.create_social_binding_proposal(
        "vk", "agent-1", {"access_token": VK_TOKEN, "group_id": "42"},
    )
    try:
        await _activate(proposal)
        assert proposal["id"] in agent_vk_bot.manager._tasks

        disabled = await agent_messenger_governance.disable_binding(proposal["id"])
        assert disabled["status"] == "revoked"
        assert proposal["id"] not in agent_vk_bot.manager._tasks
        # A disabled channel's credential stays stored but unreadable.
        assert agent_messenger_governance.resolve_social_binding_credentials(proposal["id"]) is None

        enabled = await agent_messenger_governance.enable_binding(proposal["id"])
        assert enabled["status"] == "active"
        assert proposal["id"] in agent_vk_bot.manager._tasks

        _mock_vk_group(respx_mock, name="Переименовано", screen_name="renamed", group_id=42)
        reconnected = await agent_messenger_governance.reconnect_social_binding(
            proposal["id"], {"access_token": VK_TOKEN + "-new", "group_id": "42"},
        )
        assert reconnected["status"] == "active"
        assert "renamed" in reconnected["bot_username"]
        assert agent_messenger_governance.resolve_social_binding_credentials(
            proposal["id"]
        )["access_token"] == VK_TOKEN + "-new"
    finally:
        await agent_vk_bot.manager.stop_all()


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_reconnect_keeps_a_registered_verify_token(social_db, respx_mock):
    """Regenerating it would silently break a webhook already registered in the
    Meta console, so an omitted verify token means "keep the one Meta knows"."""
    respx_mock.get(f"{GRAPH}/9001").mock(return_value=Response(200, json={"name": "Страница"}))
    proposal = agent_messenger_governance.create_social_binding_proposal(
        "facebook", "agent-1",
        {"access_token": META_TOKEN, "page_id": "9001", "app_secret": APP_SECRET},
    )
    try:
        await _activate(proposal)
        original = agent_messenger_governance.resolve_social_binding_credentials(proposal["id"])["verify_token"]
        await agent_messenger_governance.reconnect_social_binding(
            proposal["id"],
            {"access_token": META_TOKEN + "-new", "page_id": "9001", "app_secret": APP_SECRET},
        )
        after = agent_messenger_governance.resolve_social_binding_credentials(proposal["id"])
        assert after["verify_token"] == original
        assert after["access_token"] == META_TOKEN + "-new"
    finally:
        await agent_meta_channel.manager.stop_all()


# ── WhatsApp: auto-labeled reply goes out on the Cloud API ──────────────────

@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_whatsapp_auto_labeled_reply_is_disclosed(social_db, monkeypatch, respx_mock):
    respx_mock.get(f"{GRAPH}/555").mock(
        return_value=Response(200, json={"display_phone_number": "+7 999 000-00-00", "verified_name": "Vexa"})
    )
    send_route = respx_mock.post(f"{GRAPH}/555/messages").mock(
        return_value=Response(200, json={"messages": [{"id": "wamid.out"}]})
    )
    proposal = agent_messenger_governance.create_social_binding_proposal(
        "whatsapp", "agent-1",
        {"access_token": META_TOKEN, "phone_number_id": "555", "app_secret": APP_SECRET},
        response_mode="auto_labeled",
    )
    try:
        await _activate(proposal)
        monkeypatch.setattr(agent_instance, "_respond_as_subagent", AsyncMock(return_value="Готово."))

        await agent_whatsapp_channel.manager.handle_message(
            proposal["id"], "79990000000", "Статус?", message_id="wamid.in", display_name="Клиент",
        )

        assert send_route.called
        body = json.loads(send_route.calls.last.request.content)
        assert body["messaging_product"] == "whatsapp"
        assert body["to"] == "79990000000"
        # Auto-labeled mode never sends a reply that could pass for the owner.
        assert "Готово." in body["text"]["body"]
        assert "ассистент" in body["text"]["body"]

        # A Meta retry carries the same message id and must not be answered twice.
        await agent_whatsapp_channel.manager.handle_message(
            proposal["id"], "79990000000", "Статус?", message_id="wamid.in",
        )
        assert send_route.call_count == 1
    finally:
        await agent_whatsapp_channel.manager.stop_all()


# ── webhook authentication ──────────────────────────────────────────────────

def _signed(body: dict) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={digest}"


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_webhook_rejects_an_unsigned_delivery(social_db, monkeypatch, respx_mock):
    respx_mock.get(f"{GRAPH}/555").mock(return_value=Response(200, json={"display_phone_number": "+7"}))
    proposal = agent_messenger_governance.create_social_binding_proposal(
        "whatsapp", "agent-1",
        {"access_token": META_TOKEN, "phone_number_id": "555", "app_secret": APP_SECRET},
    )
    try:
        await _activate(proposal)
        handler = AsyncMock()
        monkeypatch.setattr(agent_whatsapp_channel.manager, "handle_message", handler)

        payload = {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"value": {
            "metadata": {"phone_number_id": "555"},
            "messages": [{"from": "79990000000", "id": "wamid.x", "type": "text", "text": {"body": "Привет"}}],
        }}]}]}
        raw, _good_signature = _signed(payload)

        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            bad = await client.post(
                "/api/social/webhook/whatsapp", content=raw,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=deadbeef"},
            )
        assert bad.status_code == 403
        handler.assert_not_awaited()
    finally:
        await agent_whatsapp_channel.manager.stop_all()


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_webhook_accepts_a_signed_delivery(social_db, monkeypatch, respx_mock):
    respx_mock.get(f"{GRAPH}/555").mock(return_value=Response(200, json={"display_phone_number": "+7"}))
    proposal = agent_messenger_governance.create_social_binding_proposal(
        "whatsapp", "agent-1",
        {"access_token": META_TOKEN, "phone_number_id": "555", "app_secret": APP_SECRET},
    )
    try:
        await _activate(proposal)
        handler = AsyncMock()
        monkeypatch.setattr(agent_whatsapp_channel.manager, "handle_message", handler)

        payload = {"object": "whatsapp_business_account", "entry": [{"id": "waba", "changes": [{"value": {
            "metadata": {"phone_number_id": "555"},
            "contacts": [{"wa_id": "79990000000", "profile": {"name": "Клиент"}}],
            "messages": [{"from": "79990000000", "id": "wamid.x", "type": "text", "text": {"body": "Привет"}}],
        }}]}]}
        raw, signature = _signed(payload)

        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ok = await client.post(
                "/api/social/webhook/whatsapp", content=raw,
                headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
            )
        assert ok.status_code == 200
        assert ok.json()["handled"] == 1
        handler.assert_awaited_once()
        assert handler.await_args.kwargs["display_name"] == "Клиент"
    finally:
        await agent_whatsapp_channel.manager.stop_all()


@respx.mock(assert_all_called=False)
@pytest.mark.asyncio
async def test_verify_token_handshake_matches_only_an_active_binding(social_db, respx_mock):
    respx_mock.get(f"{GRAPH}/9001").mock(return_value=Response(200, json={"name": "Моя страница"}))
    proposal = agent_messenger_governance.create_social_binding_proposal(
        "facebook", "agent-1",
        {"access_token": META_TOKEN, "page_id": "9001", "app_secret": APP_SECRET},
    )
    try:
        # Not approved yet: the handshake must not succeed for a pending channel.
        assert agent_messenger_governance.social_webhook_verify_token("facebook", "whatever") is None

        await _activate(proposal)
        details = agent_messenger_governance.social_webhook_details(proposal["id"])
        assert details["callback_url"].endswith("/api/social/webhook/facebook")
        assert details["app_secret_set"] is True

        matched = agent_messenger_governance.social_webhook_verify_token("facebook", details["verify_token"])
        assert matched and matched["id"] == proposal["id"]
        assert agent_messenger_governance.social_webhook_verify_token("facebook", "wrong-token") is None
    finally:
        await agent_meta_channel.manager.stop_all()


# ── payload extraction ──────────────────────────────────────────────────────

def test_extract_messenger_payload_and_ignores_our_own_echo():
    payload = {"object": "page", "entry": [{"id": "9001", "messaging": [
        {"sender": {"id": "psid-1"}, "message": {"mid": "m1", "text": "Привет"}},
        {"sender": {"id": "9001"}, "message": {"mid": "m2", "text": "наш ответ", "is_echo": True}},
        {"sender": {"id": "psid-1"}, "read": {"watermark": 1}},
    ]}]}
    extracted = social_webhooks._extract_messages("facebook", payload)
    assert [m["text"] for m in extracted] == ["Привет"]
    assert extracted[0] == {
        "account_id": "9001", "chat_id": "psid-1", "text": "Привет", "message_id": "m1", "display_name": "",
    }


def test_extract_whatsapp_payload_keys_on_phone_number_id():
    payload = {"object": "whatsapp_business_account", "entry": [{"id": "waba-1", "changes": [{"value": {
        "metadata": {"phone_number_id": "555"},
        "contacts": [{"wa_id": "79990000000", "profile": {"name": "Клиент"}}],
        "messages": [
            {"from": "79990000000", "id": "wamid.1", "type": "text", "text": {"body": "Привет"}},
            {"from": "79990000000", "id": "wamid.2", "type": "image", "image": {"id": "x"}},
        ],
    }}]}]}
    extracted = social_webhooks._extract_messages("whatsapp", payload)
    # The WABA id in `entry.id` is not what a binding is keyed on — the phone
    # number id from the metadata is, and non-text messages are skipped.
    assert len(extracted) == 1
    assert extracted[0]["account_id"] == "555"
    assert extracted[0]["display_name"] == "Клиент"


def test_signature_check_rejects_a_tampered_body():
    raw = b'{"hello":"world"}'
    digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert social_webhooks._signature_ok(APP_SECRET, raw, f"sha256={digest}")
    assert not social_webhooks._signature_ok(APP_SECRET, raw + b" ", f"sha256={digest}")
    assert not social_webhooks._signature_ok(APP_SECRET, raw, "")
    assert not social_webhooks._signature_ok(APP_SECRET, raw, digest)


def _app():
    """The real FastAPI app, imported lazily so the DB fixture has already
    repointed every module's DB_PATH before main.py's import side effects run."""
    from backend.main import app

    return app
