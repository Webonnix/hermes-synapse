"""take_message: the agent writes down who wants the owner and why — validated, scoped
to the conversation the turn belongs to, and reachable on a public channel without
opening anything else there."""

import json

import pytest

from backend import agent, bot_access_gate, client_tasks, clients, control_plane, inbox, tool_permissions
from backend.tests.inbox_helpers import inbox_env  # noqa: F401 — pytest fixture
from backend.tools import TOOLS_SCHEMA


def _conversation(inbox_env, with_client=False):
    decision = bot_access_gate.authorize("bind-1", "telegram", "555", "Перезвоните мне", "555", "@ivan")
    client = None
    if with_client:
        client = clients.create_client({"name": "Иван", "type": "PERSON"})
        inbox.link_client(decision.conversation_id, client_id=client["id"])
    return decision, client


@pytest.mark.parametrize("arguments, missing", [
    ({"reason": "перезвонить по заказу"}, "имя"),
    ({"name": "12345", "reason": "перезвонить по заказу"}, "имя"),
    ({"name": "Иван", "reason": "!!"}, "суть"),
    ({"name": "Иван", "reason": "по заказу", "callback": "@@@"}, "контакт"),
])
def test_invented_or_empty_arguments_come_back_as_a_question_not_a_record(inbox_env, arguments, missing):
    decision, _ = _conversation(inbox_env)
    result = json.loads(inbox.take_message(decision.session_id, arguments))

    assert result["status"] == "needs_input"
    assert missing in result["message"]
    assert [m["speaker"] for m in inbox.list_messages(decision.conversation_id)["items"]] == ["customer"]


def test_outside_a_channel_conversation_the_tool_refuses_in_words(inbox_env):
    result = json.loads(inbox.take_message("dashboard", {"name": "Иван", "reason": "перезвонить"}))
    assert result["status"] == "unavailable"


def test_a_message_becomes_a_note_a_crm_task_and_one_owner_ping(inbox_env):
    decision, client = _conversation(inbox_env, with_client=True)

    result = json.loads(inbox.take_message(decision.session_id, {
        "name": "Иван", "reason": "Хочет обсудить опт", "callback": "+7 900 123-45-67", "urgency": "high",
    }))

    assert result["status"] == "ok"
    note = inbox.list_messages(decision.conversation_id)["items"][-1]
    assert note["speaker"] == "system" and "Хочет обсудить опт" in note["text"] and "+7 900 123-45-67" in note["text"]
    task = client_tasks.get_task(result["task_id"])
    assert (task["clientId"] if "clientId" in task else task["client_id"]) == client["id"]
    assert task["priority"] == "HIGH"
    assert len(inbox_env.notifications) == 1


def test_without_a_linked_client_the_message_is_still_recorded_and_delivered(inbox_env):
    decision, _ = _conversation(inbox_env)
    result = json.loads(inbox.take_message(decision.session_id, {"name": "Иван", "reason": "Нужна консультация"}))
    assert result["status"] == "ok" and result["task_id"] is None
    assert len(inbox_env.notifications) == 1


def test_it_runs_through_the_control_plane_without_waiting_for_approval(inbox_env):
    decision, _ = _conversation(inbox_env)
    raw = control_plane.execute_governed_tool(
        "take_message", {"name": "Иван", "reason": "Перезвонить"}, chat_id=decision.session_id,
        principal=tool_permissions.PUBLIC_CHANNEL,
    )
    assert json.loads(raw)["status"] == "ok"


def test_a_public_channel_gains_this_tool_and_nothing_else_that_writes():
    assert tool_permissions.is_tool_allowed(tool_permissions.PUBLIC_CHANNEL, "take_message")
    assert not tool_permissions.is_tool_allowed(tool_permissions.PUBLIC_CHANNEL, "add_calendar_event")
    assert not tool_permissions.is_tool_allowed(tool_permissions.PUBLIC_CHANNEL, "add_todoist_task")


def test_a_callback_request_phrasing_exposes_the_tool_to_the_model():
    selected = {t["function"]["name"] for t in agent._select_tools_for_query("Перезвоните мне, пожалуйста", TOOLS_SCHEMA)}
    assert "take_message" in selected
    schema = next(t for t in TOOLS_SCHEMA if t["function"]["name"] == "take_message")
    assert schema["function"]["parameters"]["required"] == ["name", "reason"]
