import pytest

from backend import bot_access, database


@pytest.fixture()
def access_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "access.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(bot_access, "DB_PATH", db_path)
    monkeypatch.setattr(bot_access, "_local_rate_buckets", {})
    # No Valkey in the test environment; skip the connect timeout entirely and
    # exercise the in-process fallback path.
    monkeypatch.setattr(bot_access, "_valkey_unavailable_until", float("inf"))
    database.init_db()
    bot_access._init_schema()
    return db_path


@pytest.fixture()
def plan(access_db):
    return bot_access.create_plan(
        name="Консультация",
        period="monthly",
        limit_usd=1.0,
        limit_tokens=10_000,
        limit_messages=5,
        rate_limit_per_min=100,
    )


def _issue(plan_id=None, **kwargs):
    return bot_access.issue_token(
        binding_id="bind-1", subagent_id="agent-consult", plan_id=plan_id, **kwargs
    )


# ── tokens ──────────────────────────────────────────────────────────────────

def test_plaintext_is_returned_once_and_never_stored(access_db, plan):
    token = _issue(plan["id"], label="Иван")
    plaintext = token["plaintext"]
    assert plaintext.startswith("HRM-")

    fetched = bot_access.get_token(token["id"])
    assert "plaintext" not in fetched
    assert "token_hash" not in fetched
    assert fetched["display"].startswith("HRM-")
    # The hash still resolves the original, so lookups keep working.
    assert bot_access.find_token_by_plaintext(plaintext)["id"] == token["id"]


def test_tokens_cannot_target_the_main_agent(access_db):
    with pytest.raises(ValueError, match="main agent"):
        bot_access.issue_token(binding_id="bind-1", subagent_id="jarvis")


def test_bulk_issue_produces_distinct_tokens(access_db, plan):
    tokens = bot_access.issue_tokens_bulk(
        5, binding_id="bind-1", subagent_id="agent-consult", plan_id=plan["id"]
    )
    assert len({t["plaintext"] for t in tokens}) == 5
    assert len(bot_access.list_tokens(binding_id="bind-1")) == 5


# ── redemption ──────────────────────────────────────────────────────────────

def test_redeeming_creates_an_account_with_its_own_session(access_db, plan):
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"], "555", "@ivan")
    assert subscriber["token_id"] == token["id"]
    assert subscriber["session_id"] == "tgbot:bind-1:555"
    assert subscriber["status"] == "active"


def test_redeeming_twice_from_the_same_chat_is_idempotent(access_db, plan):
    token = _issue(plan["id"])
    first = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    second = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    assert first["id"] == second["id"]


def test_a_single_chat_token_cannot_be_shared_with_a_second_chat(access_db, plan):
    token = _issue(plan["id"], max_chats=1)
    bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    with pytest.raises(bot_access.RedemptionError, match="другом чате"):
        bot_access.redeem("bind-1", "telegram", "666", token["plaintext"])


def test_max_chats_allows_a_shared_token_when_the_owner_says_so(access_db, plan):
    token = _issue(plan["id"], max_chats=3)
    for chat in ("1", "2", "3"):
        bot_access.redeem("bind-1", "telegram", chat, token["plaintext"])
    with pytest.raises(bot_access.RedemptionError):
        bot_access.redeem("bind-1", "telegram", "4", token["plaintext"])


def test_a_token_is_bound_to_its_own_bot(access_db, plan):
    token = _issue(plan["id"])
    with pytest.raises(bot_access.RedemptionError, match="не распознан"):
        bot_access.redeem("bind-OTHER", "telegram", "555", token["plaintext"])


def test_revoking_locks_out_a_chat_that_already_redeemed(access_db, plan):
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    bot_access.revoke_token(token["id"])

    assert bot_access.get_subscriber(subscriber["id"])["status"] == "blocked"
    check = bot_access.check_access(token["id"], subscriber["id"], 10)
    assert check["allowed"] is False
    assert "отозван" in check["reason"]


def test_expired_and_suspended_tokens_are_refused(access_db, plan):
    expired = _issue(plan["id"], expires_at="2000-01-01T00:00:00+00:00")
    with pytest.raises(bot_access.RedemptionError, match="истёк"):
        bot_access.redeem("bind-1", "telegram", "1", expired["plaintext"])

    suspended = _issue(plan["id"])
    bot_access.update_token(suspended["id"], status="suspended")
    with pytest.raises(bot_access.RedemptionError, match="приостановлен"):
        bot_access.redeem("bind-1", "telegram", "2", suspended["plaintext"])


# ── limits ──────────────────────────────────────────────────────────────────

def test_spend_limit_stops_the_token(access_db, plan):
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])

    assert bot_access.check_access(token["id"], subscriber["id"], 10)["allowed"] is True
    bot_access.record_usage(subscriber, token["id"], "agent-consult", 1000, 500, cost_usd=1.5)

    check = bot_access.check_access(token["id"], subscriber["id"], 10)
    assert check["allowed"] is False
    assert "Лимит по этому токену" in check["reason"]


def test_token_count_limit_stops_the_token(access_db):
    plan = bot_access.create_plan(name="Мини", limit_tokens=1000, rate_limit_per_min=0)
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])

    bot_access.record_usage(subscriber, token["id"], "agent-consult", 800, 300, cost_usd=0.0)
    check = bot_access.check_access(token["id"], subscriber["id"], 10)
    assert check["allowed"] is False
    assert "количеству токенов" in check["reason"]


def test_message_limit_stops_the_token(access_db):
    plan = bot_access.create_plan(name="3 вопроса", limit_messages=2, rate_limit_per_min=0)
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])

    for _ in range(2):
        bot_access.record_usage(subscriber, token["id"], "agent-consult", 10, 10, cost_usd=0.0)
    check = bot_access.check_access(token["id"], subscriber["id"], 10)
    assert check["allowed"] is False
    assert "числу сообщений" in check["reason"]


def test_per_token_limit_overrides_the_plan(access_db, plan):
    token = _issue(plan["id"], limit_usd=10.0)  # plan says 1.0, this holder gets 10
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    bot_access.record_usage(subscriber, token["id"], "agent-consult", 100, 100, cost_usd=2.0)
    assert bot_access.check_access(token["id"], subscriber["id"], 10)["allowed"] is True


def test_rate_limit_kicks_in(access_db):
    plan = bot_access.create_plan(name="Медленный", rate_limit_per_min=2)
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])

    assert bot_access.check_access(token["id"], subscriber["id"], 5)["allowed"] is True
    assert bot_access.check_access(token["id"], subscriber["id"], 5)["allowed"] is True
    third = bot_access.check_access(token["id"], subscriber["id"], 5)
    assert third["allowed"] is False
    assert "Слишком много сообщений" in third["reason"]


def test_oversized_messages_are_refused_before_any_llm_call(access_db):
    plan = bot_access.create_plan(name="Короткий", max_message_chars=100, rate_limit_per_min=0)
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    check = bot_access.check_access(token["id"], subscriber["id"], 101)
    assert check["allowed"] is False
    assert "слишком длинное" in check["reason"]


def test_a_token_with_no_plan_is_unlimited_but_still_accounted(access_db):
    token = _issue(None)
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    bot_access.record_usage(subscriber, token["id"], "agent-consult", 5000, 5000, cost_usd=50.0)
    assert bot_access.check_access(token["id"], subscriber["id"], 10)["allowed"] is True
    assert bot_access.get_token(token["id"])["used_usd"] == pytest.approx(50.0)


def test_period_rollover_zeroes_the_counters(access_db, monkeypatch):
    plan = bot_access.create_plan(name="Месячный", period="monthly", limit_usd=1.0, rate_limit_per_min=0)
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    bot_access.record_usage(subscriber, token["id"], "agent-consult", 10, 10, cost_usd=2.0)
    assert bot_access.check_access(token["id"], subscriber["id"], 10)["allowed"] is False

    # Pretend the counters were last reset in a previous month.
    with bot_access._connect() as connection:
        connection.execute(
            "UPDATE bot_access_tokens SET period_started_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (token["id"],),
        )
    check = bot_access.check_access(token["id"], subscriber["id"], 10)
    assert check["allowed"] is True
    assert bot_access.get_token(token["id"])["used_usd"] == 0


def test_lifetime_period_never_rolls_over(access_db):
    plan = bot_access.create_plan(name="Разовый", period="lifetime", limit_usd=1.0, rate_limit_per_min=0)
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    bot_access.record_usage(subscriber, token["id"], "agent-consult", 10, 10, cost_usd=2.0)
    with bot_access._connect() as connection:
        connection.execute(
            "UPDATE bot_access_tokens SET period_started_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (token["id"],),
        )
    assert bot_access.check_access(token["id"], subscriber["id"], 10)["allowed"] is False


# ── plans ───────────────────────────────────────────────────────────────────

def test_a_plan_cannot_grant_a_server_tool(access_db):
    with pytest.raises(ValueError, match="public channels"):
        bot_access.create_plan(name="Опасный", allowed_tools=["execute_command"])


def test_a_plan_in_use_cannot_be_deleted(access_db, plan):
    _issue(plan["id"])
    with pytest.raises(ValueError, match="active token"):
        bot_access.delete_plan(plan["id"])


# ── accounting & reporting ──────────────────────────────────────────────────

def test_usage_ledger_and_summary(access_db, plan):
    token = _issue(plan["id"], label="Клиент А")
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    bot_access.record_usage(subscriber, token["id"], "agent-consult", 120, 40, 0.01, model="gpt-4o-mini")
    bot_access.record_usage(subscriber, token["id"], "agent-consult", 80, 20, 0.005, model="gpt-4o-mini")

    summary = bot_access.token_usage_summary(token["id"])
    assert summary["lifetime"]["turns"] == 2
    assert summary["lifetime"]["tokens_in"] == 200
    assert summary["lifetime"]["tokens_out"] == 60
    assert summary["token"]["used_messages"] == 2
    assert summary["token"]["used_tokens_in"] == 200
    assert len(summary["subscribers"]) == 1


def test_blocked_turns_are_recorded_with_zero_spend(access_db, plan):
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    bot_access.record_usage(subscriber, token["id"], "agent-consult", 0, 0, 0.0, status="blocked", detail="limit")

    summary = bot_access.token_usage_summary(token["id"])
    assert summary["recent"][0]["status"] == "blocked"
    assert summary["lifetime"]["cost_usd"] == 0


def test_subscriber_card_carries_the_conversation(access_db, plan):
    token = _issue(plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"], display_name="@ivan")
    database.save_message(subscriber["session_id"], "user", "Здравствуйте")
    database.save_message(subscriber["session_id"], "assistant", "Добрый день!")
    bot_access.update_subscriber(subscriber["id"], notes="ВИП", profile={"город": "Хайфа"})

    card = bot_access.subscriber_card(subscriber["id"])
    assert card["subscriber"]["display_name"] == "@ivan"
    assert card["subscriber"]["profile"] == {"город": "Хайфа"}
    assert card["subscriber"]["notes"] == "ВИП"
    assert [m["content"] for m in card["conversation"]] == ["Здравствуйте", "Добрый день!"]


def test_overview_counts_across_bots(access_db, plan):
    first = _issue(plan["id"])
    second = _issue(plan["id"])
    bot_access.redeem("bind-1", "telegram", "1", first["plaintext"])
    bot_access.revoke_token(second["id"])

    report = bot_access.overview()
    assert report["tokens"]["total"] == 2
    assert report["tokens"]["active"] == 1
    assert report["tokens"]["revoked"] == 1
    assert report["subscribers"]["total"] == 1


def test_brute_force_guard_mutes_a_chat(access_db):
    for _ in range(bot_access.TOKEN_ATTEMPT_LIMIT):
        assert bot_access.note_failed_attempt("telegram", "999") is False
    assert bot_access.note_failed_attempt("telegram", "999") is True


def test_every_generated_token_is_recognised_by_the_pattern(access_db):
    """Regression: token_urlsafe could end in '-' or '_', and the old trailing
    \\b in TOKEN_PATTERN never matches after a non-word character — roughly one
    token in sixteen was silently unrecognisable when pasted into a chat."""
    for _ in range(200):
        plaintext, _hash, _prefix = bot_access._generate_token()
        assert bot_access.TOKEN_PATTERN.fullmatch(plaintext), plaintext
        assert bot_access.TOKEN_PATTERN.search(f"Мой токен: {plaintext} — спасибо")


def test_the_pattern_does_not_match_a_fragment_of_a_longer_string(access_db):
    assert bot_access.TOKEN_PATTERN.search("xxHRM-" + "a" * 30) is None


def test_a_narrowed_plan_can_be_widened_back_to_the_default_tool_set(access_db):
    plan = bot_access.create_plan(name="Узкий", allowed_tools=["web_search"])
    assert plan["allowed_tools"] == ["web_search"]

    # None here means "the default public set", not "leave it alone" — the UI's
    # «набор по умолчанию» checkbox has to be able to undo a narrowing.
    widened = bot_access.update_plan(plan["id"], allowed_tools=None)
    assert widened["allowed_tools"] is None

    # A field that simply isn't mentioned is still left untouched.
    renamed = bot_access.update_plan(plan["id"], name="Переименован")
    assert renamed["name"] == "Переименован"
    assert renamed["allowed_tools"] is None


# ── plan ↔ agent assignment ──────────────────────────────────────────────────

def test_a_plan_defaults_to_any_agent(access_db, plan):
    assert plan["subagent_id"] is None


def test_a_plan_can_be_assigned_to_a_real_agent(access_db):
    database.save_subagent(id="researcher", name="Researcher", system_prompt="", model="llama3")
    scoped = bot_access.create_plan(name="Для researcher", subagent_id="researcher")
    assert scoped["subagent_id"] == "researcher"


def test_a_plan_cannot_be_assigned_to_an_unknown_agent(access_db):
    with pytest.raises(KeyError):
        bot_access.create_plan(name="Сирота", subagent_id="does-not-exist")


def test_a_plan_cannot_be_assigned_to_the_main_agent(access_db):
    database.save_subagent(id="jarvis", name="Vexa", system_prompt="", model="llama3")
    with pytest.raises(ValueError, match="main agent"):
        bot_access.create_plan(name="Для векса", subagent_id="jarvis")


def test_updating_a_plan_can_assign_and_then_clear_the_agent(access_db, plan):
    database.save_subagent(id="researcher", name="Researcher", system_prompt="", model="llama3")
    assigned = bot_access.update_plan(plan["id"], subagent_id="researcher")
    assert assigned["subagent_id"] == "researcher"

    # Explicit None is "any agent again", not "leave it alone" — same contract
    # as allowed_tools, since the edit form always resubmits the full state.
    cleared = bot_access.update_plan(plan["id"], subagent_id=None)
    assert cleared["subagent_id"] is None

    # A field simply absent from the call is untouched.
    untouched = bot_access.update_plan(plan["id"], name=plan["name"])
    assert untouched["subagent_id"] is None


def test_a_plan_assigned_to_one_agent_refuses_a_token_for_another(access_db):
    database.save_subagent(id="researcher", name="Researcher", system_prompt="", model="llama3")
    scoped = bot_access.create_plan(name="Для researcher", subagent_id="researcher")
    with pytest.raises(ValueError, match="different agent"):
        bot_access.issue_token("bind-1", "some-other-agent", plan_id=scoped["id"])
    # The matching agent works fine.
    token = bot_access.issue_token("bind-1", "researcher", plan_id=scoped["id"])
    assert token["plan_id"] == scoped["id"]


def test_a_plan_with_no_agent_assignment_works_for_anyone(access_db, plan):
    token = bot_access.issue_token("bind-1", "whichever-agent", plan_id=plan["id"])
    assert token["plan_id"] == plan["id"]
