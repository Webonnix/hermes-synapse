"""Tests for discipline classification and specialist routing.

A board that cannot tell a landing page from a market analysis can only assign
by availability, which is round-robin with extra steps. These cover the closed
vocabulary, the free-text inference that keeps the picker optional, and the
ranking rule that makes qualification beat idleness.
"""

import json

import pytest

from backend import autonomy, control_plane, database, dev_runs, disciplines


# ── The vocabulary itself ─────────────────────────────────────────────────────

def test_every_discipline_is_fully_specified():
    """A half-filled entry would silently route work to an agent briefed with
    an empty string."""
    for key, entry in disciplines.DISCIPLINES.items():
        assert entry["label_ru"] and entry["label_en"], key
        assert entry["keywords_ru"] and entry["keywords_en"], key
        assert len(entry["briefing"]) > 100, f"{key} briefing is too thin to steer anything"


def test_catalog_is_localized_and_ordered():
    ru = disciplines.catalog("ru")
    en = disciplines.catalog("en")
    assert [d["id"] for d in ru] == [d["id"] for d in en] == disciplines.discipline_ids()
    assert ru[0]["label"] == "Разработка сайта"
    assert en[0]["label"] == "Web development"


def test_unknown_disciplines_are_rejected_not_silently_accepted():
    assert disciplines.is_valid("web") is True
    assert disciplines.is_valid("webdev") is False   # near-miss must not pass
    assert disciplines.is_valid(None) is False
    assert disciplines.is_valid("") is False


def test_label_and_briefing_degrade_for_an_unclassified_card():
    assert disciplines.label(None, "ru") == "Общая задача"
    assert disciplines.label(None, "en") == "General"
    assert disciplines.briefing(None) == ""


# ── Inference from free text ──────────────────────────────────────────────────

@pytest.mark.parametrize("goal, expected", [
    ("Собери лендинг для стартапа", "web"),
    ("Build a React landing page with Tailwind", "web"),
    ("Нужен отчёт со статистикой по продажам", "analytics"),
    ("Build a dashboard of our key metrics", "analytics"),
    ("Продумай SEO продвижение и рекламную кампанию", "marketing"),
    ("Напиши текст для рассылки", "content"),
    ("Настрой деплой через docker и мониторинг", "devops"),
    ("Сделай телеграм-бота с интеграцией по API", "automation"),
    ("Спроектируй схему базы данных и эндпоинты", "backend"),
    ("Изучи конкурентов и сравни их предложения", "research"),
])
def test_detection_reads_the_field_out_of_russian_and_english_goals(goal, expected):
    assert disciplines.detect_discipline(goal) == expected


def test_detection_handles_russian_inflection():
    """Stems are deliberately truncated — Russian inflects heavily and listing
    every case ending by hand would rot immediately."""
    for goal in ("нужна аналитика", "займись аналитикой", "аналитический отчёт"):
        assert disciplines.detect_discipline(goal) == "analytics", goal


def test_detection_returns_none_rather_than_guessing():
    assert disciplines.detect_discipline("Сделай что-нибудь полезное") is None
    assert disciplines.detect_discipline("") is None


# ── Routing ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def runs_db(tmp_path, monkeypatch):
    """Isolated DB with the built-in specialists cleared out, so each test
    controls the whole candidate pool — init_db seeds 11 default agents and a
    ranking assertion is meaningless if half the field is invisible."""
    import sqlite3

    db_path = str(tmp_path / "runs.db")
    for module in (database, control_plane, dev_runs, autonomy):
        monkeypatch.setattr(module, "DB_PATH", db_path)
    database.init_db()
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM subagents")
    conn.commit()
    conn.close()
    return db_path


def _agent(runs_db, agent_id, covered, busy_cards=0):
    database.save_subagent(agent_id, agent_id, "prompt", "model", disciplines=covered)
    for i in range(busy_cards):
        dev_runs.create_run(f"busy work {agent_id} {i}", assignee_agent_id=agent_id, start=True)


def test_a_specialist_wins_over_an_idle_generalist(runs_db):
    """Qualification beats availability — the whole point of the feature."""
    _agent(runs_db, "generalist", [], busy_cards=0)
    _agent(runs_db, "web-specialist", ["web"], busy_cards=3)
    assert dev_runs.auto_assign_agent("web") == "web-specialist"


def test_load_breaks_ties_within_a_qualification_tier(runs_db):
    _agent(runs_db, "web-a", ["web"], busy_cards=2)
    _agent(runs_db, "web-b", ["web"], busy_cards=0)
    assert dev_runs.auto_assign_agent("web") == "web-b"


def test_a_generalist_takes_the_card_when_nobody_claims_the_discipline(runs_db):
    """An unclaimed discipline must not leave the card unassignable — everyone
    is in the same tier, so it falls through to plain least-busy."""
    _agent(runs_db, "analytics-only", ["analytics"], busy_cards=2)
    _agent(runs_db, "generalist", [], busy_cards=0)
    assert dev_runs.auto_assign_agent("web") == "generalist"


def test_assignment_without_a_discipline_is_pure_least_busy(runs_db):
    _agent(runs_db, "busy", ["web"], busy_cards=3)
    _agent(runs_db, "free", [], busy_cards=0)
    assert dev_runs.auto_assign_agent(None) == "free"


def test_disabled_agents_are_never_assigned_however_qualified(runs_db):
    database.save_subagent("web-specialist", "n", "p", "m", disciplines=["web"], is_enabled=False)
    _agent(runs_db, "generalist", [])
    assert dev_runs.auto_assign_agent("web") == "generalist"


def test_an_agent_with_corrupt_disciplines_json_is_treated_as_a_generalist(runs_db):
    """A malformed column must degrade, not crash assignment for everyone."""
    _agent(runs_db, "broken", [])
    import sqlite3
    conn = sqlite3.connect(runs_db)
    conn.execute("UPDATE subagents SET disciplines = 'not json' WHERE id = 'broken'")
    conn.commit()
    conn.close()
    assert dev_runs.auto_assign_agent("web") == "broken"


# ── Discipline on the card ────────────────────────────────────────────────────

def test_a_cards_discipline_is_inferred_when_not_given(runs_db):
    _agent(runs_db, "someone", [])
    run = dev_runs.create_run("Собери лендинг для клиента")
    assert run["discipline"] == "web"


def test_an_explicit_discipline_overrides_inference(runs_db):
    _agent(runs_db, "someone", [])
    run = dev_runs.create_run("Собери лендинг для клиента", discipline="marketing")
    assert run["discipline"] == "marketing"


def test_an_invalid_discipline_falls_back_to_inference(runs_db):
    _agent(runs_db, "someone", [])
    run = dev_runs.create_run("Собери лендинг", discipline="nonsense")
    assert run["discipline"] == "web"


def test_a_continuation_keeps_its_parents_discipline(runs_db):
    """A follow-up reading 'make the header sticky' has no web keywords of its
    own and would otherwise be reclassified as a general task."""
    _agent(runs_db, "someone", [])
    root = dev_runs.create_run("Собери лендинг", discipline="web")
    child = dev_runs.create_run("сделай шапку липкой", parent_run_id=root["id"])
    assert child["discipline"] == "web"


def test_creating_a_card_routes_it_to_the_matching_specialist(runs_db):
    _agent(runs_db, "generalist", [])
    _agent(runs_db, "analyst", ["analytics"])
    run = dev_runs.create_run("Подготовь отчёт со статистикой по выручке")
    assert run["discipline"] == "analytics"
    assert run["assignee_agent_id"] == "analyst"


def test_the_specialist_briefing_reaches_the_executor_prompt(runs_db):
    _agent(runs_db, "someone", [])
    run = dev_runs.create_run("Собери лендинг", discipline="web")
    prompt = dev_runs._build_messages(dev_runs.get_run(run["id"]))[1]["content"]
    assert "Specialist standards" in prompt
    assert "WEB BUILD" in prompt


def test_an_unclassified_card_carries_no_briefing_section(runs_db):
    _agent(runs_db, "someone", [])
    run = dev_runs.create_run("Сделай что-нибудь полезное")
    assert run["discipline"] is None
    prompt = dev_runs._build_messages(dev_runs.get_run(run["id"]))[1]["content"]
    assert "Specialist standards" not in prompt
