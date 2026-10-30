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


def earner_body(mode="repo"):
    """The exact create body docker-py sends for an Earner run."""
    import docker
    from docker.models.containers import _create_container_args
    from backend.earner import sandbox as earner_sandbox

    kwargs = (earner_sandbox.repo_container_kwargs("opp-1") if mode == "repo"
              else earner_sandbox.container_kwargs("cGF5bG9hZA==", "opp-1"))
    kwargs.pop("detach")
    kwargs["version"] = "1.45"
    api = docker.APIClient(base_url="tcp://127.0.0.1:9", version="1.45")
    return api.create_container_config(**_create_container_args(kwargs))


@pytest.fixture
def earner_images(monkeypatch):
    monkeypatch.setenv("EARNER_SANDBOX_IMAGE", "hermes-dev-runner:local")
    monkeypatch.setenv("SANDBOX_EARNER_IMAGES", "hermes-dev-runner:local")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["repo", "unit"])
async def test_earner_run_is_rebuilt_offline_without_mounts(broker, earner_images, mode):
    client, requests = broker
    response = await client.post("/v1.45/containers/create", json=earner_body(mode))
    assert response.status_code == 201
    payload = next(args["json"] for method, path, args in requests if path == "/containers/create")
    host = payload["HostConfig"]
    assert payload["Image"] == IMAGE["Id"] and payload["User"] == "10001:10001"
    assert host["Runtime"] == "runsc" and host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
    assert host["CapDrop"] == ["ALL"] and "Mounts" not in host and "Binds" not in host
    assert payload["Labels"][launcher.MARKER] == "1"
    assert payload["OpenStdin"] is (mode == "repo")


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("NetworkMode", "bridge"), ("Runtime", "runc"), ("Privileged", True), ("Binds", ["/:/host:rw"]),
    ("Memory", 64 * 1024**3), ("PidsLimit", 100000), ("CapAdd", ["SYS_ADMIN"]), ("ReadonlyRootfs", False),
    ("Tmpfs", {"/tmp": "rw,exec,size=1g,mode=1777"}),
])
async def test_earner_run_refuses_reach_beyond_the_template(broker, earner_images, field, value):
    client, requests = broker
    payload = earner_body(); payload["HostConfig"][field] = value
    assert (await client.post("/containers/create", json=payload)).status_code == 403
    assert not any(path == "/containers/create" for _, path, _ in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"Image": "ubuntu:latest"}, {"User": "0"}, {"Entrypoint": ["/bin/sh", "-c", "x", "y"]},
                                    {"Env": ["LD_PRELOAD=/tmp/x"]}, {"Cmd": ["rm -rf /"]}])
async def test_earner_run_refuses_other_images_and_identities(broker, earner_images, change):
    client, requests = broker
    payload = earner_body(); payload.update(change)
    assert (await client.post("/containers/create", json=payload)).status_code == 403
    assert not any(path == "/containers/create" for _, path, _ in requests)


@pytest.mark.asyncio
async def test_logs_and_input_only_for_owned_sandboxes(broker):
    client, _ = broker
    for method, path in [("GET", "/containers/vexa-backend/logs?stdout=1"), ("POST", "/containers/vexa-backend/kill"),
                         ("POST", "/hermes/earner/vexa-backend/stdin")]:
        assert (await client.request(method, path)).status_code == 403


@pytest.mark.asyncio
async def test_refusals_carry_the_docker_message_field(broker):
    client, _ = broker
    response = await client.post("/networks/create")
    assert response.status_code == 403 and response.json()["message"]


@pytest.mark.asyncio
async def test_networks_are_approved_by_name_or_id(monkeypatch):
    async def docker(method, path, **kwargs):
        if path.startswith("/containers/"):
            return httpx.Response(200, json={"Id": "r", "NetworkSettings": {"Networks": {"hermes-dev-net": {}}}})
        return httpx.Response(200, json={"Id": "abc123", "Labels": {"com.docker.compose.network": "dev-net"}})
    monkeypatch.setattr(launcher, "docker", docker)
    assert await launcher.allowed_networks() == {"hermes-dev-net", "abc123"}
