"""The Earner's only way to the internet: a bounded HTTPS GET.

Off until the operator lists exact hosts. Then, per request:

* ``https://`` on port 443 only, no credentials in the URL, no IP literals;
* the host must equal an allowlisted DNS name (no suffix or wildcard match);
* the name is resolved once and **every** address must be public — loopback,
  private, link-local (cloud metadata), CGNAT, multicast, reserved and
  IPv4-mapped forms are refused;
* the connection goes to that vetted address with SNI and certificate checks
  for the name. Nothing re-resolves the name afterwards, so a DNS answer that
  changes between the check and the connect (rebinding) has nothing to hit;
* redirects are never followed — a redirect is a second, unchecked URL;
* the response is capped in bytes and the whole exchange in seconds;
* proxy environment variables are ignored (the socket is opened by hand).

Counting requests against the run and the day is the caller's job (scout.py).
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from backend.earner import store

MAX_BYTES = 512 * 1024
TIMEOUT_SECONDS = 15.0
USER_AGENT = "Hermes-Earner/1.0 (operator-configured research; no automated applications)"


class EgressError(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes
    address: str
    fetched_at: str


Resolver = Callable[[str, int], Iterable[Tuple]]


def check_url(url: str, allowed_hosts: Iterable[str]) -> Tuple[str, str]:
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        raise EgressError("разрешён только https://")
    if parsed.username or parsed.password:
        raise EgressError("учётные данные в URL запрещены")
    try:
        port = parsed.port
    except ValueError as exc:
        raise EgressError("недопустимый порт") from exc
    if port not in (None, 443):
        raise EgressError("разрешён только порт 443")
    raw_host = parsed.hostname or ""
    try:
        ipaddress.ip_address(raw_host)
        raise EgressError("IP-адрес вместо имени хоста запрещён")
    except ValueError:
        pass
    try:
        host = store.normalize_host(raw_host)
    except ValueError as exc:
        raise EgressError(str(exc)) from exc
    if host not in set(allowed_hosts):
        raise EgressError(f"хост {host} не в списке разрешённых")
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    return host, target


def is_public_address(raw: str) -> bool:
    try:
        address = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped or address.sixtofour
        if mapped is not None:
            return is_public_address(str(mapped))
        if address.teredo:
            return False
    return bool(address.is_global) and not (
        address.is_multicast or address.is_reserved or address.is_loopback
        or address.is_link_local or address.is_private or address.is_unspecified)


def resolve_public(host: str, resolver: Optional[Resolver] = None) -> str:
    resolve = resolver or (lambda name, port: socket.getaddrinfo(name, port, type=socket.SOCK_STREAM))
    try:
        infos = list(resolve(host, 443))
    except socket.gaierror as exc:
        raise EgressError(f"имя {host} не разрешается") from exc
    addresses: List[str] = [info[4][0] for info in infos]
    if not addresses:
        raise EgressError(f"имя {host} не разрешается")
    for address in addresses:
        if not is_public_address(address):
            raise EgressError(f"{host} указывает на непубличный адрес — запрос заблокирован")
    return addresses[0]


def _connect(host: str, address: str, timeout: float) -> http.client.HTTPConnection:
    """TLS to the vetted address, certificate checked for ``host``."""
    context = ssl.create_default_context()
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    raw = socket.socket(family, socket.SOCK_STREAM)
    raw.settimeout(timeout)
    try:
        raw.connect((address, 443))
        tls = context.wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise
    connection = http.client.HTTPConnection(host, 443, timeout=timeout)
    connection.sock = tls  # already connected; http.client will not resolve the name again
    return connection


def fetch(url: str, allowed_hosts: Iterable[str], *, accept: str = "application/json",
          resolver: Optional[Resolver] = None, max_bytes: int = MAX_BYTES,
          timeout: float = TIMEOUT_SECONDS, method: str = "GET", json_body: Any = None,
          extra_headers: Optional[Dict[str, str]] = None) -> FetchResult:
    """``method="POST"`` with ``json_body`` is for the code-fixed hosts only
    (chain RPC in wallet.py, the GitHub API in submit.py); the Scout's
    operator-configured hosts are only ever read with GET."""
    host, target = check_url(url, allowed_hosts)
    address = resolve_public(host, resolver)
    deadline = time.monotonic() + timeout
    connection = None
    try:
        if method not in ("GET", "POST"):
            raise EgressError("разрешены только GET и POST")
        headers = {"Host": host, "User-Agent": USER_AGENT, "Accept": accept,
                   "Accept-Encoding": "identity", "Connection": "close"}
        body = None
        if method == "POST":
            body = json.dumps(json_body if json_body is not None else {}).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(body))})
        for key, value in (extra_headers or {}).items():
            if key.lower() in ("host", "content-length", "connection"):
                continue
            headers[key] = value
        connection = _connect(host, address, timeout)
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise EgressError(f"источник ответил перенаправлением {response.status} — переходы не выполняются")
        received = bytearray()
        while True:
            if time.monotonic() > deadline:
                raise EgressError("превышено время ответа")
            chunk = response.read(16384)
            if not chunk:
                break
            received.extend(chunk)
            if len(received) > max_bytes:
                raise EgressError(f"ответ больше {max_bytes // 1024} КБ")
        return FetchResult(url=url, status=response.status,
                           content_type=response.getheader("Content-Type", "") or "",
                           body=bytes(received), address=address, fetched_at=store.iso())
    except EgressError:
        raise
    except ssl.SSLError as exc:
        raise EgressError(f"ошибка TLS: {exc.reason or type(exc).__name__}") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise EgressError(f"сетевая ошибка: {type(exc).__name__}") from exc
    finally:
        if connection is not None:
            connection.close()
