"""Client notes: pinning, ordering, and that edits/deletes are actually
allowed here — unlike the append-only client_activity_log they live beside."""

import pytest

from backend import client_activity, client_notes, database


@pytest.fixture()
def notes_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "notes.db")
    for module in (database, client_activity, client_notes):
        monkeypatch.setattr(module, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(database, "DB_DIR", str(tmp_path))
    for module in (client_activity, client_notes):
        module.reset_schema_cache()
    database.init_db()
    return db_path


def test_a_note_needs_a_client_id_and_a_body(notes_db):
    with pytest.raises(ValueError):
        client_notes.create_note("", "текст")
    with pytest.raises(ValueError):
        client_notes.create_note("cli-1", "   ")


def test_create_and_list(notes_db):
    client_notes.create_note("cli-1", "Первая заметка")
    client_notes.create_note("cli-1", "Вторая заметка", author="Альберт")
    notes = client_notes.list_notes("cli-1")
    assert len(notes) == 2
    assert notes[0]["body"] == "Вторая заметка"  # most recent first
    assert notes[0]["author"] == "Альберт"


def test_pinned_notes_sort_first_regardless_of_age(notes_db):
    client_notes.create_note("cli-1", "Старая")
    pinned = client_notes.create_note("cli-1", "Важная", pinned=True)
    client_notes.create_note("cli-1", "Новая")
    ordered = client_notes.list_notes("cli-1")
    assert ordered[0]["id"] == pinned["id"]


def test_notes_are_scoped_per_client(notes_db):
    client_notes.create_note("cli-1", "Про клиента 1")
    client_notes.create_note("cli-2", "Про клиента 2")
    assert len(client_notes.list_notes("cli-1")) == 1
    assert len(client_notes.list_notes("cli-2")) == 1


def test_update_body(notes_db):
    note = client_notes.create_note("cli-1", "Исходный текст")
    updated = client_notes.update_note(note["id"], body="Исправленный текст")
    assert updated["body"] == "Исправленный текст"
    assert updated["updatedAt"] >= note["createdAt"]


def test_update_cannot_blank_the_body(notes_db):
    note = client_notes.create_note("cli-1", "текст")
    with pytest.raises(ValueError):
        client_notes.update_note(note["id"], body="   ")


def test_toggle_pinned_without_touching_body(notes_db):
    note = client_notes.create_note("cli-1", "текст")
    pinned = client_notes.update_note(note["id"], pinned=True)
    assert pinned["pinned"] is True
    assert pinned["body"] == "текст"


def test_delete_removes_it(notes_db):
    note = client_notes.create_note("cli-1", "текст")
    client_notes.delete_note(note["id"])
    assert client_notes.get_note(note["id"]) is None
    assert client_notes.list_notes("cli-1") == []


def test_missing_note_raises_keyerror(notes_db):
    with pytest.raises(KeyError):
        client_notes.update_note("note-nope", body="x")
    with pytest.raises(KeyError):
        client_notes.delete_note("note-nope")


def test_creating_a_note_logs_an_activity_event(notes_db):
    note = client_notes.create_note("cli-1", "текст заметки для аудита")
    events = client_activity.list_events(client_id="cli-1", entity_id=note["id"])
    assert any(event["event_type"] == client_activity.NOTE_CREATED for event in events)


def test_editing_and_deleting_a_note_does_not_touch_the_audit_log(notes_db):
    """The whole point of a separate notes table: unlike client_activity_log,
    a note may be corrected or removed after the fact."""
    note = client_notes.create_note("cli-1", "текст")
    client_notes.update_note(note["id"], body="исправлено")
    client_notes.delete_note(note["id"])
    events = client_activity.list_events(client_id="cli-1", entity_id=note["id"])
    assert len(events) == 1  # only the original NOTE_CREATED
    assert events[0]["event_type"] == client_activity.NOTE_CREATED
