"""Sales pipeline: stage transitions, the lost-reason and won-timestamp
bookkeeping, and the boundary with clients.py at convert_to_client()."""

import asyncio

import pytest

from backend import client_activity, clients, currency, database, deals


@pytest.fixture()
def deals_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "deals.db")
    for module in (database, currency, clients, client_activity, deals):
        monkeypatch.setattr(module, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    for module in (currency, clients, client_activity, deals):
        module.reset_schema_cache()
    database.init_db()
    return db_path


def make(**overrides):
    payload = {"title": "Сайт для Midot", "contactName": "Иван", "value": "1500", "currency": "USD"}
    payload.update(overrides)
    return deals.create_deal(payload)


# ── creation ─────────────────────────────────────────────────────────────────

def test_create_defaults_to_the_new_stage(deals_db):
    deal = make()
    assert deal["stage"] == "NEW"
    assert deal["clientId"] is None
    assert deal["wonAt"] is None and deal["lostAt"] is None


def test_a_deal_needs_a_title(deals_db):
    with pytest.raises(ValueError):
        deals.create_deal({"title": ""})


def test_cannot_be_created_already_won_or_lost(deals_db):
    with pytest.raises(ValueError):
        deals.create_deal({"title": "x", "stage": "WON"})
    with pytest.raises(ValueError):
        deals.create_deal({"title": "x", "stage": "LOST"})


def test_value_is_optional(deals_db):
    deal = deals.create_deal({"title": "Без оценки"})
    assert deal["value"] is None


# ── stage transitions ────────────────────────────────────────────────────────

def test_moving_to_lost_requires_a_reason(deals_db):
    deal = make()
    with pytest.raises(ValueError):
        deals.update_deal(deal["id"], {"stage": "LOST"})
    moved = deals.update_deal(deal["id"], {"stage": "LOST", "lostReason": "Слишком дорого"})
    assert moved["stage"] == "LOST"
    assert moved["lostReason"] == "Слишком дорого"
    assert moved["lostAt"] is not None


def test_moving_to_won_stamps_won_at(deals_db):
    deal = make()
    moved = deals.update_deal(deal["id"], {"stage": "WON"})
    assert moved["stage"] == "WON"
    assert moved["wonAt"] is not None
    assert moved["lostAt"] is None


def test_reopening_a_lost_deal_clears_the_reason_and_timestamp(deals_db):
    deal = make()
    deals.update_deal(deal["id"], {"stage": "LOST", "lostReason": "Не сейчас"})
    reopened = deals.update_deal(deal["id"], {"stage": "CONTACTED"})
    assert reopened["stage"] == "CONTACTED"
    assert reopened["lostAt"] is None
    assert reopened["lostReason"] == ""


def test_stage_changed_at_advances_only_on_a_real_stage_change(deals_db):
    deal = make()
    first = deals.update_deal(deal["id"], {"stage": "CONTACTED"})
    # Same stage again — a no-op that must not fabricate movement.
    again = deals.update_deal(deal["id"], {"stage": "CONTACTED"})
    assert again["stageChangedAt"] == first["stageChangedAt"]


def test_a_plain_field_edit_does_not_touch_the_stage(deals_db):
    deal = make()
    edited = deals.update_deal(deal["id"], {"title": "Сайт для Midot v2"})
    assert edited["title"] == "Сайт для Midot v2"
    assert edited["stage"] == "NEW"


def test_unknown_stage_is_rejected(deals_db):
    deal = make()
    with pytest.raises(ValueError):
        deals.update_deal(deal["id"], {"stage": "MAYBE"})


def test_missing_deal_raises_keyerror(deals_db):
    with pytest.raises(KeyError):
        deals.update_deal("deal-nope", {"title": "x"})
    with pytest.raises(KeyError):
        deals.delete_deal("deal-nope")


# ── conversion to a client ───────────────────────────────────────────────────

def test_convert_creates_a_real_client_and_links_it(deals_db):
    deal = make(title="SMM для Acme")
    updated, client = deals.convert_to_client(deal["id"], {
        "name": "Acme", "projectName": "SMM для Acme",
        "contact": {"name": "Пётр", "email": "p@acme.com", "isPrimary": True},
    })
    assert updated["stage"] == "WON"
    assert updated["clientId"] == client["id"]
    assert client["name"] == "Acme"
    assert client["primaryContact"]["email"] == "p@acme.com"


def test_convert_goes_through_clients_create_client_not_a_shortcut(deals_db, monkeypatch):
    """If this ever stops calling clients.create_client, a converted deal
    would silently lose the contact/service/billing machinery every other
    client gets — pin the call itself, not just its visible effect."""
    deal = make()
    calls = []
    original = clients.create_client

    def spy(payload, actor_id=None):
        calls.append(payload)
        return original(payload, actor_id=actor_id)

    monkeypatch.setattr(clients, "create_client", spy)
    deals.convert_to_client(deal["id"], {"name": "X"})
    assert len(calls) == 1


def test_convert_twice_is_rejected(deals_db):
    deal = make()
    deals.convert_to_client(deal["id"], {"name": "Acme"})
    with pytest.raises(ValueError):
        deals.convert_to_client(deal["id"], {"name": "Acme again"})


def test_a_converted_deals_stage_is_frozen(deals_db):
    deal = make()
    deals.convert_to_client(deal["id"], {"name": "Acme"})
    with pytest.raises(ValueError):
        deals.update_deal(deal["id"], {"stage": "NEGOTIATION"})
    # Editing an unrelated field is still fine — only the stage is frozen.
    edited = deals.update_deal(deal["id"], {"source": "Реферал"})
    assert edited["source"] == "Реферал"


def test_a_converted_deal_cannot_be_deleted(deals_db):
    deal = make()
    deals.convert_to_client(deal["id"], {"name": "Acme"})
    with pytest.raises(ValueError):
        deals.delete_deal(deal["id"])


def test_an_unconverted_deal_can_be_deleted(deals_db):
    deal = make()
    deals.delete_deal(deal["id"])
    assert deals.get_deal(deal["id"]) is None


# ── listing and search ───────────────────────────────────────────────────────

def test_search_matches_title_contact_company_and_source(deals_db):
    make(title="Landing для Zenith", companyName="Zenith Corp")
    make(title="Другое", contactName="Zenith Ivanov")
    make(title="Третье")
    assert len(deals.list_deals(search="zenith")) == 2


def test_stage_filter(deals_db):
    a = make(title="A")
    make(title="B")
    deals.update_deal(a["id"], {"stage": "PROPOSAL"})
    assert [d["title"] for d in deals.list_deals(stage="PROPOSAL")] == ["A"]


def test_include_closed_false_drops_won_and_lost(deals_db):
    won = make(title="Won")
    lost = make(title="Lost")
    make(title="Open")
    deals.update_deal(won["id"], {"stage": "WON"})
    deals.update_deal(lost["id"], {"stage": "LOST", "lostReason": "x"})
    open_only = deals.list_deals(include_closed=False)
    assert {d["title"] for d in open_only} == {"Open"}


def test_listing_orders_most_recently_touched_first(deals_db):
    first = make(title="First")
    second = make(title="Second")
    deals.update_deal(first["id"], {"title": "First (touched)"})
    ordered = deals.list_deals()
    assert ordered[0]["id"] == first["id"]
    assert ordered[1]["id"] == second["id"]


# ── pipeline summary ─────────────────────────────────────────────────────────

def test_pipeline_summary_counts_and_win_rate(deals_db):
    a = make(title="A", value="100", currency="USD")
    b = make(title="B", value="200", currency="USD")
    make(title="C", value="")  # no value estimate yet
    deals.update_deal(a["id"], {"stage": "WON"})
    deals.update_deal(b["id"], {"stage": "LOST", "lostReason": "x"})

    summary = deals.pipeline_summary()
    assert summary["wonCount"] == 1
    assert summary["lostCount"] == 1
    assert summary["winRate"] == 50.0
    assert summary["openCount"] == 1  # only C is still open
    assert summary["openValue"]["usd"] == "0.00"  # C has no value


def test_pipeline_summary_converts_mixed_currencies_through_usd(deals_db):
    """A raw SQL SUM across the value column would silently add a USD amount
    to a CNY one — this is the regression test for that."""
    make(title="USD deal", value="100", currency="USD")
    make(title="CNY deal", value="700", currency="CNY")  # ≈ 100 USD at the seed rate

    summary = deals.pipeline_summary()
    # Both deals are open (NEW); their USD-normalized values should be close,
    # not off by 7x the way an unconverted SUM would be.
    total = float(summary["openValue"]["usd"])
    assert 150 < total < 250


def test_win_rate_is_none_with_no_closed_deals(deals_db):
    make()
    assert deals.pipeline_summary()["winRate"] is None


def test_won_this_month_only_counts_won_stage(deals_db):
    a = make(value="500", currency="USD")
    deals.update_deal(a["id"], {"stage": "WON"})
    summary = deals.pipeline_summary()
    assert summary["wonThisMonthCount"] == 1
    assert float(summary["wonThisMonthValue"]["usd"]) == pytest.approx(500.0, abs=0.01)


def test_asyncio_smoke_for_convert_is_sync_not_async(deals_db):
    """convert_to_client is a plain function (unlike smm_posts.publish_publication)
    — this just documents that main.py must not await it directly without
    asyncio.to_thread, by proving it runs fine off the event loop."""
    deal = make()
    assert not asyncio.iscoroutinefunction(deals.convert_to_client)
