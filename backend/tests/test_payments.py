import hashlib
import hmac
import json

import pytest
import respx
from httpx import Response

from backend import bot_access, database, payments


@pytest.fixture()
def billing_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "billing.db")
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(bot_access, "DB_PATH", db_path)
    monkeypatch.setattr(payments, "DB_PATH", db_path)
    monkeypatch.setattr(bot_access, "_local_rate_buckets", {})
    monkeypatch.setattr(bot_access, "_valkey_unavailable_until", float("inf"))
    database.init_db()
    bot_access._init_schema()
    payments._init_schema()
    return db_path


@pytest.fixture()
def paid_plan(billing_db):
    return bot_access.create_plan(
        name="Подписка Про", period="monthly", limit_usd=5.0, limit_messages=100,
        rate_limit_per_min=0, price_usd=19.0, is_purchasable=True,
    )


async def _invoice(plan, **kwargs):
    return await payments.create_invoice(
        plan_id=plan["id"], binding_id="bind-1", subagent_id="agent-consult", **kwargs
    )


async def _nowpayments_invoice(plan, **kwargs):
    """A pending invoice created through the real provider path, with the
    provider's HTTP call stubbed — the shape the webhook tests need."""
    database.set_api_key(payments.KEY_NOWPAYMENTS_API, "test-api-key")
    database.set_api_key(payments.KEY_NOWPAYMENTS_IPN, "ipn-secret")
    payments.set_config(provider="nowpayments", public_base_url="https://hermes.example.net")
    with respx.mock:
        respx.post("https://api.nowpayments.io/v1/invoice").mock(
            return_value=Response(200, json={"id": 4477, "invoice_url": "https://nowpayments.io/payment/?iid=4477"})
        )
        return await _invoice(plan, **kwargs)


# ── plans as products ───────────────────────────────────────────────────────

def test_a_purchasable_plan_needs_a_price(billing_db):
    with pytest.raises(ValueError, match="needs a price"):
        bot_access.create_plan(name="Бесплатный?", is_purchasable=True)


def test_period_implies_a_default_duration(billing_db):
    monthly = bot_access.create_plan(name="Месяц", period="monthly")
    lifetime = bot_access.create_plan(name="Навсегда", period="lifetime")
    assert monthly["duration_days"] == 30
    assert lifetime["duration_days"] is None


# ── subscriptions ───────────────────────────────────────────────────────────

def test_a_lapsed_subscription_stops_the_token(billing_db, paid_plan):
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=paid_plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    subscription = bot_access.create_subscription(paid_plan, token["id"], "bind-1", "agent-consult")

    assert bot_access.check_access(token["id"], subscriber["id"], 10)["allowed"] is True

    with bot_access._connect() as connection:
        connection.execute(
            "UPDATE access_subscriptions SET current_period_end = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (subscription["id"],),
        )
    check = bot_access.check_access(token["id"], subscriber["id"], 10)
    assert check["allowed"] is False
    assert "Срок подписки истёк" in check["reason"]


def test_a_lifetime_purchase_never_lapses(billing_db):
    plan = bot_access.create_plan(name="Навсегда", period="lifetime", price_usd=99.0, is_purchasable=True)
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    subscription = bot_access.create_subscription(plan, token["id"], "bind-1", "agent-consult")

    assert subscription["current_period_end"] is None
    assert bot_access.subscription_lapsed(subscription) is False
    assert bot_access.check_access(token["id"], subscriber["id"], 10)["allowed"] is True


def test_canceling_parks_the_token_and_blocks_access(billing_db, paid_plan):
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=paid_plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    subscription = bot_access.create_subscription(paid_plan, token["id"], "bind-1", "agent-consult")

    bot_access.cancel_subscription(subscription["id"])
    assert bot_access.get_token(token["id"])["status"] == "suspended"
    check = bot_access.check_access(token["id"], subscriber["id"], 10)
    assert check["allowed"] is False


def test_renewing_restores_quota_and_unsuspends(billing_db, paid_plan):
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=paid_plan["id"])
    subscriber = bot_access.redeem("bind-1", "telegram", "555", token["plaintext"])
    subscription = bot_access.create_subscription(paid_plan, token["id"], "bind-1", "agent-consult")
    bot_access.record_usage(subscriber, token["id"], "agent-consult", 500, 200, 6.0)
    bot_access.cancel_subscription(subscription["id"])

    renewed = bot_access.renew_subscription(subscription["id"])
    assert renewed["status"] == "active"
    assert bot_access.get_token(token["id"])["status"] == "active"
    assert bot_access.get_token(token["id"])["used_usd"] == 0
    assert bot_access.check_access(token["id"], subscriber["id"], 10)["allowed"] is True


def test_renewing_early_extends_rather_than_truncates(billing_db, paid_plan):
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=paid_plan["id"])
    subscription = bot_access.create_subscription(paid_plan, token["id"], "bind-1", "agent-consult")
    first_end = bot_access._parse_iso(subscription["current_period_end"])

    renewed = bot_access.renew_subscription(subscription["id"])
    second_end = bot_access._parse_iso(renewed["current_period_end"])
    # Paying a month early must add a month, not restart the clock from today.
    assert (second_end - first_end).days >= 29


def test_the_expiry_sweep_marks_and_parks(billing_db, paid_plan):
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=paid_plan["id"])
    subscription = bot_access.create_subscription(paid_plan, token["id"], "bind-1", "agent-consult")
    with bot_access._connect() as connection:
        connection.execute(
            "UPDATE access_subscriptions SET current_period_end = '2020-01-01T00:00:00+00:00' WHERE id = ?",
            (subscription["id"],),
        )

    assert bot_access.expire_lapsed_subscriptions() == 1
    assert bot_access.get_subscription(subscription["id"])["status"] == "expired"
    assert bot_access.get_token(token["id"])["status"] == "suspended"
    assert bot_access.expire_lapsed_subscriptions() == 0  # idempotent


# ── invoices ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_free_plan_cannot_be_invoiced(billing_db):
    free = bot_access.create_plan(name="Бесплатный")
    with pytest.raises(ValueError, match="no price"):
        await _invoice(free)


@pytest.mark.asyncio
async def test_the_main_agent_is_never_for_sale(billing_db, paid_plan):
    with pytest.raises(ValueError, match="main agent"):
        await payments.create_invoice(
            plan_id=paid_plan["id"], binding_id="bind-1", subagent_id="jarvis"
        )


@pytest.mark.asyncio
async def test_manual_mode_records_an_invoice_without_calling_out(billing_db, paid_plan):
    invoice = await _invoice(paid_plan, customer_ref="Иван")
    assert invoice["provider"] == "manual"
    assert invoice["status"] == "pending"
    assert invoice["amount_usd"] == 19.0
    assert invoice["payment_url"] == ""


@pytest.mark.asyncio
@respx.mock
async def test_nowpayments_invoice_uses_the_documented_endpoint(billing_db, paid_plan):
    database.set_api_key(payments.KEY_NOWPAYMENTS_API, "test-api-key")
    payments.set_config(provider="nowpayments", public_base_url="https://hermes.example.net")
    route = respx.post("https://api.nowpayments.io/v1/invoice").mock(
        return_value=Response(200, json={"id": 4477, "invoice_url": "https://nowpayments.io/payment/?iid=4477"})
    )

    invoice = await _invoice(paid_plan, customer_ref="Иван")

    assert route.called
    sent = json.loads(route.calls[0].request.content)
    assert route.calls[0].request.headers["x-api-key"] == "test-api-key"
    assert sent["price_amount"] == 19.0
    assert sent["price_currency"] == "usd"
    assert sent["order_id"] == invoice["id"]
    assert sent["ipn_callback_url"] == "https://hermes.example.net/api/payments/webhook"
    assert invoice["provider_invoice_id"] == "4477"
    assert invoice["payment_url"] == "https://nowpayments.io/payment/?iid=4477"


@pytest.mark.asyncio
async def test_nowpayments_without_a_callback_url_refuses_to_sell(billing_db, paid_plan):
    database.set_api_key(payments.KEY_NOWPAYMENTS_API, "test-api-key")
    payments.set_config(provider="nowpayments", public_base_url="")
    with pytest.raises(ValueError, match="public base URL"):
        await _invoice(paid_plan)


# ── webhook signature ───────────────────────────────────────────────────────

def _sign(payload: dict, secret: str) -> tuple[bytes, str]:
    raw = json.dumps(payload).encode("utf-8")
    canonical = payments._canonical_json(
        json.loads(raw, parse_float=payments._RawNumber, parse_int=payments._RawNumber)
    )
    signature = hmac.new(secret.encode(), canonical.encode(), hashlib.sha512).hexdigest()
    return raw, signature


def test_signature_verification_matches_the_providers_scheme(billing_db):
    payload = {"payment_status": "finished", "order_id": "inv_1", "actually_paid": 19.0, "price_amount": 19}
    raw, signature = _sign(payload, "ipn-secret")
    assert payments.verify_nowpayments_signature(raw, signature, "ipn-secret") is True
    assert payments.verify_nowpayments_signature(raw, signature, "wrong-secret") is False
    assert payments.verify_nowpayments_signature(raw, "deadbeef", "ipn-secret") is False
    assert payments.verify_nowpayments_signature(b"{not json", signature, "ipn-secret") is False


def test_number_literals_survive_the_canonical_round_trip(billing_db):
    """JavaScript writes 1.0 as `1`; re-serialising through Python floats would
    write `1.0` and break every signature carrying a whole-number amount."""
    raw = b'{"b":1.0,"a":2,"c":1e3}'
    parsed = json.loads(raw, parse_float=payments._RawNumber, parse_int=payments._RawNumber)
    assert payments._canonical_json(parsed) == '{"a":2,"b":1.0,"c":1e3}'


def test_key_order_in_the_body_does_not_change_the_signature(billing_db):
    first, signature = _sign({"a": 1, "b": "x"}, "s3cret")
    reordered = b'{"b":"x","a":1}'
    assert payments.verify_nowpayments_signature(reordered, signature, "s3cret") is True
    assert first != reordered


# ── webhook behaviour ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_paid_webhook_issues_a_token_and_a_subscription(billing_db, paid_plan):
    invoice = await _nowpayments_invoice(paid_plan, customer_ref="Иван")

    raw, signature = _sign({"payment_status": "finished", "order_id": invoice["id"]}, "ipn-secret")
    outcome = payments.handle_webhook(raw, {"x-nowpayments-sig": signature})

    assert outcome["ok"] is True
    result = outcome["result"]
    assert result["plaintext"].startswith("HRM-")
    assert result["token"]["plan_id"] == paid_plan["id"]
    assert result["subscription"]["status"] == "active"
    assert payments.get_invoice(invoice["id"])["status"] == "paid"


@pytest.mark.asyncio
async def test_a_replayed_webhook_never_mints_a_second_token(billing_db, paid_plan):
    invoice = await _nowpayments_invoice(paid_plan)
    raw, signature = _sign({"payment_status": "finished", "order_id": invoice["id"]}, "ipn-secret")

    first = payments.handle_webhook(raw, {"x-nowpayments-sig": signature})
    second = payments.handle_webhook(raw, {"x-nowpayments-sig": signature})

    assert first["result"]["already_applied"] is False
    assert second["result"]["already_applied"] is True
    assert second["result"]["plaintext"] is None  # the buyer already has it
    assert len(bot_access.list_tokens()) == 1
    assert len(bot_access.list_subscriptions()) == 1


@pytest.mark.asyncio
async def test_an_unsigned_webhook_grants_nothing(billing_db, paid_plan):
    invoice = await _nowpayments_invoice(paid_plan)

    forged = json.dumps({"payment_status": "finished", "order_id": invoice["id"]}).encode()
    outcome = payments.handle_webhook(forged, {"x-nowpayments-sig": "0" * 128})

    assert outcome["ok"] is False
    assert outcome["detail"] == "Invalid signature"
    assert payments.get_invoice(invoice["id"])["status"] == "pending"
    assert bot_access.list_tokens() == []


@pytest.mark.asyncio
async def test_in_flight_and_underpaid_statuses_grant_nothing(billing_db, paid_plan):
    invoice = await _nowpayments_invoice(paid_plan)

    for status in ("waiting", "confirming", "sending", "partially_paid"):
        raw, signature = _sign({"payment_status": status, "order_id": invoice["id"]}, "ipn-secret")
        outcome = payments.handle_webhook(raw, {"x-nowpayments-sig": signature})
        assert outcome["ok"] is True
        assert outcome["result"] is None
        assert payments.get_invoice(invoice["id"])["status"] == "pending"
    assert bot_access.list_tokens() == []
    assert payments.get_invoice(invoice["id"])["last_status"] == "partially_paid"


@pytest.mark.asyncio
async def test_a_failed_payment_marks_the_invoice_failed(billing_db, paid_plan):
    invoice = await _nowpayments_invoice(paid_plan)

    raw, signature = _sign({"payment_status": "expired", "order_id": invoice["id"]}, "ipn-secret")
    payments.handle_webhook(raw, {"x-nowpayments-sig": signature})
    assert payments.get_invoice(invoice["id"])["status"] == "failed"
    assert bot_access.list_tokens() == []


@pytest.mark.asyncio
async def test_a_renewal_invoice_extends_instead_of_issuing_a_new_token(billing_db, paid_plan):
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=paid_plan["id"])
    subscription = bot_access.create_subscription(paid_plan, token["id"], "bind-1", "agent-consult")
    bot_access.cancel_subscription(subscription["id"])

    invoice = await _invoice(paid_plan, purpose="renewal", subscription_id=subscription["id"])
    result = payments.mark_paid_manually(invoice["id"])

    assert result["token"]["id"] == token["id"]  # same credential the customer already saved
    assert result["plaintext"] is None
    assert result["subscription"]["status"] == "active"
    assert len(bot_access.list_tokens()) == 1


@pytest.mark.asyncio
async def test_a_sale_made_inside_a_chat_binds_the_token_to_that_chat(billing_db, paid_plan):
    invoice = await _invoice(
        paid_plan, origin="bot", origin_platform="telegram", origin_chat_id="999", customer_ref="@ivan"
    )
    result = payments.mark_paid_manually(invoice["id"])

    assert result["deliver_to"] == {"binding_id": "bind-1", "platform": "telegram", "chat_id": "999"}
    subscriber = bot_access.find_subscriber("bind-1", "telegram", "999")
    assert subscriber is not None
    assert subscriber["token_id"] == result["token"]["id"]
    # The buyer can talk immediately, without pasting anything.
    assert bot_access.check_access(result["token"]["id"], subscriber["id"], 10)["allowed"] is True


@pytest.mark.asyncio
async def test_a_second_pending_invoice_is_not_created_for_the_same_chat(billing_db, paid_plan):
    await _invoice(paid_plan, origin="bot", origin_platform="telegram", origin_chat_id="999")
    found = payments.find_pending_invoice_for_chat("bind-1", "telegram", "999")
    assert found is not None
    assert found["status"] == "pending"


def test_revenue_overview_reports_paid_totals_and_mrr(billing_db, paid_plan):
    token = bot_access.issue_token("bind-1", "agent-consult", plan_id=paid_plan["id"])
    bot_access.create_subscription(paid_plan, token["id"], "bind-1", "agent-consult")
    report = payments.revenue_overview()
    assert report["subscriptions"]["active"] == 1
    assert report["mrr_usd"] == 19.0  # a $19 30-day plan is $19/month
    assert report["config"]["provider"] in payments.PROVIDERS


def test_config_never_returns_the_secrets_themselves(billing_db):
    database.set_api_key(payments.KEY_NOWPAYMENTS_API, "super-secret-key")
    database.set_api_key(payments.KEY_NOWPAYMENTS_IPN, "super-secret-ipn")
    config = payments.get_config()
    assert config["api_key_configured"] is True
    assert config["ipn_secret_configured"] is True
    assert "super-secret-key" not in json.dumps(config)
    assert "super-secret-ipn" not in json.dumps(config)


@pytest.mark.asyncio
async def test_an_invoice_refuses_a_plan_assigned_to_a_different_agent(billing_db):
    database.save_subagent(id="researcher", name="Researcher", system_prompt="", model="llama3")
    scoped = bot_access.create_plan(
        name="Для researcher", price_usd=19.0, is_purchasable=True, subagent_id="researcher"
    )
    with pytest.raises(ValueError, match="different agent"):
        await payments.create_invoice(
            plan_id=scoped["id"], binding_id="bind-1", subagent_id="some-other-agent"
        )
    # The assigned agent can still be sold to.
    invoice = await payments.create_invoice(
        plan_id=scoped["id"], binding_id="bind-1", subagent_id="researcher"
    )
    assert invoice["plan_id"] == scoped["id"]
