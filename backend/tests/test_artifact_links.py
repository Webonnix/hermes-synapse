from urllib.parse import urlsplit, parse_qs

import pytest

from backend import artifact_links


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("ARTIFACT_SIGNING_KEY", "synthetic-test-signing-key-32-characters")


def test_signature_is_path_bound_and_expires(monkeypatch):
    now = [1000]
    monkeypatch.setattr(artifact_links.time, "time", lambda: now[0])
    path = "/api/generated-images/test.png"
    query = parse_qs(urlsplit(artifact_links.sign(path)).query)
    expires, signature = query["expires"][0], query["signature"][0]
    assert artifact_links.valid(path, expires, signature)
    assert not artifact_links.valid(path.replace("test", "other"), expires, signature)
    now[0] += 300
    assert not artifact_links.valid(path, expires, signature)


@pytest.mark.parametrize("path", ["/api/plots/../secret.png", "/api/plots/x.svg", "/api/plots/.env", "/etc/passwd"])
def test_cannot_sign_non_image_or_traversal(path):
    with pytest.raises(ValueError):
        artifact_links.sign(path)
