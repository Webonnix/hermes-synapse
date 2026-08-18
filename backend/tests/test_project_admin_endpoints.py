"""Tests for /api/projects and the session-project endpoint in backend/main.py
— called directly as plain async functions, same style as
test_router_tiers_endpoints.py / test_agent_admin_endpoints.py."""

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend import main


@pytest.mark.asyncio
async def test_list_projects_calls_through():
    with patch("backend.projects.list_projects", return_value=[{"id": "proj-1", "name": "X"}]):
        result = await main.list_projects_api()
    assert result == [{"id": "proj-1", "name": "X"}]


@pytest.mark.asyncio
async def test_create_project_calls_through():
    payload = main.ProjectRequest(name="Сайт клиента", description="лендинг")
    with patch("backend.projects.create_project", return_value={"id": "proj-1"}) as create:
        result = await main.create_project_api(payload)
    assert result == {"id": "proj-1"}
    create.assert_called_once_with("Сайт клиента", "лендинг", True)


@pytest.mark.asyncio
async def test_create_project_rejects_invalid_input():
    payload = main.ProjectRequest(name="")
    with patch("backend.projects.create_project", side_effect=ValueError("Project name must be 1-80 characters")):
        with pytest.raises(HTTPException) as exc_info:
            await main.create_project_api(payload)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_update_project_not_found():
    payload = main.ProjectRequest(name="x")
    with patch("backend.projects.update_project", side_effect=KeyError("proj-missing")):
        with pytest.raises(HTTPException) as exc_info:
            await main.update_project_api("proj-missing", payload)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_project_calls_through():
    with patch("backend.projects.delete_project") as delete:
        result = await main.delete_project_api("proj-1")
    assert result == {"status": "success", "id": "proj-1"}
    delete.assert_called_once_with("proj-1")


@pytest.mark.asyncio
async def test_set_session_project_calls_through():
    payload = main.SessionProjectPayload(project_id="proj-1")
    with patch("backend.database.get_session_title", return_value="Chat 1"), \
         patch("backend.database.save_session_metadata") as save:
        result = await main.set_session_project("s1", payload)
    assert result["status"] == "success"
    save.assert_called_once_with("s1", "Chat 1", project_id="proj-1")


@pytest.mark.asyncio
async def test_set_session_project_none_clears_with_empty_string():
    """A null project_id in the request means 'unassign' — must translate to
    the empty-string clear sentinel, not None (which save_session_metadata
    treats as 'leave unchanged', so passing None straight through would
    silently no-op instead of clearing)."""
    payload = main.SessionProjectPayload(project_id=None)
    with patch("backend.database.get_session_title", return_value="Chat 1"), \
         patch("backend.database.save_session_metadata") as save:
        await main.set_session_project("s1", payload)
    save.assert_called_once_with("s1", "Chat 1", project_id="")
