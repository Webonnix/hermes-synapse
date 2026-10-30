"""A template broker, with a small Docker SDK-compatible API surface.

Only this process sees the Docker socket. The backend cannot supply a host
mount, command, privilege, namespace, port, device, image or runtime policy.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import stat
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(HTTPException)
async def docker_style_error(_request: Request, exc: HTTPException) -> JSONResponse:
    # The Docker SDK reads "message"; FastAPI's own clients read "detail".
    return JSONResponse({"message": str(exc.detail), "detail": exc.detail}, status_code=exc.status_code)
MARKER = "hermes.launcher"
DATA_ROOT = Path("/data")
VOLUME = os.getenv("SANDBOX_DATA_VOLUME", "hermes-data")
RUNNER = os.getenv("DEV_RUNNER_CONTAINER_NAME", "hermes-dev-runner")
BACKEND = os.getenv("BACKEND_CONTAINER_NAME", "vexa-backend")
NETWORK_LABELS = frozenset(x.strip() for x in os.getenv("SANDBOX_NETWORK_LABELS", "dev-net,dev-egress-net").split(",") if x.strip())
EARNER_LABEL = "hermes.earner.sandbox"
EARNER_ENTRYPOINT = ["python", "-I", "-c"]
EARNER_MAX_MEMORY = 2 * 1024**3
EARNER_MAX_CPUS = 2_000_000_000
EARNER_MAX_PIDS = 256
EARNER_MAX_STDIN = 64 * 1024 * 1024
_images: dict[str, dict] = {}
_launch_lock = asyncio.Lock()


async def docker(method: str, path: str, *, timeout: float | None = 70, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds="/var/run/docker.sock"),
                                 base_url="http://docker", timeout=timeout) as client:
        return await client.request(method, path, **kwargs)


async def inspect(name: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
        raise HTTPException(403, "Invalid identifier")
    response = await docker("GET", f"/containers/{name}/json")
    if response.status_code != 200:
        raise HTTPException(404, "Container not found")
    return response.json()


def owned(container: dict) -> bool:
    return container.get("Config", {}).get("Labels", {}).get(MARKER) == "1"


async def allowed_container(name: str, *, write=False) -> dict:
    container = await inspect(name)
    if owned(container):
        return container
    if not write:
        for source in (RUNNER, BACKEND):
            known = await inspect(source)
            if known["Id"] == container["Id"]:
                # Only network and image metadata; never unrelated env/secrets.
                return {k: container[k] for k in ("Id", "Name", "Image", "NetworkSettings") if k in container}
    raise HTTPException(403, "Container is outside launcher ownership")


async def image_for(kind: str) -> dict:
    name = {"dev": os.getenv("SANDBOX_DEV_IMAGE", "hermes-dev-runner:local"),
            "agent": os.getenv("SANDBOX_AGENT_IMAGE", "hermes-agent-desktop:local"),
            "openbot": os.getenv("SANDBOX_OPENBOT_IMAGE", "hermes-openbot-desktop:local")}[kind]
    if kind not in _images:
        result = await docker("GET", f"/images/{name}/json")
        if result.status_code != 200:
            raise HTTPException(503, "Approved sandbox image must be built before use")
        _images[kind] = result.json()
    return _images[kind]


def earner_image_names() -> list[str]:
    return [x.strip() for x in os.getenv("SANDBOX_EARNER_IMAGES", "").split(",") if x.strip()]


async def earner_image(reference: str) -> dict:
    for name in earner_image_names():
        key = "earner:" + name
        if key not in _images:
            result = await docker("GET", f"/images/{name}/json")
            if result.status_code != 200:
                continue
            _images[key] = result.json()
        image = _images[key]
        if reference in (name, image["Id"], *image.get("RepoTags", []), *image.get("RepoDigests", [])):
            return image
    raise HTTPException(403, "Image is not an approved Earner sandbox image")


async def allowed_networks() -> set[str]:
    source = await inspect(RUNNER)
    allowed = set()
    for name in source.get("NetworkSettings", {}).get("Networks", {}):
        response = await docker("GET", f"/networks/{name}")
        if response.status_code == 200:
            network = response.json()
            label = network.get("Labels", {}).get("com.docker.compose.network")
            # No inferred fallback to the backend or daemon-control network.
            if label in NETWORK_LABELS:
                # docker-py addresses a network by name or by id; both name the same one.
                allowed.update({name, network.get("Id", name)})
    if not allowed:
        raise HTTPException(503, "Sandbox networks unavailable")
    return allowed


def template(name: str, body: dict) -> tuple[str, str, str, int]:
    if re.fullmatch(r"hermes-dev-runner-run-[0-9a-f]{12}", name):
        return "dev", name.removeprefix("hermes-dev-runner-"), "/dev-repo", 10001
    key = name.removeprefix("hermes-agent-desktop-")
    if name.startswith("hermes-agent-desktop-") and re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?", key):
        kind = body.get("Labels", {}).get("hermes.agent-desktop.kind", "agent")
        if kind in ("agent", "openbot"):
            return kind, key, "/data" if kind == "openbot" else "/home/agent", 1000 if kind == "openbot" else 10001
    raise HTTPException(403, "Unknown sandbox template")


def verify_request(body: dict) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("HostConfig"), dict):
        raise HTTPException(422, "Invalid sandbox configuration")
    host = body["HostConfig"]
    forbidden = ("Privileged", "CapAdd", "Devices", "DeviceRequests", "DeviceCgroupRules", "PortBindings",
                 "PublishAllPorts", "VolumesFrom", "Mounts", "PidMode", "IpcMode", "UTSMode", "CgroupnsMode",
                 "UsernsMode", "VolumeDriver", "StorageOpt", "Sysctls", "CgroupParent", "Links")
    if any(host.get(key) for key in forbidden):
        raise HTTPException(403, "Sandbox privilege or namespace override refused")
    if any(body.get(key) for key in ("Cmd", "Entrypoint", "User", "WorkingDir", "Hostname", "Domainname")):
        raise HTTPException(403, "Sandbox entrypoint override refused")
    if host.get("Runtime") not in (None, "runsc"):
        raise HTTPException(403, "Only runsc is permitted")
    if host.get("SecurityOpt") not in (None, ["no-new-privileges:true"]):
        raise HTTPException(403, "Sandbox security override refused")
    if host.get("CapDrop") not in (None, ["ALL"]):
        raise HTTPException(403, "Sandbox capability override refused")


def prepare_subpath(subpath: str, uid: int) -> None:
    """Walk from the fixed volume using held directory handles, never symlinks."""
    fd = os.open(DATA_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for segment in subpath.split("/"):
            if segment in ("", ".", ".."):
                raise HTTPException(403, "Invalid volume subpath")
            child = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        for _, directories, files, root_fd in os.fwalk(".", dir_fd=fd, follow_symlinks=False):
            os.fchown(root_fd, uid, uid)
            for filename in files:
                try:
                    file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
                except OSError:
                    continue
                try:
                    if stat.S_ISREG(os.fstat(file_fd).st_mode):
                        os.fchown(file_fd, uid, uid)
                finally:
                    os.close(file_fd)
    except OSError as exc:
        raise HTTPException(403, "Sandbox data path unavailable or contains a symlink") from exc
    finally:
        os.close(fd)


def install_openbot_bootstrap(key: str) -> None:
    # Open every parent with NOFOLLOW; root authority never follows an agent's
    # symlink while installing the one backend-managed file.
    fd = os.open(DATA_ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for segment in ("agent-desktops", key):
            child = os.open(segment, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        bootstrap = os.open("bootstrap", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        home = os.open("home", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            try:
                source = os.open("HERMES-MODEL.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=bootstrap)
            except FileNotFoundError:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink("HERMES-MODEL.json", dir_fd=home)
                return
            with os.fdopen(source, "rb") as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise HTTPException(413, "Bootstrap too large")
            target = os.open("HERMES-MODEL.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                             0o600, dir_fd=home)
            with os.fdopen(target, "wb") as stream:
                os.fchown(stream.fileno(), 1000, 1000)
                stream.write(data)
        finally:
            os.close(bootstrap); os.close(home)
    finally:
        os.close(fd)


_TMPFS = re.compile(r"rw,noexec,nosuid,nodev,size=([0-9]+)([mg]),mode=1777")
_EARNER_HOST_KEYS = {"Runtime", "NetworkMode", "ReadonlyRootfs", "CapDrop", "SecurityOpt", "IpcMode", "Tmpfs",
                     "Memory", "MemorySwap", "NanoCpus", "PidsLimit", "Ulimits", "LogConfig", "Privileged",
                     "Binds", "Devices"}


def earner_template(body: dict, image: dict) -> dict:
    """Rebuild an Earner run from validated fields only. The code inside is
    untrusted by design; what the backend cannot choose is the container's reach."""
    host = body.get("HostConfig") or {}
    if any(value for key, value in host.items() if key not in _EARNER_HOST_KEYS):
        raise HTTPException(403, "Earner sandbox host option refused")
    if host.get("Privileged") or host.get("Binds") or host.get("Devices"):
        raise HTTPException(403, "Earner sandbox privilege or mount refused")
    if (host.get("Runtime") != "runsc" or host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True
            or host.get("CapDrop") != ["ALL"] or host.get("SecurityOpt") != ["no-new-privileges:true"]
            or host.get("IpcMode") not in (None, "none")):
        raise HTTPException(403, "Earner sandbox must be offline gVisor with no capabilities")
    tmpfs = host.get("Tmpfs") or {}
    if set(tmpfs) != {"/tmp"} or not (match := _TMPFS.fullmatch(str(tmpfs["/tmp"]))):
        raise HTTPException(403, "Earner sandbox tmpfs refused")
    if int(match[1]) * (1024 if match[2] == "g" else 1) > 1024:
        raise HTTPException(403, "Earner sandbox tmpfs too large")
    memory, cpus, pids = int(host.get("Memory") or 0), int(host.get("NanoCpus") or 0), int(host.get("PidsLimit") or 0)
    if not (0 < memory <= EARNER_MAX_MEMORY and host.get("MemorySwap") == memory
            and 0 < cpus <= EARNER_MAX_CPUS and 0 < pids <= EARNER_MAX_PIDS):
        raise HTTPException(403, "Earner sandbox limits refused")
    ulimits = []
    for item in host.get("Ulimits") or []:
        if not isinstance(item, dict) or item.get("Name") not in ("nofile", "fsize"):
            raise HTTPException(403, "Earner sandbox ulimit refused")
        cap = 1024 if item["Name"] == "nofile" else 64 * 1024 * 1024
        if not (0 < int(item.get("Soft", 0)) <= int(item.get("Hard", 0)) <= cap):
            raise HTTPException(403, "Earner sandbox ulimit refused")
        ulimits.append({"Name": item["Name"], "Soft": int(item["Soft"]), "Hard": int(item["Hard"])})
    entrypoint, command = body.get("Entrypoint"), body.get("Cmd") or []
    if (not isinstance(entrypoint, list) or len(entrypoint) != 4 or entrypoint[:3] != EARNER_ENTRYPOINT
            or not isinstance(entrypoint[3], str) or len(entrypoint[3]) > 65536):
        raise HTTPException(403, "Earner sandbox entrypoint refused")
    if not isinstance(command, list) or len(command) > 1 or not all(isinstance(x, str) and x.isdigit() for x in command):
        raise HTTPException(403, "Earner sandbox command refused")
    if body.get("User") != "10001:10001" or body.get("WorkingDir") not in (None, "/tmp") or body.get("Tty"):
        raise HTTPException(403, "Earner sandbox identity refused")
    env = body.get("Env") or []
    if not all(isinstance(x, str) and x.partition("=")[0] == "EARNER_PAYLOAD" and len(x) <= 256 * 1024 for x in env):
        raise HTTPException(403, "Unexpected sandbox environment")
    labels = body.get("Labels") or {}
    opportunity = str(labels.get("hermes.earner.opportunity", ""))
    if (labels.get(EARNER_LABEL) != "1" or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", opportunity)
            or set(labels) - {EARNER_LABEL, "hermes.earner.opportunity", "hermes.earner.mode"}
            or labels.get("hermes.earner.mode") not in (None, "repo")):
        raise HTTPException(403, "Earner sandbox labels refused")
    safe_labels = {MARKER: "1", EARNER_LABEL: "1", "hermes.earner.opportunity": opportunity}
    if labels.get("hermes.earner.mode"):
        safe_labels["hermes.earner.mode"] = "repo"
    return {"Image": image["Id"], "Entrypoint": entrypoint, "Cmd": command, "User": "10001:10001",
            "WorkingDir": "/tmp", "Env": env, "Labels": safe_labels, "Hostname": "earner-sandbox",
            "OpenStdin": bool(body.get("OpenStdin")), "StdinOnce": bool(body.get("OpenStdin")),
            "AttachStdin": False, "Tty": False, "NetworkDisabled": True,
            "HostConfig": {"Runtime": "runsc", "NetworkMode": "none", "ReadonlyRootfs": True, "CapDrop": ["ALL"],
                           "SecurityOpt": ["no-new-privileges:true"], "IpcMode": "none",
                           "Tmpfs": {"/tmp": str(tmpfs["/tmp"])}, "Memory": memory, "MemorySwap": memory,
                           "NanoCpus": cpus, "PidsLimit": pids, "Ulimits": ulimits,
                           "LogConfig": {"Type": "json-file", "Config": {"max-size": "512k", "max-file": "1"}},
                           "RestartPolicy": {"Name": "no"}}}


async def launch_earner(body: dict) -> httpx.Response:
    image = await earner_image(str(body.get("Image", "")))
    safe = earner_template(body, image)
    async with _launch_lock:
        current = await docker("GET", "/containers/json", params={"all": "1"})
        if current.status_code != 200 or not isinstance(current.json(), list):
            raise HTTPException(503, "Cannot verify sandbox quota")
        running = [c for c in current.json() if c.get("Labels", {}).get(EARNER_LABEL) == "1"]
        if len(running) >= int(os.getenv("SANDBOX_EARNER_MAX_CONTAINERS", "2")):
            raise HTTPException(429, "Earner sandbox capacity exhausted")
        return await docker("POST", "/containers/create", json=safe)


async def send_stdin(container_id: str, request: Request) -> Response:
    """Stream a request body into an owned Earner sandbox's stdin over Docker's attach."""
    reader, writer = await asyncio.open_unix_connection("/var/run/docker.sock")
    try:
        writer.write((f"POST /containers/{container_id}/attach?stdin=1&stream=1 HTTP/1.1\r\n"
                      "Host: docker\r\nConnection: Upgrade\r\nUpgrade: tcp\r\nContent-Length: 0\r\n\r\n").encode())
        await writer.drain()
        status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        if b" 101 " not in status.split(b"\r\n", 1)[0] and b" 200 " not in status.split(b"\r\n", 1)[0]:
            raise HTTPException(502, "Sandbox stdin attach refused")
        sent = 0
        async for chunk in request.stream():
            sent += len(chunk)
            if sent > EARNER_MAX_STDIN:
                raise HTTPException(413, "Sandbox input too large")
            writer.write(chunk)
            await asyncio.wait_for(writer.drain(), 120)
        if writer.can_write_eof():
            writer.write_eof()
        await asyncio.wait_for(writer.drain(), 120)
        return JSONResponse({"bytes": sent})
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def launch(name: str, body: dict) -> httpx.Response:
    async with _launch_lock:
        return await _launch(name, body)


async def _launch(name: str, body: dict) -> httpx.Response:
    verify_request(body)
    kind, key, destination, uid = template(name, body)
    image = await image_for(kind)
    if body.get("Image") not in (image["Id"], *image.get("RepoTags", []), *image.get("RepoDigests", [])):
        raise HTTPException(403, "Image is not the approved template digest")
    networks = await allowed_networks()
    network = body["HostConfig"].get("NetworkMode", "")
    if network not in networks:
        raise HTTPException(403, "Sandbox network is not approved")
    supplied_networks = body.get("NetworkingConfig", {}).get("EndpointsConfig", {})
    if set(supplied_networks) - networks:
        raise HTTPException(403, "Additional network refused")
    subpath = f"dev-repo-runs/{key}" if kind == "dev" else f"agent-desktops/{key}/home"
    # Legacy SDK bind descriptions are validated as logical data references;
    # they are NEVER forwarded as host bind mounts to Docker.
    binds = body["HostConfig"].get("Binds") or []
    main_suffix = f"/{key}" if kind == "dev" else f"/{key}/home"
    if len(binds) not in ((1,) if kind == "dev" else (1, 2)):
        raise HTTPException(403, "Unexpected sandbox mounts")
    for bind in binds:
        parts = bind.rsplit(":", 2)
        if len(parts) != 3:
            raise HTTPException(403, "Invalid mount description")
        source, target, mode = parts
        if target == destination and source.endswith(main_suffix) and mode == "rw":
            continue
        if kind != "dev" and target == "/etc/resolv.conf" and source.endswith("/resolv.conf") and mode == "ro":
            continue
        raise HTTPException(403, "Host mount refused")
    await asyncio.to_thread(prepare_subpath, subpath, uid)
    if kind == "openbot":
        await asyncio.to_thread(install_openbot_bootstrap, key)
    env = body.get("Env") or []
    names = {"DEV_RUNNER_TOKEN", "DEV_RUNNER_REPO_ROOT"} if kind == "dev" else {
        "AGENT_DESKTOP_TOKEN", "AGENT_DESKTOP_VNC_PORTS", "OPENBOT_DISPLAY_SIZE", "OPENCODE_CONFIG"}
    if not all(isinstance(value, str) and value.partition("=")[0] in names for value in env):
        raise HTTPException(403, "Unexpected sandbox environment")
    hosts = body["HostConfig"].get("ExtraHosts") or []
    backend = await inspect(BACKEND)
    backend_ips = {n.get("IPAddress") for n in backend.get("NetworkSettings", {}).get("Networks", {}).values()}
    if any(not value.startswith("hermes-backend:") or value.partition(":")[2] not in backend_ips for value in hosts):
        raise HTTPException(403, "Host mapping refused")
    memory, cpus, pids = (2 * 1024**3, 2_000_000_000, 512) if kind == "dev" else (6 * 1024**3, 4_000_000_000, 2048)
    current = await docker("GET", "/containers/json", params={"all": "1"})
    if current.status_code != 200 or not isinstance(current.json(), list):
        raise HTTPException(503, "Cannot verify sandbox quota")
    managed = [c for c in current.json() if c.get("Labels", {}).get(MARKER) == "1"]
    reserved = 0
    for container in managed:
        reserved += int((await inspect(container["Id"])).get("HostConfig", {}).get("Memory") or 0)
    if len(managed) >= int(os.getenv("SANDBOX_MAX_CONTAINERS", "4")) or reserved + memory > int(os.getenv("SANDBOX_MEMORY_BUDGET", str(8 * 1024**3))):
        raise HTTPException(429, "Sandbox capacity exhausted; remove a stopped sandbox first")
    safe = {"Image": image["Id"], "User": str(uid), "Env": env,
            "Labels": {MARKER: "1", **({"hermes.agent-desktop": "1", "hermes.agent-desktop.key": key,
                                      "hermes.agent-desktop.kind": kind} if kind != "dev" else {})},
            "HostConfig": {"Runtime": "runsc", "NetworkMode": network, "ReadonlyRootfs": True,
                "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                "Memory": memory, "MemorySwap": memory, "NanoCpus": cpus, "PidsLimit": pids,
                "RestartPolicy": {"Name": "no"}, "ExtraHosts": hosts,
                "Tmpfs": {"/tmp": f"size=1g,mode=1777,uid={uid},gid={uid}",
                          "/run/openbot": "size=16m,uid=1000,gid=1000,mode=0700"},
                "ShmSize": 1024**3, "Dns": ["1.1.1.1", "8.8.8.8"],
                "Mounts": [{"Type": "volume", "Source": VOLUME, "Target": destination,
                            "VolumeOptions": {"Subpath": subpath, "NoCopy": True}}]}}
    return await docker("POST", "/containers/create", params={"name": name}, json=safe)


@app.get("/health")
async def health():
    response = await docker("GET", "/_ping")
    return Response(status_code=200 if response.status_code == 200 else 503)


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
async def broker(request: Request, path: str):
    path = re.sub(r"^v[0-9.]+/", "", path)
    response = None
    if request.method == "GET" and path in ("_ping", "version", "info"):
        response = await docker("GET", "/" + path)
        if path == "info" and response.status_code == 200:
            return JSONResponse({"Runtimes": response.json().get("Runtimes", {})})
    elif request.method == "POST" and path == "containers/create":
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise HTTPException(413, "Configuration too large")
        try:
            body = json.loads(raw)
            # Nested values are untrusted Docker SDK input too.
            if not isinstance(body, dict):
                raise ValueError()
            # docker-py spells unset options as null; absent and null mean the same.
            body = {key: value for key, value in body.items() if value is not None}
            if isinstance(body.get("HostConfig"), dict):
                body["HostConfig"] = {key: value for key, value in body["HostConfig"].items() if value is not None}
            for field in ("HostConfig", "Labels", "NetworkingConfig"):
                if field in body and not isinstance(body[field], dict):
                    raise ValueError()
            host = body.get("HostConfig", {})
            for field in ("Binds", "ExtraHosts"):
                if host.get(field) is not None and (not isinstance(host[field], list) or
                        not all(isinstance(value, str) for value in host[field])):
                    raise ValueError()
            if body.get("Env") is not None and not isinstance(body["Env"], list):
                raise ValueError()
            if not isinstance(body.get("NetworkingConfig", {}).get("EndpointsConfig", {}), dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid sandbox configuration")
        if (body.get("Labels") or {}).get(EARNER_LABEL):
            response = await launch_earner(body)
        else:
            response = await launch(request.query_params.get("name", ""), body)
    elif request.method == "GET" and path == "containers/json":
        response = await docker("GET", "/containers/json", params={"all": "1"})
        if response.status_code == 200:
            candidates = [c for c in response.json() if c.get("Labels", {}).get(MARKER) == "1"]
            if request.query_params.get("all") not in ("1", "true"):
                candidates = [c for c in candidates if c.get("State") == "running"]
            try:
                filters = json.loads(request.query_params.get("filters", "{}"))
                for label in filters.get("label", []):
                    key, separator, value = label.partition("=")
                    candidates = [c for c in candidates if key in c.get("Labels", {}) and
                                  (not separator or c["Labels"][key] == value)]
            except (ValueError, AttributeError, TypeError):
                raise HTTPException(422, "Invalid filters")
            return JSONResponse(candidates)
    elif request.method == "POST" and (match := re.fullmatch(r"hermes/earner/([A-Za-z0-9_.-]+)/stdin", path)):
        container = await allowed_container(match[1], write=True)
        if container.get("Config", {}).get("Labels", {}).get(EARNER_LABEL) != "1" or not container.get("Config", {}).get("OpenStdin"):
            raise HTTPException(403, "Only an Earner repository sandbox accepts input")
        return await send_stdin(container["Id"], request)
    elif match := re.fullmatch(r"containers/([A-Za-z0-9_.-]+)(?:/(json|start|stop|wait|logs|kill))?", path):
        name, action = match.groups()
        container = await allowed_container(name, write=request.method != "GET")
        if request.method == "GET" and action == "json":
            return JSONResponse(container)
        if action in ("wait", "logs", "kill") and not owned(container):
            # The backend and runner are readable for network metadata only.
            raise HTTPException(403, "Container is outside launcher ownership")
        if (request.method, action) == ("POST", "wait"):
            response = await docker("POST", f"/containers/{container['Id']}/wait", timeout=None)
        elif (request.method, action) == ("GET", "logs"):
            params = {key: request.query_params[key] for key in ("stdout", "stderr", "tail", "timestamps")
                      if key in request.query_params}
            response = await docker("GET", f"/containers/{container['Id']}/logs", params=params)
        elif (request.method, action) == ("POST", "kill"):
            response = await docker("POST", f"/containers/{container['Id']}/kill")
        elif (request.method, action) in (("POST", "start"), ("POST", "stop"), ("DELETE", None)):
            if request.method == "DELETE" and container.get("State", {}).get("Running"):
                stopped = await docker("POST", f"/containers/{container['Id']}/stop", params={"t": "60"})
                if stopped.status_code not in (204, 304):
                    raise HTTPException(503, "Sandbox did not stop cleanly")
            response = await docker(request.method, f"/containers/{container['Id']}" + (f"/{action}" if action else ""),
                                    params={"t": "60"})
    elif request.method == "GET" and (match := re.fullmatch(r"images/(.+)/json", path)):
        for kind in ("dev", "agent", "openbot"):
            try:
                image = await image_for(kind)
            except HTTPException:
                continue
            if match[1] in (image["Id"], *image.get("RepoTags", []), *image.get("RepoDigests", [])):
                return JSONResponse(image)
        with contextlib.suppress(HTTPException):
            return JSONResponse(await earner_image(match[1]))
    elif match := re.fullmatch(r"networks/([A-Za-z0-9_.-]+)(?:/(connect))?", path):
        name, action = match.groups()
        if name not in await allowed_networks():
            raise HTTPException(403, "Network refused")
        if request.method == "GET" and action is None:
            response = await docker("GET", "/networks/" + name)
        elif request.method == "POST" and action == "connect":
            body = await request.json()
            container = await allowed_container(str(body.get("Container", "")), write=True)
            if container.get("State", {}).get("Running"):
                raise HTTPException(409, "Networks must be attached before sandbox startup")
            response = await docker("POST", f"/networks/{name}/connect", json={"Container": container["Id"]})
    if response is None:
        raise HTTPException(403, "Docker operation is not exposed by the launcher")
    return Response(response.content, status_code=response.status_code,
                    media_type=response.headers.get("content-type", "application/json"))
