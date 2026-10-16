"""Shared fixtures for the inbox test modules (archive, rules, routing, voice).

Everything the inbox touches is pointed at one temporary database, the messenger
governance module is replaced by a stand-in binding (no real bot, no Valkey), and the
owner's Telegram ping is recorded instead of sent.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, List

import pytest

from backend import (
    bot_access,
    channel_replies,
    client_activity,
    client_tasks,
    clients,
    control_plane,
    conversation_state,
    database,
    inbox,
    inbox_routing,
    inbox_rules,
    voice_providers,
)


class InboxEnv:
    def __init__(self, db_path: str, monkeypatch):
        self.db_path = db_path
        self.monkeypatch = monkeypatch
        self.binding: Dict[str, Any] = {}
        self.notifications: List[Dict[str, Any]] = []
        self.secrets: Dict[str, str] = {}

    def bind(self, **overrides) -> Dict[str, Any]:
        binding = {
            "id": "bind-1",
            "subagent_id": "agent-1",
            "platform": "telegram",
            "bot_username": "shop_bot",
            "status": "active",
            "access_mode": "owner_only",
            "allowed_chat_ids": [],
            "welcome_message": None,
            "response_mode": "auto_labeled",
        }
        binding.update(overrides)
        self.binding = binding
        env = self

        def get_binding(binding_id):
            return env.binding if binding_id == env.binding.get("id") else None

        def notify_owner_escalation(binding, platform, chat_id, requester, excerpt, reason=""):
            env.notifications.append({
                "binding": binding.get("id"), "platform": platform, "chat_id": chat_id,
                "requester": requester, "excerpt": excerpt, "reason": reason,
            })
            return True

        module = types.ModuleType("backend.agent_messenger_governance")
        module.get_binding = get_binding
        module.notify_owner_escalation = notify_owner_escalation
        module.apply_binding_overrides = lambda subagent, overrides: subagent
        module.auto_reply_disclosure = lambda binding_id: ""
        self.monkeypatch.setitem(sys.modules, "backend.agent_messenger_governance", module)
        return binding


@pytest.fixture()
def inbox_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "inbox.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    for module in (bot_access, conversation_state, channel_replies, clients, client_tasks, client_activity, control_plane):
        monkeypatch.setattr(module, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(bot_access, "_local_rate_buckets", {})
    monkeypatch.setattr(bot_access, "_valkey_unavailable_until", float("inf"))
    for module in (clients, client_tasks, client_activity):
        if hasattr(module, "reset_schema_cache"):
            module.reset_schema_cache()
    for module in (inbox, inbox_rules, inbox_routing, voice_providers):
        module.reset_schema_cache()
    monkeypatch.setattr(inbox, "_last_notified", {})
    monkeypatch.setattr(inbox, "_event_loop", None)
    database.init_db()
    bot_access._init_schema()
    channel_replies._init_schema()

    env = InboxEnv(db_path, monkeypatch)
    monkeypatch.setattr(
        "backend.valkey_client.set_value",
        lambda key, value, ttl_seconds=None: env.secrets.__setitem__(key, value),
    )
    monkeypatch.setattr("backend.valkey_client.get_value", lambda key: env.secrets.get(key))
    monkeypatch.setattr("backend.valkey_client.delete_value", lambda key: env.secrets.pop(key, None))
    database.save_subagent(id="agent-1", name="Консультант", system_prompt="", model="llama3")
    database.save_subagent(id="agent-2", name="Продажи", system_prompt="", model="llama3")
    env.bind()
    return env
