"""Asterisk sidecar driver: ARI call control and the RTP media leg.

Asterisk registers to the SIP provider and puts every call into the ``hermes`` Stasis
application (src/asterisk/conf/extensions.conf). This module does all call control over
ARI; the audio of each AI-handled call flows over RTP between Asterisk and this backend
through an ARI ``externalMedia`` channel bridged with the caller:

    caller ──PJSIP── [mixing bridge] ── externalMedia(rtp, ulaw) ──UDP── backend:40000+

Verified against Asterisk 22.5 (Ubuntu 26.04 package) on 2026-09-15:

* the externalMedia response carries ``channelvars.UNICASTRTP_LOCAL_ADDRESS/PORT`` — where
  Asterisk listens for our RTP — and Asterisk sends μ-law (PT 0) from that same port;
* audio flows both ways with strictrtp on.

AudioSocket was tried first and abandoned: this Asterisk version hangs a channel up the
moment a non-blocking header read finds the socket empty (see backend/phone_rtp.py).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from backend import phone_audio, phone_rtp

logger = logging.getLogger("hermes.phone_asterisk")

ARI_APP = "hermes"
MEDIA_TIMEOUT_S = 20.0


class RtpEndpoint(asyncio.DatagramProtocol):
    """One UDP port for one call's media."""

    def __init__(self, on_frame: Callable[[bytes], None]):
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.remote: Optional[Tuple[str, int]] = None
        self.on_frame = on_frame
        self.packets_in = 0
        self.packets_out = 0
        self.last_packet_at = time.monotonic()
        self.max_rms = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        packet = phone_rtp.parse(data)
        if packet is None or packet.payload_type != phone_rtp.PT_PCMU or not packet.payload:
            return
        if self.remote is None:
            self.remote = addr
        self.packets_in += 1
        self.last_packet_at = time.monotonic()
        pcm = phone_audio.ulaw_to_pcm(packet.payload)
        self.max_rms = max(self.max_rms, phone_audio.rms(pcm))
        self.on_frame(pcm)

    def send(self, datagram: bytes) -> None:
        if self.transport is not None and self.remote is not None and not self.transport.is_closing():
            self.transport.sendto(datagram, self.remote)
            self.packets_out += 1

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()


class RtpAudioOut:
    """Paced RTP sender (implements the session's AudioOut): one μ-law packet every
    20 ms, silence when nothing is being said, so Asterisk's RTP stream never stalls.
    A late packet is just a gap on UDP, so there is no lead to keep and a barge-in
    stops the voice within one frame."""

    def __init__(self, endpoint: RtpEndpoint):
        self.endpoint = endpoint
        self.packetizer = phone_rtp.Packetizer()
        self._frames: "collections.deque[bytes]" = collections.deque()
        self._buffer = phone_audio.FrameBuffer()
        self._closed = False

    async def play(self, pcm: bytes) -> None:
        self._frames.extend(self._buffer.push(pcm))

    async def clear(self) -> None:
        self._frames.clear()
        self._buffer = phone_audio.FrameBuffer()

    def pending_ms(self) -> int:
        return len(self._frames) * phone_audio.FRAME_MS

    def close(self) -> None:
        self._closed = True

    def _next_frame(self) -> bytes:
        if self._frames:
            return self._frames.popleft()
        tail = self._buffer.flush()
        return tail if tail is not None else phone_audio.SILENCE_FRAME

    async def run(self) -> None:
        frame_s = phone_audio.FRAME_MS / 1000
        next_at = time.monotonic()
        try:
            while not self._closed:
                self.endpoint.send(self.packetizer.packet(phone_audio.pcm_to_ulaw(self._next_frame())))
                next_at += frame_s
                now = time.monotonic()
                if next_at < now - 0.2:
                    next_at = now  # after a stall, resync instead of bursting stale audio
                await asyncio.sleep(max(0.0, next_at - now))
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("RTP sender stopped")


class PortPool:
    def __init__(self, low: int, high: int):
        self.low, self.high = low, high
        self.in_use: set = set()

    async def open(self, host: str, factory: Callable[[], RtpEndpoint]) -> Tuple[RtpEndpoint, int]:
        loop = asyncio.get_running_loop()
        for port in range(self.low, self.high + 1):
            if port in self.in_use:
                continue
            try:
                _transport, endpoint = await loop.create_datagram_endpoint(factory, local_addr=(host, port))
            except OSError:
                continue
            self.in_use.add(port)
            return endpoint, port
        raise RuntimeError("Нет свободных RTP-портов для звонка")

    def release(self, port: int) -> None:
        self.in_use.discard(port)


@dataclass
class AsteriskCall:
    call_id: str
    line: Dict[str, Any]
    direction: str
    channel_id: str
    remote_number: str = ""
    bridge_id: str = ""
    ext_id: str = ""
    owner_channel_id: str = ""
    selftest: bool = False
    answered: bool = False
    ended: bool = False
    session: Any = None
    endpoint: Optional[RtpEndpoint] = None
    rtp_port: int = 0
    inbound: Optional["asyncio.Queue[bytes]"] = None
    selftest_result: Dict[str, Any] = field(default_factory=dict)


class AsteriskCallControl:
    def __init__(self, controller: "AsteriskController", call: AsteriskCall):
        self.controller = controller
        self.call = call

    async def hangup(self, reason: str = "") -> None:
        await self.controller.hangup_channel(self.call.channel_id)

    async def transfer(self, number: str) -> bool:
        return await self.controller.transfer(self.call, number)


class AsteriskController:
    def __init__(self) -> None:
        self.base_url = (os.getenv("PHONE_ARI_URL") or "http://asterisk:8088/ari").rstrip("/")
        self.rtp_bind_host = os.getenv("PHONE_RTP_BIND_HOST") or "0.0.0.0"
        self.media_ip_override = os.getenv("PHONE_MEDIA_ADVERTISE_IP") or ""
        self.ports = PortPool(int(os.getenv("PHONE_RTP_PORT_MIN") or 40000), int(os.getenv("PHONE_RTP_PORT_MAX") or 40199))
        self.enabled = (os.getenv("PHONE_ASTERISK_ENABLED") or "true").lower() not in ("0", "false", "no", "off")
        self.connected = False
        self.last_error = ""
        self.asterisk_version = ""
        self._stopping = False
        self._events_task: Optional[asyncio.Task] = None
        self._media_ip: str = ""
        self.calls: Dict[str, AsteriskCall] = {}
        self._by_channel: Dict[str, str] = {}
        self._channel_waiters: Dict[str, "asyncio.Future[bool]"] = {}
        self._tasks: set = set()

    # ── plumbing ────────────────────────────────────────────────────────────
    def _secret(self) -> str:
        from backend import phone_lines

        path = phone_lines.asterisk_state_dir() / "ari.secret"
        try:
            return path.read_text().strip()
        except OSError:
            return ""

    async def ari(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                  json_body: Optional[Dict[str, Any]] = None, timeout: float = 10.0) -> Any:
        import httpx

        secret = self._secret()
        if not secret:
            raise RuntimeError("Asterisk ещё не запущен (нет ari.secret)")
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3.0)) as client:
            response = await client.request(method, f"{self.base_url}{path}", params=params, json=json_body,
                                            auth=("hermes", secret))
        if response.status_code >= 400:
            raise RuntimeError(f"ARI {method} {path.split('?')[0]} → {response.status_code}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def media_ip(self) -> str:
        """Our address as Asterisk sees it: the source address of a route towards the
        ARI host. On the compose network that is the backend's IP on hermes-web-net —
        the backend sits on several networks, so a plain hostname lookup of itself
        could hand Asterisk an address it cannot reach."""
        if self.media_ip_override:
            return self.media_ip_override
        if self._media_ip:
            return self._media_ip

        def resolve() -> str:
            host = urlparse(self.base_url).hostname or "127.0.0.1"
            target = socket.gethostbyname(host)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((target, 9))
                return probe.getsockname()[0]

        self._media_ip = await asyncio.to_thread(resolve)
        return self._media_ip

    async def start(self) -> None:
        if not self.enabled:
            return
        self._events_task = asyncio.create_task(self._events_loop())

    async def stop(self) -> None:
        self._stopping = True
        if self._events_task:
            self._events_task.cancel()
        for call in list(self.calls.values()):
            if call.session is not None:
                with contextlib.suppress(Exception):
                    await call.session.on_remote_hangup()
            self._release_media(call)
        for task in list(self._tasks):
            task.cancel()

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled, "connected": self.connected, "last_error": self.last_error,
            "version": self.asterisk_version, "active_calls": len([c for c in self.calls.values() if not c.ended]),
        }

    async def _events_loop(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stopping:
            secret = self._secret()
            if not secret:
                self.connected = False
                self.last_error = "Asterisk ещё не запущен"
                await asyncio.sleep(5)
                continue
            ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
            url = f"{ws_url}/events?app={ARI_APP}&api_key=hermes:{secret}"
            try:
                async with websockets.connect(url, open_timeout=10, ping_interval=20, max_size=2 ** 22) as ws:
                    self.connected = True
                    self.last_error = ""
                    backoff = 1.0
                    self._spawn(self._on_connected())
                    async for raw in ws:
                        try:
                            await self._dispatch(json.loads(raw))
                        except Exception:
                            logger.exception("ARI event handling failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The URL carries the ARI password: log the error type, never the URL.
                if self.connected or not self.last_error:
                    logger.warning("ARI events connection lost: %s", type(exc).__name__)
                self.last_error = f"Нет связи с Asterisk ({type(exc).__name__})"
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(30.0, backoff * 2)

    async def _on_connected(self) -> None:
        try:
            info = await self.ari("GET", "/asterisk/info")
            self.asterisk_version = str(((info or {}).get("system") or {}).get("version") or "")
        except Exception:
            pass
        await self.reload_trunks()

    async def reload_trunks(self) -> None:
        from backend import phone_lines

        await asyncio.to_thread(phone_lines.write_trunks)
        if not self.connected:
            return
        for module in ("res_pjsip.so", "res_pjsip_outbound_registration.so"):
            with contextlib.suppress(Exception):
                await self.ari("PUT", f"/asterisk/modules/{module}")

    async def trunk_state(self, line_id: str) -> Dict[str, Any]:
        from backend import phone_lines

        if not self.connected:
            raise phone_lines.PhoneProviderError(self.last_error or "Нет связи с Asterisk")
        try:
            endpoint = await self.ari("GET", f"/endpoints/PJSIP/{phone_lines.trunk_name(line_id)}")
        except RuntimeError:
            raise phone_lines.PhoneProviderError("Транк не загружен — проверьте логин и пароль SIP")
        state = str((endpoint or {}).get("state") or "unknown")
        if state != "online":
            raise phone_lines.PhoneProviderError(f"SIP-транк не в сети (состояние: {state}) — проверьте логин, пароль и сервер")
        return {"state": state}

    def _wait_channel(self, channel_id: str) -> "asyncio.Future[bool]":
        future = asyncio.get_running_loop().create_future()
        self._channel_waiters[channel_id] = future
        return future

    def _register(self, call: AsteriskCall) -> None:
        self.calls[call.call_id] = call
        self._by_channel[call.channel_id] = call.call_id

    def _release_media(self, call: AsteriskCall) -> None:
        if call.endpoint is not None:
            call.endpoint.close()
            call.endpoint = None
        if call.rtp_port:
            self.ports.release(call.rtp_port)
            call.rtp_port = 0

    # ── events ──────────────────────────────────────────────────────────────
    async def _dispatch(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        channel = event.get("channel") or {}
        channel_id = str(channel.get("id") or "")
        if kind == "StasisStart":
            args = [str(a) for a in event.get("args") or []]
            role = args[0] if args else ""
            waiter = self._channel_waiters.pop(channel_id, None)
            if waiter and not waiter.done():
                waiter.set_result(True)
            if role == "inbound":
                self._spawn(self._handle_inbound(channel, args))
            elif role in ("outbound", "selftest"):
                call = self.calls.get(args[1] if len(args) > 1 else "")
                if call and not call.answered:
                    call.answered = True
                    self._spawn(self._connect_ai(call))
        elif kind in ("StasisEnd", "ChannelDestroyed"):
            waiter = self._channel_waiters.pop(channel_id, None)
            if waiter and not waiter.done():
                waiter.set_result(False)
            call_id = self._by_channel.get(channel_id)
            if call_id:
                call = self.calls.get(call_id)
                if call and channel_id in (call.channel_id, call.owner_channel_id):
                    self._spawn(self._caller_gone(call, event))

    async def _handle_inbound(self, channel: Dict[str, Any], args: list) -> None:
        from backend import phone_lines, phone_service
        from backend.phone_audio import normalize_e164

        channel_id = str(channel.get("id"))
        line_id = phone_lines.line_id_from_channel(str(channel.get("name") or ""))
        line = await asyncio.to_thread(phone_lines.get_line, line_id) if line_id else None
        if not line:
            logger.warning("Inbound call on an unknown trunk — hanging up")
            await self.hangup_channel(channel_id)
            return
        caller = normalize_e164(str((channel.get("caller") or {}).get("number") or ""))
        decision = await asyncio.to_thread(phone_service.inbound_decision, line, caller)
        record = await asyncio.to_thread(
            phone_lines.create_call, line=line, direction="inbound", remote_number=caller, provider_call_id=channel_id,
        )
        call = AsteriskCall(call_id=record["id"], line=line, direction="inbound", channel_id=channel_id, remote_number=caller)
        self._register(call)
        action = decision["action"]
        if action in ("block", "reject", "busy"):
            await asyncio.to_thread(phone_service.record_rejected, record, line, decision)
            await self.hangup_channel(channel_id, reason="busy" if action == "busy" else "rejected")
            call.ended = True
            return
        await self.ari("POST", f"/channels/{channel_id}/answer")
        call.answered = True
        if action == "transfer":
            await asyncio.to_thread(phone_service.note_direct_transfer, record, line, decision)
            bridge = await self.ari("POST", "/bridges", params={"type": "mixing"})
            call.bridge_id = bridge["id"]
            await self.ari("POST", f"/bridges/{call.bridge_id}/addChannel", params={"channel": channel_id})
            if not await self._dial_into_bridge(call, line["owner_number"]):
                await self.hangup_channel(channel_id)
            return
        await self._connect_ai(call)

    async def _connect_ai(self, call: AsteriskCall) -> None:
        try:
            if not call.bridge_id:
                bridge = await self.ari("POST", "/bridges", params={"type": "mixing"})
                call.bridge_id = bridge["id"]
                await self.ari("POST", f"/bridges/{call.bridge_id}/addChannel", params={"channel": call.channel_id})
            queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=500)

            def on_frame(pcm: bytes) -> None:
                if queue.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                queue.put_nowait(pcm)

            call.inbound = queue
            call.endpoint, call.rtp_port = await self.ports.open(self.rtp_bind_host, lambda: RtpEndpoint(on_frame))
            call.ext_id = f"hx-{call.call_id}"
            ip = await self.media_ip()
            media = await self.ari("POST", "/channels/externalMedia", params={
                "channelId": call.ext_id, "app": ARI_APP, "external_host": f"{ip}:{call.rtp_port}",
                "encapsulation": "rtp", "transport": "udp", "format": "ulaw",
            })
            channel_vars = (media or {}).get("channelvars") or {}
            if channel_vars.get("UNICASTRTP_LOCAL_ADDRESS") and channel_vars.get("UNICASTRTP_LOCAL_PORT"):
                call.endpoint.remote = (channel_vars["UNICASTRTP_LOCAL_ADDRESS"], int(channel_vars["UNICASTRTP_LOCAL_PORT"]))
            await self.ari("POST", f"/bridges/{call.bridge_id}/addChannel", params={"channel": call.ext_id})
        except Exception:
            logger.exception("Call %s: could not connect the AI media channel", call.call_id)
            self._release_media(call)
            await self.hangup_channel(call.channel_id)
            return
        if call.selftest:
            self._spawn(self._selftest_media(call))
        else:
            self._spawn(self._run_session(call))

    async def _dial_into_bridge(self, call: AsteriskCall, number: str) -> bool:
        from backend import phone_lines

        owner_channel = f"ho-{call.call_id}-{uuid.uuid4().hex[:4]}"
        waiter = self._wait_channel(owner_channel)
        try:
            await self.ari("POST", "/channels", params={
                "endpoint": f"PJSIP/{number}@{phone_lines.trunk_name(call.line['id'])}", "app": ARI_APP,
                "appArgs": f"transfer,{call.call_id}", "callerId": call.line.get("number") or "",
                "timeout": 30, "channelId": owner_channel,
            })
            answered = await asyncio.wait_for(waiter, timeout=35)
        except Exception:
            self._channel_waiters.pop(owner_channel, None)
            answered = False
        if not answered:
            return False
        call.owner_channel_id = owner_channel
        self._by_channel[owner_channel] = call.call_id
        await self.ari("POST", f"/bridges/{call.bridge_id}/addChannel", params={"channel": owner_channel})
        return True

    async def transfer(self, call: AsteriskCall, number: str) -> bool:
        if not number or not call.bridge_id:
            return False
        if not await self._dial_into_bridge(call, number):
            return False
        ext = call.ext_id
        call.ext_id = ""
        if ext:
            with contextlib.suppress(Exception):
                await self.ari("POST", f"/bridges/{call.bridge_id}/removeChannel", params={"channel": ext})
            await self.hangup_channel(ext)
        return True

    async def _caller_gone(self, call: AsteriskCall, event: Dict[str, Any]) -> None:
        if call.ended:
            return
        call.ended = True
        for channel in (call.ext_id, call.owner_channel_id, call.channel_id):
            if channel:
                await self.hangup_channel(channel)
        if call.bridge_id:
            with contextlib.suppress(Exception):
                await self.ari("DELETE", f"/bridges/{call.bridge_id}")
        if call.session is not None:
            with contextlib.suppress(Exception):
                await call.session.on_remote_hangup()
        elif not call.answered and not call.selftest:
            from backend import phone_service

            cause = str(event.get("cause_txt") or event.get("cause") or "")
            await asyncio.to_thread(phone_service.mark_unanswered, call.call_id, cause)
        elif call.owner_channel_id:
            from backend import phone_lines

            await asyncio.to_thread(phone_lines.update_call, call.call_id, status="ended",
                                    ended_at=phone_lines._now(), end_reason="transferred_call_ended")
        self._release_media(call)
        self.calls.pop(call.call_id, None)
        for key in [k for k, v in self._by_channel.items() if v == call.call_id]:
            self._by_channel.pop(key, None)

    async def hangup_channel(self, channel_id: str, reason: str = "normal") -> None:
        if not channel_id:
            return
        with contextlib.suppress(Exception):
            await self.ari("DELETE", f"/channels/{channel_id}", params={"reason": reason})

    # ── outbound & self-test ────────────────────────────────────────────────
    async def originate(self, record: Dict[str, Any], line: Dict[str, Any], to: str) -> None:
        from backend import phone_lines

        if not self.connected:
            raise phone_lines.PhoneProviderError(self.last_error or "Нет связи с Asterisk")
        channel_id = f"hc-{record['id']}"
        call = AsteriskCall(call_id=record["id"], line=line, direction="outbound", channel_id=channel_id, remote_number=to)
        self._register(call)
        try:
            await self.ari("POST", "/channels", params={
                "endpoint": f"PJSIP/{to}@{phone_lines.trunk_name(line['id'])}", "app": ARI_APP,
                "appArgs": f"outbound,{record['id']}", "callerId": line.get("number") or "",
                "timeout": 45, "channelId": channel_id,
            })
        except Exception as exc:
            self.calls.pop(record["id"], None)
            self._by_channel.pop(channel_id, None)
            raise phone_lines.PhoneProviderError(f"Asterisk не смог начать звонок: {exc}") from exc

    async def selftest(self, seconds: float = 3.0) -> Dict[str, Any]:
        """Plays a built-in prompt through a Local channel into the full ARI + RTP path
        and reports whether audio actually arrived."""
        if not self.connected:
            return {"ok": False, "error": self.last_error or "Нет связи с Asterisk"}
        call_id = f"selftest{uuid.uuid4().hex[:8]}"
        call = AsteriskCall(call_id=call_id, line={"id": "selftest"}, direction="selftest",
                            channel_id=f"hs-{call_id}", selftest=True)
        call.selftest_result = {"seconds": seconds, "done": asyncio.get_running_loop().create_future()}
        self._register(call)
        try:
            await self.ari("POST", "/channels", params={
                "endpoint": "Local/play@hermes-selftest", "app": ARI_APP, "appArgs": f"selftest,{call_id}",
                "channelId": call.channel_id, "timeout": 10,
            })
            result = await asyncio.wait_for(call.selftest_result["done"], timeout=seconds + 10)
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}
        finally:
            await self.hangup_channel(call.channel_id)
        return result

    async def _selftest_media(self, call: AsteriskCall) -> None:
        out = RtpAudioOut(call.endpoint)  # type: ignore[arg-type]
        pacer = asyncio.create_task(out.run())
        await asyncio.sleep(call.selftest_result["seconds"])
        out.close()
        pacer.cancel()
        endpoint = call.endpoint
        packets = endpoint.packets_in if endpoint else 0
        result = {"ok": packets >= 50 and bool(endpoint and endpoint.max_rms > 100), "audio_ms": packets * 20,
                  "max_rms": endpoint.max_rms if endpoint else 0}
        if not call.selftest_result["done"].done():
            call.selftest_result["done"].set_result(result)

    # ── session ─────────────────────────────────────────────────────────────
    async def _run_session(self, call: AsteriskCall) -> None:
        from backend import phone_lines, phone_service

        endpoint, queue = call.endpoint, call.inbound
        if endpoint is None or queue is None:
            return
        out = RtpAudioOut(endpoint)
        pacer = asyncio.create_task(out.run())
        session = None
        runner: Optional[asyncio.Task] = None
        exit_reason = "session_finished"
        try:
            record = await asyncio.to_thread(phone_lines.get_call, call.call_id)
            if record is None:
                exit_reason = "unknown_call"
                return
            session = await phone_service.create_session(record, call.line, out, AsteriskCallControl(self, call))
            call.session = session
            runner = asyncio.create_task(session.run())
            frames = phone_audio.FrameBuffer()
            while True:
                getter = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait({getter, runner}, timeout=5.0, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    if runner in done:
                        break  # the session ended on its own (hang-up, transfer, remote end)
                    if time.monotonic() - endpoint.last_packet_at > MEDIA_TIMEOUT_S:
                        exit_reason = "media_timeout"
                        break
                    continue
                for frame in frames.push(getter.result()):
                    await session.on_audio(frame)
        finally:
            logger.info("Call %s: media leg ended (%s), %d packets in, %d out", call.call_id, exit_reason,
                        endpoint.packets_in, endpoint.packets_out)
            if session is not None:
                await session.on_remote_hangup()
                if runner is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(runner, timeout=10)
            out.close()
            pacer.cancel()
            if exit_reason != "session_finished" and not call.ended and not call.owner_channel_id:
                # The media leg died while the caller is still on the line: without this
                # they would sit in a silent bridge until they gave up.
                await self.hangup_channel(call.channel_id)
            if not call.owner_channel_id:
                self._release_media(call)


controller = AsteriskController()
