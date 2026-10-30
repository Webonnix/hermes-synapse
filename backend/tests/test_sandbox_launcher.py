import copy
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio

from sandbox_launcher import server as launcher

NAME = "hermes-dev-runner-run-0123456789ab"
IMAGE = {"Id": "sha256:" + "a" * 64, "RepoTags": ["hermes-dev-runner:local"]}


def body():
    return {"Image": IMAGE["RepoTags"][0], "Env": ["DEV_RUNNER_TOKEN=synthetic-token"],
            "HostConfig": {"NetworkMode": "test_dev-net", "Binds": ["/logical/run-0123456789ab:/dev-repo:rw"]}}


@pytest_asyncio.fixture
async def broker(monkeypatch):
    requests = []
    async def docker(method, path, **kwargs):
        requests.append((method, path, kwargs))
        if path == "/containers/json":
            return httpx.Response(200, json=[])
        if path.startswith("/images/"):
            return httpx.Response(200, json=IMAGE)
        if path.startswith("/networks/"):
            return httpx.Response(200, json={"Labels": {"com.docker.compose.network": "dev-net"}})
        if path.startswith("/containers/") and path.endswith("/json"):
            name = path.split("/")[2]
            return httpx.Response(200, json={"Id": name, "Config": {"Labels": {}},
                "NetworkSettings": {"Networks": {"test_dev-net": {"IPAddress": "172.30.0.2"}}}})
        if path == "/containers/create":
            return httpx.Response(201, json={"Id": "sandbox-id", "Warnings": []})
        return httpx.Response(200, json={})
    monkeypatch.setattr(launcher, "docker", docker)
    monkeypatch.setattr(launcher, "_images", {})
    monkeypatch.setattr(launcher, "prepare_subpath", lambda *args: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=launcher.app), base_url="http://launcher") as client:
        yield client, requests


@pytest.mark.asyncio
async def test_broker_constructs_policy_and_never_forwards_host_binds(broker):
    client, requests = broker
    response = await client.post(f"/v1.45/containers/create?name={NAME}", json=body())
    assert response.status_code == 201
    payload = next(args["json"] for method, path, args in requests if path == "/containers/create")
    assert payload["Image"] == IMAGE["Id"]
    assert payload["User"] == "10001"
    host = payload["HostConfig"]
    assert host["Runtime"] == "runsc" and host["ReadonlyRootfs"]
    assert host["CapDrop"] == ["ALL"]
    assert "Binds" not in host
    assert host["Mounts"] == [{"Type": "volume", "Source": launcher.VOLUME, "Target": "/dev-repo",
                                "VolumeOptions": {"Subpath": "dev-repo-runs/run-0123456789ab", "NoCopy": True}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("Privileged", True), ("Runtime", "runc"), ("NetworkMode", "host"), ("PidMode", "host"),
    ("CapAdd", ["SYS_ADMIN"]), ("Devices", [{"PathOnHost": "/dev/sda"}]),
    ("Binds", ["/:/host:rw"]), ("Mounts", [{"Type": "bind", "Source": "/"}]),
    ("SecurityOpt", ["seccomp=unconfined"]), ("PortBindings", {"80/tcp": [{"HostPort": "8080"}]}),
])
async def test_broker_rejects_escape_before_any_create(broker, field, value):
    client, requests = broker
    payload = body(); payload["HostConfig"][field] = value
    assert (await client.post(f"/containers/create?name={NAME}", json=payload)).status_code == 403
    assert not any(path == "/containers/create" for _, path, _ in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"Image": "ubuntu:latest"}, {"Cmd": ["sh"]}, {"User": "0"},
                                     {"Env": ["LD_PRELOAD=/tmp/escape"]}, {"Entrypoint": ["/bin/sh"]}])
async def test_broker_rejects_unknown_images_and_programs(broker, change):
    client, requests = broker
    payload = body(); payload.update(change)
    assert (await client.post(f"/containers/create?name={NAME}", json=payload)).status_code == 403
    assert not any(path == "/containers/create" for _, path, _ in requests)


@pytest.mark.asyncio
async def test_unmanaged_containers_and_docker_admin_apis_are_inaccessible(broker):
    client, requests = broker
    for method, path in [("GET", "/containers/other-owner/json"), ("POST", "/containers/other-owner/stop"),
                         ("POST", "/containers/other-owner/exec"), ("POST", "/images/create"),
                         ("POST", "/volumes/create"), ("POST", "/networks/create"), ("POST", "/build")]:
        assert (await client.request(method, path)).status_code == 403
    assert not any(method != "GET" for method, _, _ in requests)


def test_volume_walk_refuses_symlink_to_host(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "DATA_ROOT", tmp_path)
    (tmp_path / "dev-repo-runs").mkdir()
    (tmp_path / "dev-repo-runs/run-0123456789ab").symlink_to("/tmp", target_is_directory=True)
    with pytest.raises(Exception) as error:
        launcher.prepare_subpath("dev-repo-runs/run-0123456789ab", 10001)
    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [b'{', b'[]', b'null', b'{"HostConfig":[]}', b'{"HostConfig":{"Binds":[null]}}', b'{"Env":"wrong"}'])
async def test_malformed_sdk_requests_fail_before_docker(broker, payload):
    client, requests = broker
    assert (await client.post('/containers/create?name=' + NAME, content=payload)).status_code == 422
    assert not requests


@pytest.mark.asyncio
async def test_streamed_create_body_has_a_bounded_size(broker):
    client, requests = broker
    async def chunks():
        for _ in range(20):
            yield b'x' * 4096
    assert (await client.post('/containers/create?name=' + NAME, content=chunks())).status_code == 413
    assert not requests
