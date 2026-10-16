"""The main agent's own handles on the autonomous board.

Before these tools existed, "сделай сайт" in chat had nowhere to go: the owner
had to open the Kanban board and create the card by hand. And before web_search
reached DEV_RUN_TOOLS, a brief that said "find the real models and prices" was
not merely done badly — it was unexecutable, so the executor silently wrote the
page from the model's own recollection.
"""

import json

import pytest

from backend import autonomy, control_plane, database, dev_runs, tool_permissions, tools
from backend.agent import TOOL_INTENT_BUNDLES, TOOL_INTENT_KEYWORDS, _select_tools_for_query


@pytest.fixture()
def runs_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "dev-runs.db")
    for module in (database, control_plane, dev_runs, autonomy):
        monkeypatch.setattr(module, "DB_PATH", db_path)
    database.init_db()
    return db_path


# ── start_dev_run / list_dev_runs ───────────────────────────────────────────

def test_start_dev_run_creates_and_starts_a_card(runs_db):
    payload = json.loads(tools.start_dev_run(
        goal="Сайт салона BYD: каталог моделей с реальными характеристиками.",
        title="Салон BYD",
        discipline="web",
    ))
    assert "error" not in payload
    run = dev_runs.get_run(payload["run_id"])
    # start=True means the card must not be left sitting in the backlog.
    assert run["status"] != "backlog"
    assert run["discipline"] == "web"
    assert run["goal"].startswith("Сайт салона BYD")


def test_start_dev_run_rejects_an_empty_goal(runs_db):
    payload = json.loads(tools.start_dev_run(goal="   ", title="Пустышка"))
    assert "error" in payload
    assert dev_runs.list_runs() == []


def test_start_dev_run_truncates_an_overlong_title(runs_db):
    payload = json.loads(tools.start_dev_run(goal="Бриф.", title="Ы" * 400))
    run = dev_runs.get_run(payload["run_id"])
    assert len(run["title"]) <= dev_runs.TITLE_MAX_CHARS


def test_list_dev_runs_filters_by_status(runs_db):
    tools.start_dev_run(goal="Первый бриф.", title="A")
    idle = dev_runs.create_run("Второй бриф.", title="B", start=False)

    everything = json.loads(tools.list_dev_runs())
    assert {r["title"] for r in everything["runs"]} == {"A", "B"}

    backlog = json.loads(tools.list_dev_runs(status="backlog"))
    assert [r["id"] for r in backlog["runs"]] == [idle["id"]]


def test_list_dev_runs_honours_the_limit(runs_db):
    for i in range(4):
        dev_runs.create_run(f"Бриф {i}.", title=f"T{i}", start=False)
    assert len(json.loads(tools.list_dev_runs(limit=2))["runs"]) == 2


# ── Governance ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool_name", ["start_dev_run", "list_dev_runs"])
def test_autonomous_board_is_owner_only(tool_name):
    """Hours of the owner's GPU and a publish to the owner's public /demo/ path
    are not a token holder's or a sub-agent's to spend."""
    assert tool_permissions.is_tool_allowed(tool_permissions.OWNER, tool_name)
    assert not tool_permissions.is_tool_allowed(tool_permissions.SUBAGENT, tool_name)
    assert not tool_permissions.is_tool_allowed(tool_permissions.PUBLIC_CHANNEL, tool_name)


def test_start_dev_run_needs_an_approval():
    # R4 is the default for anything unregistered, so this also proves the tool
    # was classified deliberately rather than falling through.
    assert control_plane.TOOL_RISKS["start_dev_run"] == "R3"
    assert control_plane.TOOL_RISKS["list_dev_runs"] == "R0"


# ── Reachability: a tool the model never sees may as well not exist ─────────

def test_building_a_product_exposes_the_autonomous_runner():
    from backend.tools import TOOLS_SCHEMA
    names = {
        t["function"]["name"]
        for t in _select_tools_for_query("Сделай сайт автосалона BYD", TOOLS_SCHEMA)
    }
    assert {"start_dev_run", "list_dev_runs"} <= names
    # Research travels with development: writing an API signature or a product
    # spec from memory is the failure this bundle exists to prevent.
    assert {"web_search", "browser_read"} <= names
    # And it must be able to ship what it built, not just write it.
    assert {"dev_publish_demo", "dev_review_demo"} <= names


def test_reading_a_site_is_not_hijacked_by_the_dev_bundle():
    """"почитай сайт X" is research, not development — the bare word "сайт"
    must not pull in the whole dev toolchain and route away from the researcher."""
    msg = "почитай сайт byd.com и расскажи что там"
    assert not any(kw in msg for kw in TOOL_INTENT_BUNDLES["dev"]["keywords"])


def test_every_tool_is_reachable_by_keyword():
    """`list_tools` is the deliberate exception: it is the discovery escape
    hatch and is appended to every selection by _with_discovery_tool. The in-call
    tools (hang up, transfer, free slots…) are not chat tools at all: agent.py
    adds them only while a phone call is live."""
    from backend.phone_context import PHONE_TOOL_NAMES
    from backend.tools import TOOLS_SCHEMA

    covered = set(TOOL_INTENT_KEYWORDS)
    for bundle in TOOL_INTENT_BUNDLES.values():
        covered |= set(bundle["tools"])
    everything = {t["function"]["name"] for t in TOOLS_SCHEMA}
    assert everything - covered - set(PHONE_TOOL_NAMES) == {"list_tools"}


# ── The executor's own toolbox ──────────────────────────────────────────────

def test_dev_runs_can_research():
    assert "web_search" in dev_runs.DEV_RUN_TOOLS
    assert "browser_read" in dev_runs.DEV_RUN_TOOLS


def test_dev_runs_cannot_drive_a_browser_unattended():
    """browser_task clicks and submits forms and is R3 — an autonomous run would
    park on an approval prompt, or act outside the sandbox without one."""
    assert "browser_task" not in dev_runs.DEV_RUN_TOOLS


def test_executor_is_told_not_to_invent_facts():
    prompt = dev_runs.EXECUTOR_SYSTEM_PROMPT
    assert "web_search" in prompt and "never from" in prompt
