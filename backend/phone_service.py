"""Call orchestration: who answers, starting and finishing sessions, provider webhooks
and media streams for Twilio and Telnyx.

The three providers differ only at the edge:

* **Zadarma** — Asterisk (backend/phone_asterisk.py) owns signalling and media.
* **Twilio** — a signed voice webhook returns TwiML that opens a bidirectional Media
  Stream (μ-law 8 kHz over wss) to :func:`handle_twilio_media`.
* **Telnyx** — signed Call Control webhooks; answering (or dialling) with bidirectional
  streaming opens :func:`handle_telnyx_media`.

Everything after that — rules, the inbox conversation, the agent, speech, the call record
— is shared and lives in :func:`inbound_decision`, :func:`create_session` and
:func:`finalize`.

Webhook routes are public by necessity. They authenticate each request themselves
(X-Twilio-Signature / Telnyx Ed25519) and a media stream only opens with the per-call
HMAC token that this backend put into the stream URL itself.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend import phone_audio, phone_lines

logger = logging.getLogger("hermes.phone_service")

ACTIVE: Dict[str, Any] = {}  # call_id -> CallSession
_CONTROLS: Dict[str, Any] = {}  # call_id -> CallControl


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── decisions ───────────────────────────────────────────────────────────────
def capacity_available() -> bool:
    return phone_lines.count_active_calls() < int(phone_lines.get_settings()["max_concurrent_calls"])


def inbound_decision(line: Dict[str, Any], caller: str) -> Dict[str, Any]:
    """ai | transfer | block | reject | busy — with the reason, for the archive."""
    if not line.get("enabled") or not line.get("inbound_enabled"):
        return {"action": "reject", "reason": "line_disabled"}
    if not capacity_available():
        return {"action": "busy", "reason": "capacity"}
    owner = line.get("owner_number") or ""
    if caller:
        from backend import inbox, inbox_rules

        rule = inbox_rules.evaluate(line["id"], inbox.sender_identities("phone", caller))
        if rule and rule.action == "block":
            return {"action": "block", "reason": "rule", "pattern": rule.pattern}
        if rule and rule.action == "pass":
            return {"action": "transfer" if owner else "ai", "reason": "rule_pass", "pattern": rule.pattern}
        try:
            from backend import conversation_state

            state = conversation_state.get_state(line["id"], "phone", caller)
        except Exception:
            state = None
        if state and state.get("handling_mode") in ("owner_takeover", "waiting") and owner:
            return {"action": "transfer", "reason": "taken_over"}
    if not line.get("subagent_id"):
        return {"action": "transfer", "reason": "no_agent"} if owner else {"action": "reject", "reason": "no_agent"}
    return {"action": "ai", "reason": ""}


def _conversation_for(call: Dict[str, Any], line: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from backend import inbox

    remote = call.get("remote_number") or ""
    chat_id = remote or f"hidden-{call['id']}"
    return inbox.ensure_conversation(
        binding_id=line["id"], platform="phone", chat_id=chat_id, session_id=f"phone:{line['id']}:{chat_id}",
        display_name=remote or "Скрытый номер", external_user_id=remote, subagent_id=line.get("subagent_id") or "",
    )


_DECISION_LABELS = {
    "block": "заблокирован правилом", "reject": "линия не принимает звонки", "busy": "все линии заняты",
    "rule_pass": "по правилу — сразу владельцу", "taken_over": "диалог забран владельцем", "no_agent": "агент не назначен",
}


def record_rejected(call: Dict[str, Any], line: Dict[str, Any], decision: Dict[str, Any]) -> None:
    from backend import inbox

    conversation = _conversation_for(call, line)
    phone_lines.update_call(call["id"], status="rejected", ended_at=_now(), end_reason=decision.get("reason") or decision["action"],
                            conversation_id=(conversation or {}).get("id"))
    if conversation:
        label = _DECISION_LABELS.get(decision["action"]) or _DECISION_LABELS.get(decision.get("reason", ""), decision["action"])
        inbox.record_agent_reply(conversation["id"], f"📵 Звонок от {call.get('remote_number') or 'скрытого номера'} не принят: {label}",
                                 speaker="system", unread=decision["action"] != "block",
                                 meta={"event": "call_rejected", "call_id": call["id"]})


def note_direct_transfer(call: Dict[str, Any], line: Dict[str, Any], decision: Dict[str, Any]) -> None:
    from backend import inbox

    conversation = _conversation_for(call, line)
    phone_lines.update_call(call["id"], status="active", answered_at=_now(), transferred_to=line.get("owner_number") or "",
                            end_reason="direct_transfer", conversation_id=(conversation or {}).get("id"))
    if conversation:
        label = _DECISION_LABELS.get(decision.get("reason", ""), "")
        inbox.record_agent_reply(conversation["id"], f"↪ Звонок от {call.get('remote_number') or 'скрытого номера'} "
                                 f"сразу переведён на владельца ({label})", speaker="system", unread=True,
                                 meta={"event": "direct_transfer", "call_id": call["id"]})


def mark_unanswered(call_id: str, cause: str) -> None:
    from backend import inbox

    call = phone_lines.get_call(call_id)
    if not call or call["status"] not in ("ringing",):
        return
    reason = "busy" if "busy" in cause.lower() else "no_answer"
    phone_lines.update_call(call_id, status="failed", ended_at=_now(), end_reason=reason)
    line = phone_lines.get_line(call["line_id"])
    if line:
        conversation = _conversation_for(call, line)
        if conversation:
            inbox.record_agent_reply(conversation["id"],
                                     f"☎ Исходящий звонок на {call['remote_number']} не состоялся: "
                                     f"{'занято' if reason == 'busy' else 'не ответили'}",
                                     speaker="system", meta={"event": "call_failed", "call_id": call_id})


# ── sessions ────────────────────────────────────────────────────────────────
async def create_session(call: Dict[str, Any], line: Dict[str, Any], audio_out: Any, control: Any):
    from backend import inbox_rules, phone_speech
    from backend.phone_context import CallContext
    from backend.phone_session import CallSession

    conversation = await asyncio.to_thread(_conversation_for, call, line) or {}
    settings = await asyncio.to_thread(inbox_rules.get_binding_settings, line["id"])
    remote = call.get("remote_number") or ""
    chat_id = remote or f"hidden-{call['id']}"
    ctx = CallContext(
        call_id=call["id"], line_id=line["id"], conversation_id=conversation.get("id") or "",
        session_id=f"phone:{line['id']}:{chat_id}", direction=call["direction"], remote_number=remote,
        local_number=line.get("number") or "", language=line.get("language") or "auto", purpose=call.get("purpose") or "",
        client_id=conversation.get("client_id") or call.get("client_id"), client_name=conversation.get("client_name") or "",
        owner_number=line.get("owner_number") or "", auto_book=bool(line.get("auto_book")),
        business_hours=settings.get("business_hours") or "", timezone=settings.get("timezone") or "Asia/Jerusalem",
        line_name=line.get("name") or "",
    )
    try:
        stt = await asyncio.to_thread(phone_speech.build_stt)
    except phone_speech.SpeechError:
        stt = phone_speech.LocalWhisperSTT()
    tts = await asyncio.to_thread(phone_speech.build_tts)
    started = time.monotonic()
    await asyncio.to_thread(phone_lines.update_call, call["id"], status="active", answered_at=_now(),
                            conversation_id=ctx.conversation_id or None, client_id=ctx.client_id)

    async def on_finished(result) -> None:
        await asyncio.to_thread(finalize, call["id"], result, int(time.monotonic() - started))

    session = CallSession(
        ctx=ctx, agent_id=line.get("subagent_id") or "", audio_out=audio_out, control=control, stt=stt, tts=tts,
        greeting=(line.get("greeting") or "") if call["direction"] == "inbound" else "",
        on_finished=on_finished, max_call_s=float(line.get("max_call_minutes") or 20) * 60,
    )
    ACTIVE[call["id"]] = session
    _CONTROLS[call["id"]] = control
    return session


def finalize(call_id: str, result: Any, duration_s: int) -> None:
    ACTIVE.pop(call_id, None)
    _CONTROLS.pop(call_id, None)
    latencies = sorted(result.latencies_ms or [])
    avg = int(sum(latencies) / len(latencies)) if latencies else None
    p90 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))] if latencies else None
    phone_lines.update_call(
        call_id, status="ended", ended_at=_now(), duration_s=duration_s, end_reason=result.end_reason,
        turns=result.turns, interruptions=result.interruptions, first_audio_ms_avg=avg, first_audio_ms_p90=p90,
        transferred_to=result.transferred_to, degraded=result.degraded,
    )
    logger.info("Call %s finished: %s, %ds, %d turns, first audio avg=%s p90=%s ms", call_id, result.end_reason,
                duration_s, result.turns, avg, p90)


# ── outbound & live control ─────────────────────────────────────────────────
async def start_outbound(line_id: str, to: str, *, purpose: str = "", client_id: Optional[str] = None,
                         initiated_by: str = "") -> Dict[str, Any]:
    from backend import inbox, inbox_rules

    line = await asyncio.to_thread(phone_lines.get_line, line_id)
    if line is None:
        raise KeyError(line_id)
    if not line["enabled"] or not line["outbound_enabled"]:
        raise ValueError("Исходящие звонки на этой линии выключены")
    if not line["subagent_id"]:
        raise ValueError("На линии не назначен агент")
    number = phone_audio.normalize_e164(to)
    if not phone_lines._E164.match(number or ""):
        raise ValueError("Номер в международном формате, например +972501234567")
    if number == line.get("number"):
        raise ValueError("Нельзя позвонить на номер самой линии")
    rule = await asyncio.to_thread(inbox_rules.evaluate, line["id"], inbox.sender_identities("phone", number))
    if rule and rule.action == "block":
        raise ValueError("Этот номер заблокирован правилом — звонок не выполнен")
    if not await asyncio.to_thread(capacity_available):
        raise ValueError("Достигнут лимит одновременных звонков")
    purpose = (purpose or "").strip()[:500]
    record = await asyncio.to_thread(phone_lines.create_call, line=line, direction="outbound", remote_number=number,
                                     purpose=purpose, client_id=client_id, initiated_by=initiated_by)
    try:
        if line["provider"] == "zadarma":
            from backend.phone_asterisk import controller

            await controller.originate(record, line, number)
        elif line["provider"] == "twilio":
            urls = phone_lines.provider_urls(line, record["id"])
            sid = await phone_lines.twilio_client(line).create_call(
                number, line["number"], phone_lines.twiml_stream(urls["twilio_media"], record["id"]), urls["twilio_status"])
            await asyncio.to_thread(phone_lines.update_call, record["id"], provider_call_id=sid)
        elif line["provider"] == "telnyx":
            urls = phone_lines.provider_urls(line, record["id"])
            ccid = await phone_lines.telnyx_client(line).create_call(
                connection_id=line["config"].get("connection_id") or "", to=number, from_=line["number"],
                webhook_url=urls["telnyx_events"], media_url=urls["telnyx_media"], client_state=record["id"])
            await asyncio.to_thread(phone_lines.update_call, record["id"], provider_call_id=ccid)
    except Exception as exc:
        await asyncio.to_thread(phone_lines.update_call, record["id"], status="failed", ended_at=_now(),
                                end_reason=str(exc)[:200])
        raise
    conversation = await asyncio.to_thread(_conversation_for, record, line)
    if conversation:
        await asyncio.to_thread(
            inbox.record_agent_reply, conversation["id"],
            f"☎ Исходящий звонок на {number}" + (f" — {purpose}" if purpose else ""), speaker="system",
            meta={"event": "call_started_outbound", "call_id": record["id"]},
        )
        await asyncio.to_thread(phone_lines.update_call, record["id"], conversation_id=conversation["id"])
    return await asyncio.to_thread(phone_lines.get_call, record["id"])


def start_outbound_from_tool(arguments: Dict[str, Any]) -> str:
    """Body of the owner-only ``phone_call`` tool. Tools run in a worker thread; the
    call is placed on the main loop (the one the inbox bound at startup), because the
    Asterisk driver and provider clients live there."""
    from backend import inbox

    args = arguments if isinstance(arguments, dict) else {}
    loop = inbox._event_loop
    if loop is None or loop.is_closed():
        return json.dumps({"status": "unavailable", "message": "Телефония не запущена."}, ensure_ascii=False)
    wanted = str(args.get("line") or "").strip().lower()
    lines = [line for line in phone_lines.list_lines() if line["enabled"] and line["outbound_enabled"] and line["subagent_id"]]
    if wanted:
        lines = [line for line in lines if wanted in (line["id"].lower(), line["name"].lower())] or lines
    if not lines:
        return json.dumps({"status": "unavailable",
                           "message": "Нет телефонной линии с разрешёнными исходящими звонками — настройте во «Входящих» → «Телефония»."},
                          ensure_ascii=False)
    future = asyncio.run_coroutine_threadsafe(
        start_outbound(lines[0]["id"], str(args.get("to") or ""), purpose=str(args.get("purpose") or ""), initiated_by="vexa"),
        loop,
    )
    try:
        call = future.result(timeout=40)
    except Exception as exc:
        return json.dumps({"status": "error", "message": f"Звонок не начат: {exc}"}, ensure_ascii=False)
    return json.dumps({
        "status": "calling", "call_id": call["id"], "line": lines[0]["name"], "to": call["remote_number"],
        "message": "Звонок начат. Разговор и итог появятся во «Входящих».",
    }, ensure_ascii=False)


async def hangup_call(call_id: str) -> bool:
    session = ACTIVE.get(call_id)
    control = _CONTROLS.get(call_id)
    if session is None or control is None:
        return False
    await control.hangup("operator")
    await session.on_remote_hangup()
    return True


async def transfer_call(call_id: str, number: str = "") -> bool:
    session = ACTIVE.get(call_id)
    if session is None:
        return False
    target = phone_audio.normalize_e164(number) if number else session.ctx.owner_number
    if not target:
        raise ValueError("Не указан номер для перевода")
    session.ctx.actions["transfer"] = {"to": target, "reason": "operator"}
    await session._barge_in()
    await session._apply_actions()
    return session.result.transferred_to == target


# ── Twilio ──────────────────────────────────────────────────────────────────
class TwilioAudioOut:
    def __init__(self, ws: Any):
        self.ws = ws
        self.stream_sid = ""
        self._playhead = time.monotonic()

    async def play(self, pcm: bytes) -> None:
        if not self.stream_sid or not pcm:
            return
        payload = base64.b64encode(phone_audio.pcm_to_ulaw(pcm)).decode()
        await self.ws.send_text(json.dumps({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}}))
        self._playhead = max(self._playhead, time.monotonic()) + len(pcm) / 16000

    async def clear(self) -> None:
        if self.stream_sid:
            await self.ws.send_text(json.dumps({"event": "clear", "streamSid": self.stream_sid}))
        self._playhead = time.monotonic()

    def pending_ms(self) -> int:
        return max(0, int((self._playhead - time.monotonic()) * 1000))


class TwilioControl:
    def __init__(self, line: Dict[str, Any], call_id: str):
        self.line = line
        self.call_id = call_id

    def _sid(self) -> str:
        return (phone_lines.get_call(self.call_id) or {}).get("provider_call_id") or ""

    async def hangup(self, reason: str = "") -> None:
        with contextlib.suppress(Exception):
            await phone_lines.twilio_client(self.line).update_call(await asyncio.to_thread(self._sid), status="completed")

    async def transfer(self, number: str) -> bool:
        try:
            await phone_lines.twilio_client(self.line).update_call(
                await asyncio.to_thread(self._sid), twiml=phone_lines.twiml_dial(number, self.line.get("number") or ""))
            return True
        except Exception as exc:
            logger.warning("Twilio transfer failed: %s", exc)
            return False


def twilio_voice_webhook(line_id: str, url: str, params: Dict[str, str], signature: str) -> Tuple[int, str]:
    line = phone_lines.get_line(line_id)
    if not line or line["provider"] != "twilio":
        return 404, ""
    if not phone_lines.validate_twilio_signature(phone_lines.line_secrets(line_id).get("auth_token") or "", url, params, signature):
        return 403, ""
    call_sid = params.get("CallSid") or ""
    existing = phone_lines.find_call_by_provider("twilio", call_sid)
    if existing and existing["direction"] == "outbound":
        # An outbound call's TwiML is sent inline; nothing to do here.
        return 200, '<?xml version="1.0" encoding="UTF-8"?><Response/>'
    caller = phone_audio.normalize_e164(params.get("From") or "")
    decision = inbound_decision(line, caller)
    record = existing or phone_lines.create_call(line=line, direction="inbound", remote_number=caller, provider_call_id=call_sid)
    if decision["action"] in ("block", "reject"):
        record_rejected(record, line, decision)
        return 200, phone_lines.twiml_reject()
    if decision["action"] == "busy":
        record_rejected(record, line, decision)
        return 200, '<?xml version="1.0" encoding="UTF-8"?><Response><Reject reason="busy"/></Response>'
    if decision["action"] == "transfer":
        note_direct_transfer(record, line, decision)
        return 200, phone_lines.twiml_dial(line["owner_number"], line.get("number") or "")
    urls = phone_lines.provider_urls(line, record["id"])
    return 200, phone_lines.twiml_stream(urls["twilio_media"], record["id"])


def twilio_status_webhook(line_id: str, url: str, params: Dict[str, str], signature: str) -> int:
    line = phone_lines.get_line(line_id)
    if not line or line["provider"] != "twilio":
        return 404
    if not phone_lines.validate_twilio_signature(phone_lines.line_secrets(line_id).get("auth_token") or "", url, params, signature):
        return 403
    call = phone_lines.find_call_by_provider("twilio", params.get("CallSid") or "")
    status = params.get("CallStatus") or ""
    if call and call["id"] not in ACTIVE and call["status"] in ("ringing", "active"):
        if status in ("busy", "no-answer", "failed", "canceled"):
            mark_unanswered(call["id"], "busy" if status == "busy" else status)
        elif status == "completed":
            phone_lines.update_call(call["id"], status="ended", ended_at=_now(), end_reason=call["end_reason"] or "completed")
    return 204


async def _media_loop(ws: Any, line: Dict[str, Any], call: Dict[str, Any], out: Any, control: Any,
                      decode, *, on_start=None) -> None:
    session = await create_session(call, line, out, control)
    runner = asyncio.create_task(session.run())
    frames = phone_audio.FrameBuffer()
    try:
        while not runner.done():
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            message = json.loads(raw)
            event = message.get("event")
            if event == "start" and on_start:
                await on_start(message)
            elif event == "media":
                media = message.get("media") or {}
                if media.get("track") not in (None, "inbound", "inbound_track"):
                    continue
                try:
                    pcm = decode(base64.b64decode(media.get("payload") or ""))
                except (ValueError, TypeError):
                    continue
                for frame in frames.push(pcm):
                    await session.on_audio(frame)
            elif event == "stop":
                break
    except Exception as exc:
        if type(exc).__name__ not in ("WebSocketDisconnect", "ConnectionClosed", "ConnectionClosedOK"):
            logger.warning("Media stream for call %s ended: %s", call["id"], type(exc).__name__)
    finally:
        await session.on_remote_hangup()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(runner, timeout=10)


async def _authorize_media(ws: Any, provider: str, line_id: str, call_id: str, token: str):
    if not phone_lines.check_media_token(call_id, token):
        await ws.close(code=1008)
        return None, None
    call = await asyncio.to_thread(phone_lines.get_call, call_id)
    line = await asyncio.to_thread(phone_lines.get_line, line_id)
    if not call or not line or call["line_id"] != line_id or line["provider"] != provider or call["status"] not in ("ringing", "active"):
        await ws.close(code=1008)
        return None, None
    await ws.accept()
    return call, line


async def handle_twilio_media(ws: Any, line_id: str, call_id: str, token: str) -> None:
    call, line = await _authorize_media(ws, "twilio", line_id, call_id, token)
    if call is None:
        return
    out = TwilioAudioOut(ws)

    # Twilio sends "connected" then "start" before any media; the stream sid is
    # needed to speak, so wait for it before the session can greet the caller.
    for _ in range(20):
        message = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=10))
        if message.get("event") == "start":
            start = message.get("start") or {}
            out.stream_sid = start.get("streamSid") or message.get("streamSid") or ""
            if start.get("callSid") and not call.get("provider_call_id"):
                await asyncio.to_thread(phone_lines.update_call, call_id, provider_call_id=start["callSid"])
            break
    await _media_loop(ws, line, call, out, TwilioControl(line, call_id), phone_audio.ulaw_to_pcm)


# ── Telnyx ──────────────────────────────────────────────────────────────────
class TelnyxAudioOut:
    """Batches outgoing audio: Telnyx documents bidirectional media as chunks of
    20 ms–30 s with at most about one submission per second. The first chunk of a
    phrase goes out immediately — that is the latency the caller hears."""

    def __init__(self, ws: Any, min_interval_s: float = 1.0):
        self.ws = ws
        self.min_interval_s = min_interval_s
        self._pending = bytearray()
        self._last_send = 0.0
        self._playhead = time.monotonic()
        self._lock = asyncio.Lock()
        self._flusher: Optional[asyncio.Task] = None

    async def play(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._pending.extend(phone_audio.pcm_to_ulaw(pcm))
        self._playhead = max(self._playhead, time.monotonic()) + len(pcm) / 16000
        if time.monotonic() - self._last_send >= self.min_interval_s:
            await self._flush()
        elif self._flusher is None or self._flusher.done():
            self._flusher = asyncio.create_task(self._flush_later())

    async def _flush_later(self) -> None:
        await asyncio.sleep(max(0.0, self.min_interval_s - (time.monotonic() - self._last_send)))
        await self._flush()

    async def _flush(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            chunk, self._pending = bytes(self._pending), bytearray()
            self._last_send = time.monotonic()
            await self.ws.send_text(json.dumps({"event": "media", "media": {"payload": base64.b64encode(chunk).decode()}}))

    async def clear(self) -> None:
        self._pending = bytearray()
        if self._flusher:
            self._flusher.cancel()
        await self.ws.send_text(json.dumps({"event": "clear"}))
        self._playhead = time.monotonic()

    def pending_ms(self) -> int:
        return max(0, int((self._playhead - time.monotonic()) * 1000))


class TelnyxControl:
    def __init__(self, line: Dict[str, Any], call_id: str):
        self.line = line
        self.call_id = call_id

    def _ccid(self) -> str:
        return (phone_lines.get_call(self.call_id) or {}).get("provider_call_id") or ""

    async def hangup(self, reason: str = "") -> None:
        with contextlib.suppress(Exception):
            await phone_lines.telnyx_client(self.line).hangup(await asyncio.to_thread(self._ccid))

    async def transfer(self, number: str) -> bool:
        try:
            await phone_lines.telnyx_client(self.line).transfer(await asyncio.to_thread(self._ccid), number)
            return True
        except Exception as exc:
            logger.warning("Telnyx transfer failed: %s", exc)
            return False


async def telnyx_event_webhook(line_id: str, raw_body: bytes, headers: Dict[str, str]) -> int:
    line = await asyncio.to_thread(phone_lines.get_line, line_id)
    if not line or line["provider"] != "telnyx":
        return 404
    lowered = {k.lower(): v for k, v in headers.items()}
    if not phone_lines.verify_telnyx_signature(line["config"].get("public_key") or "", lowered.get("telnyx-timestamp", ""),
                                               raw_body, lowered.get("telnyx-signature-ed25519", "")):
        return 403
    try:
        data = (json.loads(raw_body.decode()) or {}).get("data") or {}
    except ValueError:
        return 400
    event_type = str(data.get("event_type") or "")
    payload = data.get("payload") or {}
    ccid = str(payload.get("call_control_id") or "")
    client = phone_lines.telnyx_client(line)
    if event_type == "call.initiated" and payload.get("direction") == "incoming":
        if await asyncio.to_thread(phone_lines.find_call_by_provider, "telnyx", ccid):
            return 200  # a retried delivery
        caller = phone_audio.normalize_e164(str(payload.get("from") or ""))
        decision = await asyncio.to_thread(inbound_decision, line, caller)
        record = await asyncio.to_thread(phone_lines.create_call, line=line, direction="inbound",
                                         remote_number=caller, provider_call_id=ccid)
        try:
            if decision["action"] in ("block", "reject", "busy"):
                await asyncio.to_thread(record_rejected, record, line, decision)
                await client.reject(ccid)
            elif decision["action"] == "transfer":
                await asyncio.to_thread(note_direct_transfer, record, line, decision)
                await client.transfer(ccid, line["owner_number"])
            else:
                urls = phone_lines.provider_urls(line, record["id"])
                await client.answer(ccid, urls["telnyx_media"], record["id"])
        except phone_lines.PhoneProviderError as exc:
            logger.warning("Telnyx command failed for call %s: %s", record["id"], exc)
        return 200
    if event_type == "call.hangup":
        call = await asyncio.to_thread(phone_lines.find_call_by_provider, "telnyx", ccid)
        if call and call["id"] in ACTIVE:
            await ACTIVE[call["id"]].on_remote_hangup()
        elif call and call["status"] == "ringing":
            await asyncio.to_thread(mark_unanswered, call["id"], str(payload.get("hangup_cause") or ""))
        return 200
    return 200


async def handle_telnyx_media(ws: Any, line_id: str, call_id: str, token: str) -> None:
    call, line = await _authorize_media(ws, "telnyx", line_id, call_id, token)
    if call is None:
        return
    await _media_loop(ws, line, call, TelnyxAudioOut(ws), TelnyxControl(line, call_id), phone_audio.ulaw_to_pcm)


def status() -> Dict[str, Any]:
    from backend.phone_asterisk import controller

    return {
        "asterisk": controller.status(),
        "active_calls": [
            {"call_id": call_id, "remote_number": session.ctx.remote_number, "direction": session.ctx.direction,
             "line_id": session.ctx.line_id, "conversation_id": session.ctx.conversation_id}
            for call_id, session in list(ACTIVE.items())
        ],
        "settings": phone_lines.get_settings(),
    }
