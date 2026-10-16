"""Routing rules and business hours (backend/inbox_rules.py), as the access gate
applies them to arriving senders."""

from datetime import datetime, timezone

import pytest

from backend import bot_access, bot_access_gate, database, inbox, inbox_rules
from backend.tests.inbox_helpers import inbox_env  # noqa: F401 — pytest fixture


# ── business hours ──────────────────────────────────────────────────────────

def _at(weekday_date: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{weekday_date}T{hhmm}:00")


def test_hours_parse_day_ranges_and_multiple_windows():
    windows = inbox_rules.parse_hours("mo-fr 09:00-18:00; sa 10:00-14:00")
    # 2026-09-14 is a Monday.
    assert inbox_rules.within_hours(windows, _at("2026-09-14", "09:00"))
    assert not inbox_rules.within_hours(windows, _at("2026-09-14", "18:00"))
    assert inbox_rules.within_hours(windows, _at("2026-09-19", "11:30"))
    assert not inbox_rules.within_hours(windows, _at("2026-09-20", "11:30"))


def test_an_overnight_window_belongs_to_the_day_it_starts_on():
    windows = inbox_rules.parse_hours("fr 22:00-02:00")
    assert inbox_rules.within_hours(windows, _at("2026-09-18", "23:00"))  # Friday night
    assert inbox_rules.within_hours(windows, _at("2026-09-19", "01:30"))  # early Saturday
    assert not inbox_rules.within_hours(windows, _at("2026-09-19", "23:00"))


def test_no_configured_hours_means_always_open():
    assert inbox_rules.within_hours([], _at("2026-09-20", "03:00"))


@pytest.mark.parametrize("raw", ["пн-пт 9-18", "mo-fr 25:00-26:00", "mo 10:00-10:00", "every day"])
def test_a_schedule_that_does_not_parse_is_refused_instead_of_saved(inbox_env, raw):
    with pytest.raises(ValueError):
        inbox_rules.set_binding_settings("bind-1", raw, "Europe/Moscow")


def test_an_unknown_timezone_is_refused(inbox_env):
    with pytest.raises(ValueError):
        inbox_rules.set_binding_settings("bind-1", "mo-fr 09:00-18:00", "Mars/Olympus")


def test_business_hours_are_evaluated_in_the_bindings_own_timezone(inbox_env):
    inbox_rules.set_binding_settings("bind-1", "mo-fr 09:00-18:00", "Asia/Tokyo")
    # 01:00 UTC Monday is 10:00 in Tokyo — inside hours there, not in UTC.
    moment = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
    assert inbox_rules.is_within_business_hours("bind-1", moment)


# ── matching ────────────────────────────────────────────────────────────────

def test_exact_beats_prefix_and_a_longer_prefix_beats_a_shorter_one(inbox_env):
    inbox_rules.create_rule("+7", "block", "prefix")
    inbox_rules.create_rule("+7900", "ai", "prefix")
    inbox_rules.create_rule("+79001234567", "pass", "exact")

    assert inbox_rules.evaluate("bind-1", ["+79001234567"]).configured_action == "pass"
    assert inbox_rules.evaluate("bind-1", ["+79005550000"]).configured_action == "ai"
    assert inbox_rules.evaluate("bind-1", ["+71112223344"]).configured_action == "block"
    assert inbox_rules.evaluate("bind-1", ["+44123456789"]) is None


def test_a_bindings_own_rule_beats_an_equally_specific_global_one(inbox_env):
    inbox_rules.create_rule("ivan", "block", "exact")
    inbox_rules.create_rule("ivan", "ai", "exact", binding_id="bind-1")

    assert inbox_rules.evaluate("bind-1", ["ivan"]).configured_action == "ai"
    assert inbox_rules.evaluate("bind-2", ["ivan"]).configured_action == "block"


def test_a_rule_written_against_a_phone_matches_however_the_number_is_formatted(inbox_env):
    inbox_rules.create_rule("+7 (900) 123-45-67", "block")
    assert inbox_rules.list_rules()[0]["pattern"] == "+79001234567"
    assert inbox_rules.evaluate("bind-1", [inbox.normalize_identity("79001234567")]).action == "block"


def test_a_rule_about_a_handle_matches_the_sender_by_any_identity_they_arrived_with(inbox_env):
    inbox_rules.create_rule("@Troll", "block")
    decision = bot_access_gate.authorize("bind-1", "telegram", "12345", "hi", "12345", "@troll")
    assert decision.action == bot_access_gate.IGNORE


# ── what the gate does with a decision ──────────────────────────────────────

def test_block_silences_the_sender_archives_them_and_keeps_them_out_of_agent_memory(inbox_env):
    inbox_rules.create_rule("555", "block")
    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "купи рекламу")

    assert decision.action == bot_access_gate.IGNORE
    conversation = inbox.find_conversation("bind-1", "telegram", "555")
    assert conversation["handling"] == "blocked"
    assert [m["text"] for m in inbox.list_messages(conversation["id"])["items"]] == ["купи рекламу"]
    assert database.get_chat_history("tgbot:bind-1:555") == []


def test_block_wins_over_a_valid_access_token(inbox_env):
    inbox_env.bind(access_mode="token", allowed_chat_ids=["100"])
    token = bot_access.issue_token("bind-1", "agent-1")
    assert bot_access_gate.authorize("bind-1", "telegram", "999", token["plaintext"]).action == bot_access_gate.REPLY
    assert bot_access_gate.authorize("bind-1", "telegram", "999", "вопрос").action == bot_access_gate.PROCEED

    inbox_rules.create_rule("999", "block")

    assert bot_access_gate.authorize("bind-1", "telegram", "999", "вопрос").action == bot_access_gate.IGNORE


def test_pass_inside_hours_silences_the_agent_and_tells_the_owner_once(inbox_env, monkeypatch):
    monkeypatch.setattr(inbox_rules, "is_within_business_hours", lambda binding_id, moment=None: True)
    inbox_rules.create_rule("555", "pass")

    first = bot_access_gate.authorize("bind-1", "telegram", "555", "Позовите менеджера")
    second = bot_access_gate.authorize("bind-1", "telegram", "555", "Алло?")

    assert first.action == second.action == bot_access_gate.IGNORE
    assert inbox.find_conversation("bind-1", "telegram", "555")["handling"] == "human"
    assert len(inbox_env.notifications) == 1
    # Silence, but not amnesia: the agent sees what was said once it answers again.
    assert [m["content"] for m in database.get_chat_history("tgbot:bind-1:555")] == ["Позовите менеджера", "Алло?"]


def test_pass_outside_hours_lets_the_ai_answer(inbox_env, monkeypatch):
    monkeypatch.setattr(inbox_rules, "is_within_business_hours", lambda binding_id, moment=None: False)
    inbox_rules.create_rule("555", "pass")

    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Вы работаете?")

    assert decision.action == bot_access_gate.PROCEED
    assert inbox.find_conversation("bind-1", "telegram", "555")["handling"] == "ai"
    assert inbox_env.notifications == []


def test_removing_a_rule_hands_the_sender_straight_back(inbox_env):
    rule = inbox_rules.create_rule("555", "block")
    assert bot_access_gate.authorize("bind-1", "telegram", "555", "раз").action == bot_access_gate.IGNORE

    inbox_rules.delete_rule(rule["id"])

    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "два")
    assert decision.action == bot_access_gate.PROCEED
    assert inbox.get_conversation(decision.conversation_id)["handling"] == "ai"


def test_an_unreadable_rules_table_does_not_mute_every_customer(inbox_env, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("rules table corrupt")

    monkeypatch.setattr(inbox_rules, "evaluate", broken)
    assert bot_access_gate.authorize("bind-1", "telegram", "555", "Привет").action == bot_access_gate.PROCEED


@pytest.mark.parametrize("payload", [
    {"pattern": "", "action": "block"},
    {"pattern": "555", "action": "delete"},
    {"pattern": "555", "action": "block", "match": "regex"},
])
def test_invalid_rules_are_refused(inbox_env, payload):
    with pytest.raises(ValueError):
        inbox_rules.create_rule(payload["pattern"], payload["action"], payload.get("match", "exact"))
