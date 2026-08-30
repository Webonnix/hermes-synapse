"""SMM Factory publication register (backend/smm_posts.py).

Covers the promises the panel above it makes and nothing that merely restates
SQLite: that the register never claims a post went out when it did not, that
the filters/pagination the table drives are done in the database rather than
the browser, and that the KPI numbers are derived from the same rows.
"""

import asyncio

import pytest
from fastapi import HTTPException

from backend import main, smm_posts


@pytest.fixture()
def posts_db(tmp_path, monkeypatch):
    monkeypatch.setattr(smm_posts, "DB_PATH", str(tmp_path / "smm.db"))
    smm_posts._init_schema()
    return smm_posts


def make(db, title="Пост", platform="telegram", **kwargs):
    return db.create_publication(title, platform, **kwargs)


def test_create_defaults_to_a_draft_with_no_reach(posts_db):
    post = make(posts_db, "Итоги недели")
    assert post["status"] == "draft"
    assert post["delivery_mode"] == "manual"
    assert post["reach"] is None
    assert post["published_at"] is None
    assert post["tags"] == []


def test_tags_are_normalised_and_deduplicated(posts_db):
    post = make(posts_db, tags=["#новости", "новости", " обновление "])
    assert post["tags"] == ["новости", "обновление"]


def test_a_scheduled_post_needs_a_date(posts_db):
    with pytest.raises(ValueError):
        make(posts_db, status="scheduled")


def test_auto_delivery_cannot_be_marked_published_by_hand(posts_db):
    """The whole value of delivery_mode='auto' is that 'Опубликовано' means the
    provider accepted it. An edit must not be able to assert that."""
    with pytest.raises(ValueError):
        make(posts_db, delivery_mode="auto", status="published")

    post = make(posts_db, delivery_mode="auto", binding_id="b1", target_chat_id="@ch")
    with pytest.raises(ValueError):
        posts_db.update_publication(post["id"], status="published")


def test_manual_publish_records_the_owners_claim(posts_db):
    post = make(posts_db, delivery_mode="manual")
    published = asyncio.run(posts_db.publish_publication(post["id"]))
    assert published["status"] == "published"
    assert published["published_at"]


def test_failed_delivery_leaves_the_row_failed_with_the_providers_wording(posts_db, monkeypatch):
    post = make(posts_db, delivery_mode="auto", binding_id="b1", target_chat_id="@ch")

    async def boom(*args, **kwargs):
        raise RuntimeError("Bot token was revoked")

    monkeypatch.setattr("backend.channel_replies.send_to_channel", boom)

    with pytest.raises(RuntimeError):
        asyncio.run(posts_db.publish_publication(post["id"]))

    stored = posts_db.get_publication(post["id"])
    assert stored["status"] == "failed"
    assert "Bot token was revoked" in stored["error"]
    assert stored["published_at"] is None


def test_auto_publish_sends_title_body_and_tags_as_one_message(posts_db, monkeypatch):
    post = make(posts_db, "Заголовок", body="Тело", tags=["новости"],
                delivery_mode="auto", binding_id="b1", target_chat_id="@ch")
    sent = {}

    async def capture(binding_id, platform, chat_id, text):
        sent.update(binding_id=binding_id, platform=platform, chat_id=chat_id, text=text)

    monkeypatch.setattr("backend.channel_replies.send_to_channel", capture)

    result = asyncio.run(posts_db.publish_publication(post["id"]))

    assert result["status"] == "published"
    assert sent["chat_id"] == "@ch"
    assert sent["text"] == "Заголовок\n\nТело\n\n#новости"


def test_auto_publish_refuses_without_a_destination(posts_db):
    post = make(posts_db, delivery_mode="auto")
    with pytest.raises(ValueError):
        asyncio.run(posts_db.publish_publication(post["id"]))


def test_due_sweep_moves_only_auto_posts(posts_db, monkeypatch):
    """A manual post has no way out of the backend, so a timer flipping it to
    'published' would be inventing an event."""
    auto = make(posts_db, "Авто", delivery_mode="auto", binding_id="b1", target_chat_id="@ch",
                status="scheduled", scheduled_at="2020-01-01T00:00:00+00:00")
    manual = make(posts_db, "Вручную", status="scheduled", scheduled_at="2020-01-01T00:00:00+00:00")
    future = make(posts_db, "Позже", delivery_mode="auto", binding_id="b1", target_chat_id="@ch",
                  status="scheduled", scheduled_at="2999-01-01T00:00:00+00:00")

    async def ok(*args, **kwargs):
        return None

    monkeypatch.setattr("backend.channel_replies.send_to_channel", ok)

    sent = asyncio.run(posts_db.publish_due())

    assert [item["id"] for item in sent] == [auto["id"]]
    assert posts_db.get_publication(manual["id"])["status"] == "scheduled"
    assert posts_db.get_publication(future["id"])["status"] == "scheduled"


def test_filters_and_pagination_happen_in_the_database(posts_db):
    for index in range(12):
        make(posts_db, f"Пост {index:02d}", platform="vk" if index % 2 else "telegram")
    make(posts_db, "Отдельный", platform="telegram", tags=["уникально"])

    page = posts_db.list_publications(per_page=5, page=2)
    assert len(page["items"]) == 5
    assert page["total"] == 13
    assert page["pages"] == 3

    assert posts_db.list_publications(platform="vk")["total"] == 6
    assert posts_db.list_publications(query="уникально")["total"] == 1
    assert posts_db.list_publications(query="ПОСТ 03")["total"] == 1


def test_a_page_past_the_end_is_clamped_not_emptied(posts_db):
    make(posts_db, "Единственный")
    page = posts_db.list_publications(page=9, per_page=10)
    assert page["page"] == 1
    assert len(page["items"]) == 1


def test_unknown_sort_key_falls_back_instead_of_reaching_sql(posts_db):
    make(posts_db, "A")
    make(posts_db, "B")
    # Not an injection test so much as the reason the whitelist exists: the key
    # comes off a query string and lands in an ORDER BY.
    page = posts_db.list_publications(sort="title); DROP TABLE smm_publications;--")
    assert page["total"] == 2


def test_period_filter_covers_the_whole_last_day(posts_db):
    post = make(posts_db, "Вечерний", status="published", published_at="2026-07-31T23:30:00+00:00")
    inside = posts_db.list_publications(date_from="2026-07-01", date_to="2026-07-31")
    assert [item["id"] for item in inside["items"]] == [post["id"]]
    assert posts_db.list_publications(date_from="2026-08-01", date_to="2026-08-31")["total"] == 0


def test_an_unscheduled_draft_sorts_by_when_it_was_saved(posts_db):
    """A draft keeps its old scheduled_at, so ordering by COALESCE would put it
    back where its abandoned slot was."""
    post = make(posts_db, "Черновик", status="scheduled", scheduled_at="2020-01-01T00:00:00+00:00")
    posts_db.update_publication(post["id"], status="draft", scheduled_at="")
    assert posts_db.get_publication(post["id"])["scheduled_at"] is None
    assert posts_db.list_publications(date_from="2020-01-01", date_to="2020-01-01")["total"] == 0


def test_stats_break_down_the_same_rows_the_table_shows(posts_db):
    make(posts_db, "1", status="published", published_at="2026-07-01T00:00:00+00:00", reach=12400, reach_delta=8.2)
    make(posts_db, "2", status="published", published_at="2026-07-02T00:00:00+00:00", reach=8700)
    make(posts_db, "3", status="scheduled", scheduled_at="2999-01-01T00:00:00+00:00")
    make(posts_db, "4")

    stats = posts_db.publication_stats()
    assert stats["total"] == 4
    assert stats["published"] == 2
    assert stats["scheduled"] == 1
    assert stats["drafts"] == 1
    assert stats["published_share"] == 50.0
    assert stats["best_reach"] == 12400
    assert stats["best_reach_delta"] == 8.2
    # Oldest first, so the sparkline reads left to right like the dates do.
    assert stats["reach_series"] == [12400, 8700]


def test_stats_follow_the_platform_filter(posts_db):
    make(posts_db, "tg", platform="telegram", status="published", reach=100)
    make(posts_db, "vk", platform="vk", status="published", reach=900)

    stats = posts_db.publication_stats(platform="vk")
    assert stats["total"] == 1
    assert stats["best_reach"] == 900


def test_bulk_delete_reports_what_actually_went(posts_db):
    first = make(posts_db, "1")
    second = make(posts_db, "2")
    assert posts_db.delete_publications([first["id"], second["id"], "post-gone"]) == 2
    assert posts_db.list_publications()["total"] == 0


def test_leaving_the_failed_state_clears_the_stale_error(posts_db):
    post = make(posts_db, "Сломанный", delivery_mode="auto", binding_id="b1", target_chat_id="@ch")
    smm_posts._mark_failed(post["id"], "Bot token was revoked")
    fixed = posts_db.update_publication(post["id"], status="draft")
    assert fixed["error"] == ""


# ── HTTP surface (backend/main.py) ─────────────────────────────────────────
# Handlers are called directly, same style as test_agent_admin_endpoints.py.
# What is worth pinning here is the error contract the panel renders — a 409
# carrying the provider's wording is what keeps "Ошибка" from becoming a bare
# "что-то пошло не так" — and the route order, since /stats would otherwise be
# swallowed by /{post_id}.

def test_stats_route_is_matched_before_the_post_id_route():
    paths = [route.path for route in main.app.routes if getattr(route, "path", "").startswith("/api/smm/publications")]
    assert paths.index("/api/smm/publications/stats") < paths.index("/api/smm/publications/{post_id}")


@pytest.mark.asyncio
async def test_invalid_filter_is_a_400_not_a_500(posts_db):
    with pytest.raises(HTTPException) as exc:
        await main.list_smm_publications_api(status="не-статус")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_missing_publication_is_a_404(posts_db):
    with pytest.raises(HTTPException) as exc:
        await main.get_smm_publication_api("post-missing")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_provider_refusal_surfaces_as_409_with_its_own_wording(posts_db, monkeypatch):
    post = make(posts_db, "Пост", delivery_mode="auto", binding_id="b1", target_chat_id="@ch")

    async def boom(*args, **kwargs):
        raise RuntimeError("Bot token was revoked")

    monkeypatch.setattr("backend.channel_replies.send_to_channel", boom)

    with pytest.raises(HTTPException) as exc:
        await main.publish_smm_publication_api(post["id"])
    assert exc.value.status_code == 409
    assert "Bot token was revoked" in exc.value.detail
    assert posts_db.get_publication(post["id"])["status"] == "failed"


@pytest.mark.asyncio
async def test_update_only_writes_the_fields_that_were_sent(posts_db):
    post = make(posts_db, "Пост", body="Тело", account="@acc")
    await main.update_smm_publication_api(post["id"], main.SmmPublicationUpdateRequest(title="Новое имя"))
    stored = posts_db.get_publication(post["id"])
    assert stored["title"] == "Новое имя"
    assert stored["body"] == "Тело"
    assert stored["account"] == "@acc"


@pytest.mark.asyncio
async def test_creating_a_publication_round_trips_through_the_endpoint(posts_db):
    created = await main.create_smm_publication_api(main.SmmPublicationRequest(
        title="Итоги недели", platform="telegram", body="Главные новости",
        tags=["#новости"], scheduled_at="",
    ))
    assert created["status"] == "draft"
    assert created["tags"] == ["новости"]
    assert created["scheduled_at"] is None
    assert (await main.smm_publication_stats_api())["total"] == 1


def test_an_already_published_auto_post_stays_editable(posts_db, monkeypatch):
    """Blocking the manual 'published' flag protects the *transition*. Once the
    send really happened, the text and the measured reach still have to be
    correctable."""
    post = make(posts_db, "Пост", delivery_mode="auto", binding_id="b1", target_chat_id="@ch")

    async def ok(*args, **kwargs):
        return None

    monkeypatch.setattr("backend.channel_replies.send_to_channel", ok)
    asyncio.run(posts_db.publish_publication(post["id"]))

    fixed = posts_db.update_publication(post["id"], status="published", title="Пост (уточнён)", reach=4200)
    assert fixed["title"] == "Пост (уточнён)"
    assert fixed["reach"] == 4200
    assert fixed["status"] == "published"
