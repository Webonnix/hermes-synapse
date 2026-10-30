"""Every protected route must refuse an anonymous caller before its handler."""
import re

import httpx
import pytest

from backend.main import app

PUBLIC = {"/api/auth/request-code", "/api/auth/verify-code", "/api/auth/login", "/api/auth/session",
          "/api/payments/webhook"}
SIGNED_WEBHOOKS = ("/api/social/webhook/", "/api/voice/webhook/", "/api/phone/twilio/", "/api/phone/telnyx/")
# OpenAPI expands included/lazy routers too; app.routes alone omits them in
# newer FastAPI versions.
ROUTES = [(method.upper(), re.sub(r"\{[^}]+\}", "security-probe", path))
          for path, operations in app.openapi()["paths"].items()
          if path.startswith("/api/") and path not in PUBLIC and not path.startswith(SIGNED_WEBHOOKS)
          for method in operations if method in {"get", "post", "put", "patch", "delete", "head"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", ROUTES)
async def test_anonymous_route_is_rejected_before_execution(method, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.request(method, path, content=b"{}", follow_redirects=False)
    assert response.status_code == 401, (method, path, response.status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/plots/test.png", "/api/generated-images/test.png"])
async def test_static_images_are_not_an_auth_exemption(path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        assert (await client.get(path)).status_code == 401
