import json
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from backend import agent, control_plane, database, tool_permissions
from backend.tool_permissions import OWNER, PUBLIC_CHANNEL, SUBAGENT


@pytest.fixture()
def control_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "browser-sessions.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(control_plane, "DB_PATH", db_path)
    database.init_db()
    return db_path


BROWSER_SCHEMA = [
    {"type": "function", "function": {"name": name, "parameters": {}}}
    for name in ("browser_read", "browser_task", "web_search")
]


@pytest.mark.parametrize("message", [
    "Можешь зарегистрироваться в инстаграмм ?",
    "А ты можешь зайти под моим пользователем в инстаграмм ?",
    "Зайди в мой аккаунт на авито и посмотри сообщения",
    "Log in to my LinkedIn and check messages",
])
def test_website_account_requests_get_the_browser(message):
    names = {tool["function"]["name"] for tool in agent._select_tools_for_query(message, BROWSER_SCHEMA)}
    assert {"browser_read", "browser_task"} <= names


def test_browser_directive_lists_saved_logins_and_forbids_password_requests():
    with patch("backend.browser_sessions.list_profiles_cached",
               return_value=[{"name": "instagram", "start_url": "https://www.instagram.com/accounts/login/"}]):
        text = agent._browser_directive({"browser_task"})
    assert "instagram" in text
    assert "profile" in text
    assert "не проси пароль" in text
    assert "текстовый ассистент" in text
    assert agent._browser_directive({"web_search"}) == ""


def test_browser_directive_survives_a_down_runner():
    with patch("backend.browser_sessions.list_profiles_cached", side_effect=RuntimeError("down")):
        text = agent._browser_directive({"browser_read"})
    assert "Сохранённых входов пока нет" in text


@pytest.mark.parametrize(("principal", "allowed"), [(OWNER, True), (SUBAGENT, False), (PUBLIC_CHANNEL, False), (None, False)])
def test_saved_login_is_owner_only(principal, allowed):
    refusal = tool_permissions.check_tool_arguments(principal, "browser_read", {"task": "x", "profile": "instagram"})
    assert (refusal is None) is allowed
    assert tool_permissions.check_tool_arguments(principal, "browser_read", {"task": "x"}) is None


def test_control_plane_refuses_a_subagent_using_the_owners_login(control_db):
    tools_module = ModuleType("backend.tools")
    tools_module.execute_tool = MagicMock(return_value="{}")
    with patch.dict(sys.modules, {"backend.tools": tools_module}):
        result = json.loads(control_plane.execute_governed_tool(
            "browser_read", {"task": "read my feed", "profile": "instagram"}, "agent", principal=SUBAGENT,
        ))
    assert result["status"] == "forbidden"
    tools_module.execute_tool.assert_not_called()


def test_browser_tools_forward_the_profile(monkeypatch):
    from backend import tools

    sent = {}
    monkeypatch.setattr(tools, "_browser_runner_request", lambda payload, timeout=300.0: sent.update(payload) or "{}")
    tools.execute_tool("browser_task", {"task": "post", "profile": "instagram"})
    assert sent["profile"] == "instagram"
    tools.execute_tool("browser_read", {"task": "read"})
    assert sent["profile"] is None
