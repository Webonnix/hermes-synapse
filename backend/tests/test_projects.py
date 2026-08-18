import pytest

from backend import projects


@pytest.fixture()
def projects_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "projects.db")
    monkeypatch.setattr(projects, "DB_PATH", db_path)
    projects._init_schema()
    return db_path


def test_create_and_get_project(projects_db):
    project = projects.create_project("Сайт клиента X", "Лендинг + форма")
    assert project["name"] == "Сайт клиента X"
    assert project["description"] == "Лендинг + форма"
    assert project["is_active"] is True
    assert projects.get_project(project["id"]) == project


def test_create_project_defaults(projects_db):
    project = projects.create_project("Minimal")
    assert project["description"] == ""
    assert project["is_active"] is True


def test_create_project_rejects_empty_name(projects_db):
    with pytest.raises(ValueError):
        projects.create_project("")
    with pytest.raises(ValueError):
        projects.create_project("   ")


def test_create_project_rejects_overlong_name(projects_db):
    with pytest.raises(ValueError):
        projects.create_project("x" * 81)


def test_list_projects_ordered_by_creation(projects_db):
    first = projects.create_project("First")
    second = projects.create_project("Second")
    listed = projects.list_projects()
    assert [p["id"] for p in listed] == [first["id"], second["id"]]


def test_get_missing_project_returns_none(projects_db):
    assert projects.get_project("proj-ghost") is None


def test_update_project_name_and_description(projects_db):
    project = projects.create_project("Old name")
    updated = projects.update_project(project["id"], name="New name", description="new desc")
    assert updated["name"] == "New name"
    assert updated["description"] == "new desc"


def test_update_project_toggle_active(projects_db):
    project = projects.create_project("X")
    updated = projects.update_project(project["id"], is_active=False)
    assert updated["is_active"] is False


def test_update_project_ignores_unknown_fields(projects_db):
    project = projects.create_project("X")
    updated = projects.update_project(project["id"], id="hacked-id", created_at="2000-01-01")
    assert updated["id"] == project["id"]
    assert updated["created_at"] == project["created_at"]


def test_update_missing_project_raises(projects_db):
    with pytest.raises(KeyError):
        projects.update_project("proj-ghost", name="x")


def test_update_project_rejects_empty_name(projects_db):
    project = projects.create_project("X")
    with pytest.raises(ValueError):
        projects.update_project(project["id"], name="")


def test_delete_project(projects_db):
    project = projects.create_project("X")
    assert projects.delete_project(project["id"]) is True
    assert projects.get_project(project["id"]) is None


def test_delete_missing_project_returns_false(projects_db):
    assert projects.delete_project("proj-ghost") is False
