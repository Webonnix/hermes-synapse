"""Black-box checks against a running Hermes, through nginx, as an anonymous caller.

    python ops/dast_probe.py https://192.168.0.200:9443 [--routes routes.txt]

Only unauthenticated requests are sent, so it is safe against production: each
must be refused before any handler runs. --routes takes "METHOD /path" lines
(e.g. from `python -m ops.dump_routes`); without it a built-in sample is used.
Exit 0 = every check passed, 1 = findings (printed one per line).
"""
from __future__ import annotations

import argparse
import base64
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

SAMPLE_ROUTES = ["GET /api/control-plane/summary", "POST /api/control-plane/kill", "GET /api/config",
                 "PUT /api/agent-autonomy", "GET /api/earner/overview", "POST /api/subagents",
                 "GET /api/control-plane/approval-rules", "GET /api/agent-desktops", "GET /api/history"]
REQUIRED_HEADERS = {"content-security-policy": "default-src 'self'", "strict-transport-security": "max-age=",
                    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer"}
_CONTEXT = ssl.create_default_context()
_CONTEXT.check_hostname = False
_CONTEXT.verify_mode = ssl.CERT_NONE  # the LAN deployment uses a self-signed certificate


def request(base: str, method: str, path: str, headers: dict | None = None, body: bytes | None = None):
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, context=_CONTEXT, timeout=15) as response:
            return response.status, dict(response.headers.items()), response.read(4096)
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read(4096)


def websocket_status(base: str, path: str) -> int:
    parts = urlsplit(base)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    raw = socket.create_connection((parts.hostname, port), timeout=10)
    sock = _CONTEXT.wrap_socket(raw, server_hostname=parts.hostname) if parts.scheme == "https" else raw
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {base}\r\n\r\n").encode())
    line = sock.recv(256).split(b"\r\n", 1)[0].decode(errors="replace")
    sock.close()
    return int(line.split()[1]) if len(line.split()) > 1 else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("--routes")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    findings: list[str] = []

    status, headers, _ = request(base, "GET", "/")
    lowered = {k.lower(): v for k, v in headers.items()}
    if status != 200:
        findings.append(f"GET / -> {status}")
    for name, needle in REQUIRED_HEADERS.items():
        if needle not in lowered.get(name, ""):
            findings.append(f"missing or weak header {name}")
    if any(ch.isdigit() for ch in lowered.get("server", "")):
        findings.append(f"server version disclosed: {lowered['server']}")

    routes = SAMPLE_ROUTES
    if args.routes:
        with open(args.routes, encoding="utf-8") as stream:
            routes = [line.strip() for line in stream if line.strip()]
    for line in routes:
        method, path = line.split(" ", 1)
        status, _, _ = request(base, method, path, {"Content-Type": "application/json"},
                               b"{}" if method in ("POST", "PUT", "PATCH") else None)
        if status not in (401, 403, 405):
            findings.append(f"anonymous {method} {path} -> {status}")

    for path, expected in (("/api/plots/probe.png", (401,)), ("/api/generated-images/probe.png", (401,)),
                           ("/llm/v1/models", (401, 403)), ("/agent-llm/v1/models", (401, 403, 404))):
        status, headers, _ = request(base, "GET", path)
        spa_fallback = status == 200 and "text/html" in {k.lower(): v for k, v in headers.items()}.get("content-type", "")
        if status not in expected and not spa_fallback:  # nginx serves the app shell for unknown paths
            findings.append(f"anonymous GET {path} -> {status}")

    status, _, _ = request(base, "POST", "/api/auth/login",
                           {"Content-Type": "application/json", "Origin": "https://evil.example"},
                           b'{"username":"probe","password":"probe"}')
    if status != 403:
        findings.append(f"foreign-origin login not refused -> {status}")

    try:
        ws = websocket_status(base, "/ws")
        if ws == 101:
            findings.append("anonymous WebSocket /ws upgraded")
    except OSError as error:
        findings.append(f"WebSocket probe failed: {error}")

    status, _, _ = request(base, "TRACE", "/")
    if status == 200:
        findings.append("TRACE enabled")

    for finding in findings:
        print(finding)
    print(f"{len(routes)} routes, {len(findings)} findings")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
