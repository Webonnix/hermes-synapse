import json

import pytest

from backend import bot_access, bot_access_gate, conversation_state, database


@pytest.fixture()
def gate_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "gate.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(bot_access, "DB_PATH", db_path)
    monkeypatch.setattr(bot_access, "_local_rate_buckets", {})
    monkeypatch.setattr(bot_access, "_valkey_unavailable_until", float("inf"))
    monkeypatch.setattr(conversation_state, "DB_PATH", db_path)
    database.init_db()
    bot_access._init_schema()
    return db_path


def _bind(monkeypatch, **overrides):
    """Stands in for agent_messenger_governance.get_binding so the gate can be
    tested without a real messenger binding row."""
    binding = {
        "id": "bind-1",
        "subagent_id": "agent-consult",
        "platform": "telegram",
        "bot_username": "consult_bot",
        "status": "active",
        "access_mode": "owner_only",
        "allowed_chat_ids": [],
        "welcome_message": None,
        "response_mode": "draft",
    }
    binding.update(overrides)

    module = type("M", (), {"get_binding": staticmethod(lambda binding_id: binding if binding_id == binding["id"] else None)})
    monkeypatch.setitem(
        __import__("sys").modules, "backend.agent_messenger_governance", module
    )
    return binding


# ── owner-only bindings keep their old behaviour ────────────────────────────

def test_owner_only_with_a_whitelist_ignores_strangers(gate_db, monkeypatch):
    _bind(monkeypatch, allowed_chat_ids=["100"])
    assert bot_access_gate.authorize("bind-1", "telegram", "999", "привет").action == bot_access_gate.IGNORE

    owner = bot_access_gate.authorize("bind-1", "telegram", "100", "привет")
    assert owner.action == bot_access_gate.PROCEED
    assert owner.scope == "owner"
    assert owner.session_id == "tgbot:bind-1:100"


def test_owner_only_without_a_whitelist_still_answers_everyone(gate_db, monkeypatch):
    _bind(monkeypatch, allowed_chat_ids=[])
    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "привет")
    assert decision.action == bot_access_gate.PROCEED
    assert decision.scope == "owner"


def test_an_inactive_binding_answers_nobody(gate_db, monkeypatch):
    _bind(monkeypatch, status="revoked")
    assert bot_access_gate.authorize("bind-1", "telegram", "1", "привет").action == bot_access_gate.IGNORE


# ── token mode ──────────────────────────────────────────────────────────────

def _token_binding(monkeypatch, **overrides):
    return _bind(monkeypatch, access_mode="token", allowed_chat_ids=["100"], **overrides)


def test_a_stranger_is_asked_for_a_token_before_anything_costs_money(gate_db, monkeypatch):
    _token_binding(monkeypatch)
    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте, помогите")
    assert decision.action == bot_access_gate.REPLY
    assert "токен" in decision.reply_text.lower()
    assert decision.subscriber is None


def test_the_owner_never_needs_a_token_on_their_own_bot(gate_db, monkeypatch):
    _token_binding(monkeypatch)
    decision = bot_access_gate.authorize("bind-1", "telegram", "100", "привет")
    assert decision.action == bot_access_gate.PROCEED
    assert decision.scope == "owner"


def test_a_valid_token_opens_an_account_then_lets_the_next_message_through(gate_db, monkeypatch):
    _token_binding(monkeypatch)
    plan = bot_access.create_plan(name="Базовый", rate_limit_per_min=0)
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=plan["id"])

    redeemed = bot_access_gate.authorize("bind-1", "telegram", "999", token["plaintext"])
    assert redeemed.action == bot_access_gate.REPLY
    assert redeemed.subscriber is not None

    following = bot_access_gate.authorize("bind-1", "telegram", "999", "Какой у меня вопрос?")
    assert following.action == bot_access_gate.PROCEED
    assert following.scope == "public"
    assert following.session_id == "tgbot:bind-1:999"


@pytest.mark.parametrize("template", ["{token}", "/start {token}", "Вот мой токен: {token}, спасибо"])
def test_a_token_is_recognised_bare_in_a_deep_link_or_in_a_sentence(gate_db, monkeypatch, template):
    _token_binding(monkeypatch)
    token = bot_access.issue_token("bind-1", "agent-consult")
    decision = bot_access_gate.authorize(
        "bind-1", "telegram", "777", template.format(token=token["plaintext"])
    )
    assert decision.subscriber is not None


def test_a_wrong_token_is_refused_and_eventually_mutes_the_chat(gate_db, monkeypatch):
    _token_binding(monkeypatch)
    for _ in range(bot_access.TOKEN_ATTEMPT_LIMIT):
        decision = bot_access_gate.authorize("bind-1", "telegram", "999", "HRM-" + "x" * 30)
        assert decision.action == bot_access_gate.REPLY
        assert decision.reply_text != bot_access_gate.MUTED_MESSAGE
    muted = bot_access_gate.authorize("bind-1", "telegram", "999", "HRM-" + "x" * 30)
    assert muted.reply_text == bot_access_gate.MUTED_MESSAGE


def test_an_exhausted_token_is_refused_without_calling_the_model(gate_db, monkeypatch):
    binding = _token_binding(monkeypatch)
    plan = bot_access.create_plan(name="Один вопрос", limit_messages=1, rate_limit_per_min=0)
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "999", token["plaintext"])
    bot_access.record_usage(subscriber, token["id"], binding["subagent_id"], 10, 10, 0.001)

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "ещё вопрос")
    assert decision.action == bot_access_gate.REPLY
    assert "Лимит" in decision.reply_text
    # The refusal itself is in the ledger, so the admin can see it.
    recent = bot_access.token_usage_summary(token["id"])["recent"]
    assert recent[0]["status"] == "blocked"


def test_a_blocked_subscriber_is_silently_ignored(gate_db, monkeypatch):
    _token_binding(monkeypatch)
    token = bot_access.issue_token("bind-1", "agent-consult")
    subscriber = bot_access.redeem("bind-1", "telegram", "999", token["plaintext"])
    bot_access.update_subscriber(subscriber["id"], status="blocked")
    assert bot_access_gate.authorize("bind-1", "telegram", "999", "привет").action == bot_access_gate.IGNORE


def test_token_mode_pointing_at_the_main_agent_serves_nobody(gate_db, monkeypatch):
    _bind(monkeypatch, access_mode="token", subagent_id="jarvis", allowed_chat_ids=[])
    assert bot_access_gate.authorize("bind-1", "telegram", "999", "привет").action == bot_access_gate.IGNORE


def test_email_matching_is_case_insensitive(gate_db, monkeypatch):
    _bind(monkeypatch, platform="email", access_mode="token", allowed_chat_ids=["Owner@Example.COM"])
    decision = bot_access_gate.authorize("bind-1", "email", "owner@example.com", "привет")
    assert decision.action == bot_access_gate.PROCEED
    assert decision.scope == "owner"


def test_a_gate_failure_refuses_rather_than_letting_someone_through(gate_db, monkeypatch):
    broken = type("M", (), {"get_binding": staticmethod(lambda _id: (_ for _ in ()).throw(RuntimeError("db gone")))})
    monkeypatch.setitem(__import__("sys").modules, "backend.agent_messenger_governance", broken)
    assert bot_access_gate.authorize("bind-1", "telegram", "1", "привет").action == bot_access_gate.IGNORE


# ── persona and accounting ──────────────────────────────────────────────────

def test_public_context_narrows_the_agent_and_adds_the_card(gate_db, monkeypatch):
    _token_binding(monkeypatch)
    plan = bot_access.create_plan(
        name="Юрист", system_prompt_suffix="Отвечайте только по трудовому праву.",
        allowed_tools=["web_search"], rate_limit_per_min=0,
    )
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "999", token["plaintext"], display_name="@ivan")
    bot_access.update_subscriber(subscriber["id"], profile={"город": "Хайфа"}, notes="ВИП")

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "вопрос")
    effective = bot_access_gate.apply_subscriber_context(
        {"id": "agent-consult", "system_prompt": "Вы консультант."}, decision
    )

    assert effective["id"] == "agent-consult"  # budgets still bill the real agent
    assert effective["_access_scope"] == "public"
    assert effective["_plan_allowed_tools"] == ["web_search"]
    assert "трудовому праву" in effective["system_prompt"]
    assert "@ivan" in effective["system_prompt"]
    assert "Хайфа" in effective["system_prompt"]
    assert "ВИП" in effective["system_prompt"]


def test_owner_context_leaves_the_agent_alone(gate_db, monkeypatch):
    _bind(monkeypatch)
    decision = bot_access_gate.authorize("bind-1", "telegram", "1", "привет")
    effective = bot_access_gate.apply_subscriber_context(
        {"id": "agent-consult", "system_prompt": "Вы консультант."}, decision
    )
    assert effective["system_prompt"] == "Вы консультант."
    assert effective["_access_scope"] == "owner"


def test_record_turn_bills_the_token_with_the_providers_real_counts(gate_db, monkeypatch):
    _token_binding(monkeypatch)
    token = bot_access.issue_token("bind-1", "agent-consult")
    bot_access.redeem("bind-1", "telegram", "999", token["plaintext"])
    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "вопрос")

    bot_access_gate.record_turn(
        decision,
        {"input_tokens": 1200, "output_tokens": 300, "model": "gpt-4o-mini", "latency_ms": 900},
        "ответ",
    )
    billed = bot_access.get_token(token["id"])
    assert billed["used_tokens_in"] == 1200
    assert billed["used_tokens_out"] == 300
    assert billed["used_messages"] == 1
    assert billed["used_usd"] > 0


def test_record_turn_is_a_no_op_for_the_owner(gate_db, monkeypatch):
    _bind(monkeypatch)
    decision = bot_access_gate.authorize("bind-1", "telegram", "1", "привет")
    bot_access_gate.record_turn(decision, {"input_tokens": 999, "output_tokens": 999}, "ответ")
    assert bot_access.overview()["tokens"]["total"] == 0


def test_public_turns_always_use_the_disclosed_auto_reply_mode(gate_db, monkeypatch):
    _token_binding(monkeypatch)
    token = bot_access.issue_token("bind-1", "agent-consult")
    bot_access.redeem("bind-1", "telegram", "999", token["plaintext"])
    public = bot_access_gate.authorize("bind-1", "telegram", "999", "вопрос")
    owner = bot_access_gate.authorize("bind-1", "telegram", "100", "вопрос")

    assert bot_access_gate.effective_response_mode(public, "draft") == "auto_labeled"
    assert bot_access_gate.effective_response_mode(owner, "draft") == "draft"


# ── selling access in-chat ──────────────────────────────────────────────────

@pytest.fixture()
def billing(gate_db, monkeypatch):
    from backend import payments

    monkeypatch.setattr(payments, "DB_PATH", gate_db)
    payments._init_schema()
    return payments


def test_a_stranger_is_quoted_a_price_when_the_plan_is_for_sale(gate_db, billing, monkeypatch):
    """Manual provider: no hosted page exists, so the bot quotes the price and
    points at the owner — but the invoice is still recorded."""
    plan = bot_access.create_plan(
        name="Подписка Про", price_usd=19.0, is_purchasable=True, rate_limit_per_min=0
    )
    _token_binding(monkeypatch, default_plan_id=plan["id"])

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")

    assert decision.action == bot_access_gate.REPLY
    assert "$19.00" in decision.reply_text
    invoice = billing.find_pending_invoice_for_chat("bind-1", "telegram", "999")
    assert invoice["origin"] == "bot"
    assert invoice["plan_id"] == plan["id"]


def test_a_stranger_gets_a_real_payment_link_from_the_provider(gate_db, billing, monkeypatch):
    import respx
    from httpx import Response

    plan = bot_access.create_plan(
        name="Подписка Про", price_usd=19.0, is_purchasable=True, rate_limit_per_min=0
    )
    _token_binding(monkeypatch, default_plan_id=plan["id"])
    database.set_api_key(billing.KEY_NOWPAYMENTS_API, "test-api-key")
    billing.set_config(provider="nowpayments", public_base_url="https://hermes.example.net")

    with respx.mock:
        respx.post("https://api.nowpayments.io/v1/invoice").mock(
            return_value=Response(200, json={"id": 1, "invoice_url": "https://pay.example/i/1"})
        )
        decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")

    assert "Купить доступ" in decision.reply_text
    assert "https://pay.example/i/1" in decision.reply_text


def test_a_second_hello_reuses_the_same_invoice(gate_db, billing, monkeypatch):
    plan = bot_access.create_plan(name="Про", price_usd=19.0, is_purchasable=True, rate_limit_per_min=0)
    _token_binding(monkeypatch, default_plan_id=plan["id"])

    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")
    bot_access_gate.authorize("bind-1", "telegram", "999", "Ещё раз здравствуйте")

    assert len(billing.list_invoices(binding_id="bind-1")) == 1


def test_a_free_plan_is_never_offered_for_sale(gate_db, billing, monkeypatch):
    plan = bot_access.create_plan(name="Внутренний", rate_limit_per_min=0)  # no price
    _token_binding(monkeypatch, default_plan_id=plan["id"])

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")
    assert "Купить доступ" not in decision.reply_text
    assert billing.list_invoices() == []


def test_a_billing_failure_still_asks_for_a_token(gate_db, billing, monkeypatch):
    plan = bot_access.create_plan(name="Про", price_usd=19.0, is_purchasable=True, rate_limit_per_min=0)
    _token_binding(monkeypatch, default_plan_id=plan["id"])
    monkeypatch.setattr(
        billing, "create_invoice", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider down"))
    )

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")
    # The bot must still explain itself when the payment provider is unreachable.
    assert decision.action == bot_access_gate.REPLY
    assert "токен" in decision.reply_text.lower()


def test_an_exhausted_customer_is_offered_a_renewal(gate_db, billing, monkeypatch):
    plan = bot_access.create_plan(
        name="Про", price_usd=19.0, is_purchasable=True, limit_messages=1, rate_limit_per_min=0
    )
    binding = _token_binding(monkeypatch, default_plan_id=plan["id"])
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "999", token["plaintext"])
    bot_access.create_subscription(plan, token["id"], "bind-1", binding["subagent_id"])
    bot_access.record_usage(subscriber, token["id"], "agent-consult", 10, 10, 0.001)

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "ещё вопрос")

    assert decision.action == bot_access_gate.REPLY
    assert "Лимит" in decision.reply_text
    assert "Продление" in decision.reply_text
    invoice = billing.find_pending_invoice_for_chat("bind-1", "telegram", "999")
    assert invoice["purpose"] == "renewal"
    assert invoice["subscription_id"]


def test_a_rate_limited_customer_is_not_upsold(gate_db, billing, monkeypatch):
    plan = bot_access.create_plan(name="Про", price_usd=19.0, is_purchasable=True, rate_limit_per_min=1)
    _token_binding(monkeypatch, default_plan_id=plan["id"])
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "999", token["plaintext"])
    bot_access.create_subscription(plan, token["id"], "bind-1", "agent-consult")

    bot_access_gate.authorize("bind-1", "telegram", "999", "первый")
    second = bot_access_gate.authorize("bind-1", "telegram", "999", "второй")

    assert "Слишком много сообщений" in second.reply_text
    assert "Продл" not in second.reply_text  # nothing to sell — just slow down
    assert billing.list_invoices() == []


# ── Escalation: "this needs the owner personally" ───────────────────────────

@pytest.fixture(autouse=True)
def _reset_escalation_state():
    """These caches are process-wide by design (they must survive between
    turns), so tests must reset them or they leak into each other."""
    def _clear():
        bot_access_gate._ESCALATION_OFFERED.clear()
        bot_access_gate._URGENT_NOTIFIED.clear()
        bot_access_gate._HANDLING_FIRST_MESSAGE.clear()

    _clear()
    yield
    _clear()

def _bind_with_escalation(monkeypatch, notified: list, **overrides):
    """Like _bind(), but the stub module also carries the two governance
    helpers _apply_escalation calls (auto_reply_disclosure, notify_owner_escalation)
    — the plain _bind() stub only defines get_binding, which is enough for every
    other test since escalation_enabled defaults to falsy there."""
    binding = _bind(monkeypatch, escalation_enabled=True, **overrides)
    module = __import__("sys").modules["backend.agent_messenger_governance"]
    module.auto_reply_disclosure = staticmethod(lambda binding_id: "\n\n— личный AI-ассистент.")
    module.notify_owner_escalation = staticmethod(
        lambda binding, platform, chat_id, requester, excerpt: notified.append(
            {"platform": platform, "chat_id": chat_id, "requester": requester, "excerpt": excerpt}
        )
    )
    return binding


def _classifier(answer: bool):
    async def fake(*args, **kwargs):
        return answer
    return fake


def test_escalation_disabled_by_default_never_classifies(gate_db, monkeypatch):
    """The classifier call has a real (if small) LLM cost — a binding that never
    opted in must never trigger it, not even to decide 'no'."""
    _bind(monkeypatch, allowed_chat_ids=[])  # escalation_enabled omitted -> falsy
    called = []
    monkeypatch.setattr(bot_access_gate, "_needs_owner_involvement", lambda *a, **k: called.append(1))
    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "У меня дело к вам лично")
    assert decision.action == bot_access_gate.PROCEED
    assert called == []


def test_message_needing_the_owner_gets_the_offer_instead_of_proceeding(gate_db, monkeypatch):
    notified: list = []
    _bind_with_escalation(monkeypatch, notified, allowed_chat_ids=[])
    monkeypatch.setattr(bot_access_gate, "_needs_owner_involvement", _classifier(True))

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "У меня дело к Альберту лично")
    assert decision.action == bot_access_gate.REPLY
    assert decision.reply_text.startswith(bot_access_gate._ESCALATION_OFFER_TEXT)
    assert decision.reply_text.endswith("личный AI-ассистент.")
    assert notified == []  # only on confirmation, not on the offer itself


def test_ordinary_messages_are_not_escalated(gate_db, monkeypatch):
    notified: list = []
    _bind_with_escalation(monkeypatch, notified, allowed_chat_ids=[])
    monkeypatch.setattr(bot_access_gate, "_needs_owner_involvement", _classifier(False))

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Привет, как дела?")
    assert decision.action == bot_access_gate.PROCEED


def test_confirming_the_offer_notifies_the_owner_and_stops_asking_again(gate_db, monkeypatch):
    notified: list = []
    _bind_with_escalation(monkeypatch, notified, allowed_chat_ids=[])
    monkeypatch.setattr(bot_access_gate, "_needs_owner_involvement", _classifier(True))

    offer = bot_access_gate.authorize("bind-1", "telegram", "999", "У меня дело")
    assert offer.action == bot_access_gate.REPLY

    confirm = bot_access_gate.authorize("bind-1", "telegram", "999", "Да, пожалуйста")
    assert confirm.action == bot_access_gate.REPLY
    assert confirm.reply_text.startswith(bot_access_gate._ESCALATION_CONFIRM_TEXT)
    assert len(notified) == 1
    assert notified[0]["excerpt"] == "У меня дело"

    # The offer is consumed — a normal follow-up now proceeds like any other message.
    monkeypatch.setattr(bot_access_gate, "_needs_owner_involvement", _classifier(False))
    after = bot_access_gate.authorize("bind-1", "telegram", "999", "Спасибо")
    assert after.action == bot_access_gate.PROCEED


def test_declining_the_offer_lets_the_conversation_continue(gate_db, monkeypatch):
    notified: list = []
    _bind_with_escalation(monkeypatch, notified, allowed_chat_ids=[])
    monkeypatch.setattr(bot_access_gate, "_needs_owner_involvement", _classifier(True))
    bot_access_gate.authorize("bind-1", "telegram", "999", "У меня дело")

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "нет, не надо")
    assert decision.action == bot_access_gate.PROCEED
    assert notified == []


def test_ambiguous_reply_to_the_offer_still_proceeds_without_notifying(gate_db, monkeypatch):
    """Neither a yes nor a no — err toward not paging the owner rather than
    guessing, and just let the assistant answer normally."""
    notified: list = []
    _bind_with_escalation(monkeypatch, notified, allowed_chat_ids=[])
    monkeypatch.setattr(bot_access_gate, "_needs_owner_involvement", _classifier(True))
    bot_access_gate.authorize("bind-1", "telegram", "999", "У меня дело")

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "расскажи мне анекдот")
    assert decision.action == bot_access_gate.PROCEED
    assert notified == []


# ── Handling mode: "talk to the AI now / wait for the owner" + manual takeover ──

def _bind_with_handling_menu(monkeypatch, notified: list = None, delivered: bool = True, **overrides):
    """Like _bind_with_escalation: the stub module also needs auto_reply_disclosure
    (appended to the AI-confirmation reply) and notify_owner_escalation, which
    the "срочно" option reuses to ping the owner on Telegram."""
    binding = _bind(monkeypatch, handling_menu_enabled=True, **overrides)
    module = __import__("sys").modules["backend.agent_messenger_governance"]
    module.auto_reply_disclosure = staticmethod(lambda binding_id: "\n\n— личный AI-ассистент.")

    def _notify(binding, platform, chat_id, requester, excerpt, reason=""):
        if notified is not None:
            notified.append({"platform": platform, "chat_id": chat_id, "requester": requester,
                             "excerpt": excerpt, "reason": reason})
        return delivered

    module.notify_owner_escalation = staticmethod(_notify)
    return binding


def test_handling_menu_disabled_by_default_leaves_behaviour_unchanged(gate_db, monkeypatch):
    _bind(monkeypatch, allowed_chat_ids=[])  # handling_menu_enabled omitted -> falsy
    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Привет")
    assert decision.action == bot_access_gate.PROCEED
    assert conversation_state.get_state("bind-1", "telegram", "999") is None


def test_first_message_gets_the_menu_and_waits_for_a_choice(gate_db, monkeypatch):
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")
    assert decision.action == bot_access_gate.REPLY
    assert decision.reply_text == bot_access_gate._HANDLING_MENU_TEXT
    assert conversation_state.get_state("bind-1", "telegram", "999")["handling_mode"] == "pending_choice"


def test_choosing_the_ai_confirms_then_proceeds_normally_next_turn(gate_db, monkeypatch):
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")

    chosen = bot_access_gate.authorize("bind-1", "telegram", "999", "1")
    assert chosen.action == bot_access_gate.REPLY
    assert chosen.reply_text.startswith(bot_access_gate._HANDLING_AI_CONFIRM_TEXT)
    assert chosen.reply_text.endswith("личный AI-ассистент.")
    assert conversation_state.get_state("bind-1", "telegram", "999")["handling_mode"] == "ai"

    following = bot_access_gate.authorize("bind-1", "telegram", "999", "У меня вопрос")
    assert following.action == bot_access_gate.PROCEED


def test_choosing_to_wait_then_silences_further_messages(gate_db, monkeypatch):
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")

    chosen = bot_access_gate.authorize("bind-1", "telegram", "999", "2")
    assert chosen.action == bot_access_gate.REPLY
    assert chosen.reply_text == bot_access_gate._HANDLING_WAIT_CONFIRM_TEXT
    assert conversation_state.get_state("bind-1", "telegram", "999")["handling_mode"] == "waiting"

    following = bot_access_gate.authorize("bind-1", "telegram", "999", "Ау, вы тут?")
    assert following.action == bot_access_gate.IGNORE


def test_someone_who_chose_to_wait_can_change_their_mind(gate_db, monkeypatch):
    """The mute used to be permanent: "1" was swallowed by the waiting branch
    like any other message, and touch_state refreshed updated_at on every
    incoming one, so the inactivity reset never came either — the conversation
    could only be revived by the owner from the dashboard."""
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")
    bot_access_gate.authorize("bind-1", "telegram", "999", "2")

    revived = bot_access_gate.authorize("bind-1", "telegram", "999", "1")
    assert revived.action == bot_access_gate.REPLY
    assert revived.reply_text.startswith(bot_access_gate._HANDLING_AI_CONFIRM_TEXT)
    assert conversation_state.get_state("bind-1", "telegram", "999")["handling_mode"] == "ai"
    assert bot_access_gate.authorize("bind-1", "telegram", "999", "Вопрос").action == bot_access_gate.PROCEED


def test_waiting_can_be_escalated_to_urgent_without_the_owner(gate_db, monkeypatch):
    notified: list = []
    _bind_with_handling_menu(monkeypatch, notified, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Упал прод")
    bot_access_gate.authorize("bind-1", "telegram", "999", "2")

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "3")
    assert decision.reply_text == bot_access_gate._HANDLING_URGENT_CONFIRM_TEXT
    assert len(notified) == 1


def test_the_wait_confirmation_says_how_to_come_back(gate_db, monkeypatch):
    """An escape hatch nobody is told about is not an escape hatch."""
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")

    chosen = bot_access_gate.authorize("bind-1", "telegram", "999", "2")
    assert "«1»" in chosen.reply_text and "«3»" in chosen.reply_text


def test_waiting_still_ignores_anything_that_is_not_a_bare_choice(gate_db, monkeypatch):
    """A loose match would mute-and-unmute at random: someone waiting for the
    owner often writes sentences that start with a digit."""
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")
    bot_access_gate.authorize("bind-1", "telegram", "999", "2")

    for text in ("Ау, вы тут?", "1 вопрос всё же есть", "3 часа жду"):
        assert bot_access_gate.authorize("bind-1", "telegram", "999", text).action == bot_access_gate.IGNORE
    assert conversation_state.get_state("bind-1", "telegram", "999")["handling_mode"] == "waiting"


def test_a_conversation_that_chose_the_ai_answers_directly_despite_draft_mode(gate_db, monkeypatch):
    """Draft mode queues the answer for the owner to send by hand — which the
    menu's "отвечу на любой вопрос" cannot survive: the scripted replies go out
    instantly while the actual answer never arrives, so from the chat the bot
    looks dead. Choosing the AI is consent to an automated answer, and it is
    delivered with the disclosure attached."""
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")
    bot_access_gate.authorize("bind-1", "telegram", "999", "1")

    answered = bot_access_gate.authorize("bind-1", "telegram", "999", "Почему падает maven-jar-plugin?")
    assert answered.action == bot_access_gate.PROCEED
    assert bot_access_gate.effective_response_mode(answered, "draft") == "auto_labeled"


def test_the_urgent_option_also_leaves_the_ai_able_to_answer_directly(gate_db, monkeypatch):
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Упал прод")
    bot_access_gate.authorize("bind-1", "telegram", "999", "3")

    answered = bot_access_gate.authorize("bind-1", "telegram", "999", "Да, подскажите пока вы тут")
    assert answered.action == bot_access_gate.PROCEED
    assert bot_access_gate.effective_response_mode(answered, "draft") == "auto_labeled"


def test_draft_mode_is_untouched_for_a_binding_without_the_menu(gate_db, monkeypatch):
    """The owner's own review workflow is the default for a reason — only an
    explicit choice by the person on the other side overrides it."""
    _bind(monkeypatch, allowed_chat_ids=[])
    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Привет")
    assert decision.action == bot_access_gate.PROCEED
    assert bot_access_gate.effective_response_mode(decision, "draft") == "draft"


def test_an_unclear_reply_asks_again_without_losing_the_pending_choice(gate_db, monkeypatch):
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "не понимаю о чём вы")
    assert decision.action == bot_access_gate.REPLY
    assert decision.reply_text == bot_access_gate._HANDLING_UNCLEAR_TEXT
    assert conversation_state.get_state("bind-1", "telegram", "999")["handling_mode"] == "pending_choice"


def test_urgent_choice_pings_the_owner_and_quotes_the_original_question(gate_db, monkeypatch):
    notified: list = []
    _bind_with_handling_menu(monkeypatch, notified, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Упал прод, нужен Альберт", display_name="@ivan")

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "3")
    assert decision.action == bot_access_gate.REPLY
    assert decision.reply_text == bot_access_gate._HANDLING_URGENT_CONFIRM_TEXT
    assert conversation_state.get_state("bind-1", "telegram", "999")["handling_mode"] == "notified"

    assert len(notified) == 1
    assert notified[0]["requester"] == "@ivan"
    # The bare "3" is worthless to the owner — the ping quotes what was asked.
    assert notified[0]["excerpt"] == "Упал прод, нужен Альберт"
    assert "срочн" in notified[0]["reason"]


def test_urgent_reply_admits_when_the_ping_could_not_be_delivered(gate_db, monkeypatch):
    _bind_with_handling_menu(monkeypatch, delivered=False, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "3")
    assert decision.reply_text == bot_access_gate._HANDLING_URGENT_FAILED_TEXT


def test_repeated_urgent_requests_do_not_spam_the_owner(gate_db, monkeypatch):
    notified: list = []
    _bind_with_handling_menu(monkeypatch, notified, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")

    for _ in range(3):
        decision = bot_access_gate.authorize("bind-1", "telegram", "999", "3")
        assert decision.reply_text == bot_access_gate._HANDLING_URGENT_CONFIRM_TEXT
    assert len(notified) == 1  # the cooldown swallowed the repeats


def test_after_the_urgent_ping_a_bare_digit_still_re_picks(gate_db, monkeypatch):
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")
    bot_access_gate.authorize("bind-1", "telegram", "999", "3")

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "2")
    assert decision.reply_text == bot_access_gate._HANDLING_WAIT_CONFIRM_TEXT
    assert conversation_state.get_state("bind-1", "telegram", "999")["handling_mode"] == "waiting"


def test_after_the_urgent_ping_a_real_question_is_answered_by_the_ai(gate_db, monkeypatch):
    """The person was offered help while the owner is on the way — taking it up
    must reach the agent, not another menu."""
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")
    bot_access_gate.authorize("bind-1", "telegram", "999", "3")

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Да, подскажите про 2 сервера")
    assert decision.action == bot_access_gate.PROCEED
    assert conversation_state.get_state("bind-1", "telegram", "999")["handling_mode"] == "ai"


def test_urgent_wording_is_recognised_without_the_digit(gate_db, monkeypatch):
    notified: list = []
    _bind_with_handling_menu(monkeypatch, notified, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")

    # Contains "Альберт" too, which on its own would read as "wait" — urgency wins.
    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Это срочно, нужен Альберт")
    assert decision.reply_text == bot_access_gate._HANDLING_URGENT_CONFIRM_TEXT
    assert len(notified) == 1


def test_handling_menu_only_applies_to_owner_scope_conversations(gate_db, monkeypatch):
    """Token subscribers already get an instant disclosed AI reply — the
    owner-availability menu would be redundant friction for a paid flow."""
    binding = _bind_with_handling_menu(monkeypatch, access_mode="token", allowed_chat_ids=["100"])
    binding.setdefault("response_mode", "auto_labeled")
    token = bot_access.issue_token("bind-1", "agent-consult")
    bot_access.redeem("bind-1", "telegram", "999", token["plaintext"])

    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "вопрос")
    assert decision.action == bot_access_gate.PROCEED
    assert decision.scope == "public"
    assert conversation_state.get_state("bind-1", "telegram", "999") is None


def test_the_question_that_triggered_the_menu_is_kept_for_the_agent(gate_db, monkeypatch):
    """The whole point of picking "1" is to get help with what was already
    asked — so the menu exchange must be in the history the agent then reads."""
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Упал прод, что делать?")

    session_id = conversation_state.session_id_for_conversation("bind-1", "telegram", "999")
    stored = database.get_session_transcript(session_id)
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert stored[0]["content"] == "Упал прод, что делать?"
    assert stored[1]["content"] == bot_access_gate._HANDLING_MENU_TEXT


def test_messages_sent_while_the_bot_is_silent_are_still_recorded(gate_db, monkeypatch):
    """Waiting for the owner must not lose what the person says meanwhile —
    otherwise switching them to the AI later starts from nothing."""
    _bind_with_handling_menu(monkeypatch, allowed_chat_ids=[])
    bot_access_gate.authorize("bind-1", "telegram", "999", "Здравствуйте")
    bot_access_gate.authorize("bind-1", "telegram", "999", "2")

    silenced = bot_access_gate.authorize("bind-1", "telegram", "999", "Ещё деталь: база на реплике")
    assert silenced.action == bot_access_gate.IGNORE

    session_id = conversation_state.session_id_for_conversation("bind-1", "telegram", "999")
    assert "Ещё деталь: база на реплике" in [m["content"] for m in database.get_session_transcript(session_id)]


def test_the_agent_is_left_to_record_its_own_turns(gate_db, monkeypatch):
    """On PROCEED the agent itself writes the turn — recording it here too would
    duplicate every message the bot actually answers."""
    _bind(monkeypatch, allowed_chat_ids=[])
    decision = bot_access_gate.authorize("bind-1", "telegram", "999", "Привет")
    assert decision.action == bot_access_gate.PROCEED
    assert database.get_session_transcript(decision.session_id) == []


def test_owner_takeover_records_what_is_said_while_albert_handles_it(gate_db, monkeypatch):
    _bind(monkeypatch, allowed_chat_ids=[])
    conversation_state.set_handling_mode("bind-1", "telegram", "999", "owner_takeover")

    bot_access_gate.authorize("bind-1", "telegram", "999", "Пишу, пока отвечает Альберт")

    session_id = conversation_state.session_id_for_conversation("bind-1", "telegram", "999")
    assert [m["content"] for m in database.get_session_transcript(session_id)] == [
        "Пишу, пока отвечает Альберт"
    ]


def test_owner_takeover_silences_the_bot_until_released(gate_db, monkeypatch):
    """The admin dashboard's take-over/release button — independent of
    handling_menu_enabled, and works even mid-menu."""
    _bind(monkeypatch, allowed_chat_ids=[])  # handling_menu_enabled omitted -> plain draft binding
    conversation_state.set_handling_mode("bind-1", "telegram", "999", "owner_takeover")

    silenced = bot_access_gate.authorize("bind-1", "telegram", "999", "Есть кто?")
    assert silenced.action == bot_access_gate.IGNORE

    conversation_state.set_handling_mode("bind-1", "telegram", "999", "ai")
    resumed = bot_access_gate.authorize("bind-1", "telegram", "999", "Есть кто?")
    assert resumed.action == bot_access_gate.PROCEED
