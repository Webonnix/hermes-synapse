"""One live phone conversation: listen, think, speak — and stop the instant the caller
cuts in.

Transport-agnostic. Asterisk, Twilio and Telnyx each hand the session the same three
things: inbound 8 kHz PCM frames (:meth:`CallSession.on_audio`), an :class:`AudioOut` to
speak into, and a :class:`CallControl` to hang up or transfer.

The turn, end to end
────────────────────
1. The speech service ends the caller's turn (``final``). That moment is ``t0``.
2. The agent answers through the ordinary sub-agent turn with reasoning off
   (:mod:`backend.phone_context`), streaming tokens.
3. Tokens are cut into phrases (:class:`backend.phone_audio.PhraseChunker`); each phrase
   is synthesised as a stream and played while the model is still writing the next.
4. ``t0 → first audio byte`` is measured on every turn — that number is the call's
   quality, and it is stored on the call record.

Barge-in is two acts and both happen here: the running turn is cancelled (no more
tokens, no more synthesis — and no more billing) and the transport's queued audio is
cleared. What the caller actually heard is what gets stored; a line nobody heard would
make the transcript lie.

Silence, overlong calls and a turn the model is slow to start are handled too: a short
filler covers a slow tool call, and a caller who went quiet is asked once whether they
are still there before the call is closed politely.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from backend import phone_audio
from backend.phone_context import CURRENT_CALL, CallContext
from backend.phone_speech import STTStream, TTSEngine, detect_language

logger = logging.getLogger("hermes.phone_session")

FILLERS = {"ru": "Секунду.", "en": "One moment."}
STILL_THERE = {"ru": "Вы меня слышите?", "en": "Are you still there?"}
GOODBYE_SILENCE = {"ru": "Не слышу вас. Перезвоните, когда будет удобно. До свидания!",
                   "en": "I can't hear you. Please call back any time. Goodbye!"}
TOO_LONG = {"ru": "К сожалению, время разговора истекло. Спасибо за звонок, до свидания!",
            "en": "I'm sorry, we've reached the time limit for this call. Thank you, goodbye!"}
TRANSFER_FAILED = {"ru": "Не получилось соединить. Могу записать ваше сообщение — что передать?",
                   "en": "I couldn't connect you. I can take a message — what should I pass on?"}
TECH_TROUBLE = {"ru": "Извините, у нас технические неполадки. Пожалуйста, перезвоните позже.",
                "en": "Sorry, we are having technical difficulties. Please call back later."}


class AudioOut(Protocol):
    async def play(self, pcm: bytes) -> None: ...
    async def clear(self) -> None: ...
    def pending_ms(self) -> int: ...


class CallControl(Protocol):
    async def hangup(self, reason: str = "") -> None: ...
    async def transfer(self, number: str) -> bool: ...


AgentTurn = Callable[["CallSession", str, Callable[[str], Awaitable[None]]], Awaitable[str]]


@dataclass
class TurnStats:
    first_audio_ms: Optional[int] = None
    interrupted: bool = False


@dataclass
class SessionResult:
    end_reason: str = ""
    turns: int = 0
    latencies_ms: List[int] = field(default_factory=list)
    interruptions: int = 0
    transferred_to: str = ""
    degraded: List[str] = field(default_factory=list)


async def default_agent_turn(session: "CallSession", text: str, on_token: Callable[[str], Awaitable[None]]) -> str:
    """The line's agent answers through the ordinary sub-agent turn."""
    from backend.agent import agent_instance
    from backend.database import get_subagent

    subagent = await asyncio.to_thread(get_subagent, session.agent_id)
    if not subagent or not subagent.get("is_enabled"):
        return TECH_TROUBLE["ru" if session.language_hint() == "ru" else "en"]
    effective = dict(subagent)
    # A caller is a stranger to the tool policy, same as a token holder on a public
    # channel: read-only informational tools plus the call-scoped phone tools.
    effective["_access_scope"] = "public"
    effective["_plan_allowed_tools"] = None

    async def callback(event: Dict[str, Any]) -> None:
        content = event.get("content") if isinstance(event, dict) else None
        if content:
            await on_token(str(content))

    token = CURRENT_CALL.set(session.ctx)
    try:
        return await agent_instance._respond_as_subagent(
            text, effective, chat_id=session.ctx.session_id, stream_callback=callback,
        )
    finally:
        CURRENT_CALL.reset(token)


class CallSession:
    def __init__(
        self,
        *,
        ctx: CallContext,
        agent_id: str,
        audio_out: AudioOut,
        control: CallControl,
        stt: STTStream,
        tts: TTSEngine,
        greeting: str = "",
        agent_turn: Optional[AgentTurn] = None,
        on_finished: Optional[Callable[[SessionResult], Awaitable[None]]] = None,
        barge_in_min_chars: int = 4,
        filler_after_s: float = 1.6,
        silence_prompt_s: float = 12.0,
        silence_hangup_s: float = 22.0,
        max_call_s: float = 1800.0,
    ):
        self.ctx = ctx
        self.agent_id = agent_id
        self.audio_out = audio_out
        self.control = control
        self.stt = stt
        self.tts = tts
        self.greeting = greeting
        self.agent_turn = agent_turn or default_agent_turn
        self.on_finished = on_finished
        self.barge_in_min_chars = barge_in_min_chars
        self.filler_after_s = filler_after_s
        self.silence_prompt_s = silence_prompt_s
        self.silence_hangup_s = silence_hangup_s
        self.max_call_s = max_call_s

        self.result = SessionResult()
        self._turn_task: Optional[asyncio.Task] = None
        self._tasks: List[asyncio.Task] = []
        self._ended = asyncio.Event()
        self._started_at = time.monotonic()
        self._last_activity = time.monotonic()
        self._prompted_silence = False
        self._last_caller_text = ""
        self._finishing = False
        # Archive writes run on one dedicated worker: off the event loop, because a
        # SQLite write stalling the loop starves the paced audio sender (see
        # phone_asterisk.RtpAudioOut), and single-threaded so lines keep their order.
        self._archive = ThreadPoolExecutor(max_workers=1, thread_name_prefix="call-archive")

    # ── lifecycle ───────────────────────────────────────────────────────────
    def language_hint(self) -> str:
        if self.ctx.language in ("ru", "en"):
            return self.ctx.language
        return detect_language(self._last_caller_text) if self._last_caller_text else "ru"

    async def run(self) -> SessionResult:
        try:
            await self.stt.start()
        except Exception as exc:
            logger.error("Call %s: speech recognition unavailable (%s)", self.ctx.call_id, type(exc).__name__)
            self.result.degraded.append("stt_unavailable")
            await self._speak_and_wait(TECH_TROUBLE[self.language_hint()])
            await self._end("stt_unavailable", hangup=True)
            return self.result
        if self._ended.is_set():
            # The line dropped while recognition was still connecting.
            return self.result
        self._tasks.append(asyncio.create_task(self._read_stt()))
        self._tasks.append(asyncio.create_task(self._watchdog()))
        self._note(f"📞 {'Входящий' if self.ctx.direction == 'inbound' else 'Исходящий'} звонок "
                   f"{'от' if self.ctx.direction == 'inbound' else 'на'} {self.ctx.remote_number or 'скрытый номер'}",
                   {"event": "call_started", "call_id": self.ctx.call_id})
        if self.greeting:
            self._start_turn(None, scripted=self.greeting)
        elif self.ctx.direction == "outbound":
            # An outbound call opens with the agent introducing itself and the reason.
            self._start_turn("[Собеседник снял трубку. Поздоровайся, представься и скажи, зачем звонишь.]", record_caller=False)
        await self._ended.wait()
        return self.result

    async def on_audio(self, frame: bytes) -> None:
        if self._ended.is_set():
            return
        try:
            await self.stt.send(frame)
        except Exception as exc:
            logger.warning("Call %s: STT send failed (%s)", self.ctx.call_id, type(exc).__name__)

    async def on_remote_hangup(self) -> None:
        await self._end("remote_hangup", hangup=False)

    async def _end(self, reason: str, *, hangup: bool) -> None:
        if self._ended.is_set():
            return
        self.result.end_reason = self.result.end_reason or reason
        if self._turn_task and not self._turn_task.done():
            self._turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._turn_task
        for task in self._tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await self.stt.close()
        if hangup:
            with contextlib.suppress(Exception):
                await self.control.hangup(reason)
        self._note(f"Звонок завершён ({_REASON_LABELS.get(self.result.end_reason, self.result.end_reason)}), "
                   f"{int(time.monotonic() - self._started_at)} с", {"event": "call_ended", "call_id": self.ctx.call_id})
        self._ended.set()
        # Everything the call archived is on disk before anyone reads the result.
        with contextlib.suppress(Exception):
            await asyncio.get_running_loop().run_in_executor(self._archive, lambda: None)
        self._archive.shutdown(wait=False)
        if self.on_finished:
            with contextlib.suppress(Exception):
                await self.on_finished(self.result)

    # ── listening ───────────────────────────────────────────────────────────
    def _agent_busy(self) -> bool:
        return bool(self._turn_task and not self._turn_task.done()) or self.audio_out.pending_ms() > 120

    async def _read_stt(self) -> None:
        while not self._ended.is_set():
            event = await self.stt.queue.get()
            if event.kind == "degraded":
                self.result.degraded.append(f"stt:{event.text}")
                continue
            if event.kind == "error":
                continue
            if event.kind in ("speech_started", "partial"):
                self._last_activity = time.monotonic()
                self._prompted_silence = False
                enough = event.kind == "speech_started" and not getattr(self.stt, "has_partials", True)
                if event.kind == "partial" and len(event.text.strip()) >= self.barge_in_min_chars:
                    enough = True
                if enough and self._agent_busy() and not self._finishing:
                    await self._barge_in()
                continue
            if event.kind == "final":
                text = event.text.strip()
                if not text:
                    continue
                self._last_activity = time.monotonic()
                self._prompted_silence = False
                self._last_caller_text = text
                if self._finishing:
                    continue
                if self._agent_busy():
                    await self._barge_in()
                self._start_turn(text, t0=event.at or time.monotonic())

    async def _barge_in(self) -> None:
        self.result.interruptions += 1
        task = self._turn_task
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self.audio_out.clear()

    # ── speaking ────────────────────────────────────────────────────────────
    def _start_turn(self, text: Optional[str], *, t0: Optional[float] = None, scripted: str = "",
                    record_caller: bool = True) -> None:
        if self._ended.is_set():
            return
        self._turn_task = asyncio.create_task(self._turn(text, t0 or time.monotonic(), scripted, record_caller))

    async def _turn(self, text: Optional[str], t0: float, scripted: str, record_caller: bool) -> None:
        stats = TurnStats()
        spoken: List[str] = []
        queued_ms: List[int] = []  # cumulative audio end per phrase
        total_ms = 0
        phrases: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
        chunker = phone_audio.PhraseChunker()
        streamed_any = False
        if text is not None and record_caller:
            self._record("customer", text)

        async def speak_phrases() -> None:
            nonlocal total_ms
            while True:
                phrase = await phrases.get()
                if phrase is None:
                    return
                async for chunk in self.tts.synthesize(phrase):
                    if stats.first_audio_ms is None:
                        stats.first_audio_ms = int((time.monotonic() - t0) * 1000)
                    await self.audio_out.play(chunk)
                    total_ms += phone_audio.split_pcm_duration_ms(chunk)
                spoken.append(phrase)
                queued_ms.append(total_ms)

        async def on_token(token: str) -> None:
            nonlocal streamed_any
            streamed_any = True
            for phrase in chunker.push(token):
                phrases.put_nowait(phrase)

        async def filler() -> None:
            await asyncio.sleep(self.filler_after_s)
            if stats.first_audio_ms is None and phrases.empty():
                phrases.put_nowait(FILLERS[self.language_hint()])

        speaker = asyncio.create_task(speak_phrases())
        filler_task = asyncio.create_task(filler()) if (text is not None and not scripted) else None
        try:
            if scripted:
                for phrase in chunker.push(scripted):
                    phrases.put_nowait(phrase)
            else:
                response = await self.agent_turn(self, text or "", on_token)
                if not streamed_any and response:
                    # A path that did not stream (an error message, a budget stop)
                    # is still spoken rather than leaving the caller in silence.
                    for phrase in chunker.push(response):
                        phrases.put_nowait(phrase)
            tail = chunker.flush()
            if tail:
                phrases.put_nowait(tail)
            if filler_task:
                filler_task.cancel()
            phrases.put_nowait(None)
            await speaker
        except asyncio.CancelledError:
            stats.interrupted = True
            speaker.cancel()
            if filler_task:
                filler_task.cancel()
            heard = self._heard(spoken, queued_ms, total_ms)
            if heard:
                self._record("agent", heard + " …", meta={"interrupted": True})
                self._remember_interrupted(heard)
            self._account(stats)
            raise
        except Exception:
            logger.exception("Call %s: turn failed", self.ctx.call_id)
            speaker.cancel()
            if filler_task:
                filler_task.cancel()
            self.result.degraded.append("turn_error")
            with contextlib.suppress(Exception):
                async for chunk in self.tts.synthesize(TECH_TROUBLE[self.language_hint()]):
                    await self.audio_out.play(chunk)
            self._account(stats)
            return

        if spoken:
            self._record("agent", " ".join(spoken))
        self._account(stats)
        self._last_activity = time.monotonic()
        await self._apply_actions()

    def _heard(self, spoken: List[str], queued_ms: List[int], total_ms: int) -> str:
        played_ms = total_ms - self.audio_out.pending_ms()
        return " ".join(phrase for phrase, end in zip(spoken, queued_ms) if end <= played_ms + 60)

    def _remember_interrupted(self, heard: str) -> None:
        # The cancelled turn never saved the assistant message; without this the
        # agent would not know what it already told the caller.
        def write() -> None:
            try:
                from backend.database import save_message

                save_message(self.ctx.session_id, "assistant", heard + " …")
            except Exception:
                logger.exception("Call %s: could not store the interrupted reply", self.ctx.call_id)

        self._submit(write)

    def _submit(self, fn) -> None:
        try:
            self._archive.submit(fn)
        except RuntimeError:
            fn()  # executor already shut down (call ended) — write inline, rarely

    def _account(self, stats: TurnStats) -> None:
        self.result.turns += 1
        if stats.first_audio_ms is not None:
            self.result.latencies_ms.append(stats.first_audio_ms)
            logger.info("Call %s turn %d: first audio %d ms%s", self.ctx.call_id, self.result.turns,
                        stats.first_audio_ms, " (interrupted)" if stats.interrupted else "")

    async def _wait_playback(self, timeout_s: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_s
        while self.audio_out.pending_ms() > 0 and time.monotonic() < deadline and not self._ended.is_set():
            await asyncio.sleep(0.05)

    async def _speak_and_wait(self, text: str) -> None:
        with contextlib.suppress(Exception):
            async for chunk in self.tts.synthesize(text):
                await self.audio_out.play(chunk)
        await self._wait_playback()

    async def _apply_actions(self) -> None:
        actions = self.ctx.actions
        if "transfer" in actions:
            target = actions.pop("transfer")
            self._finishing = True
            await self._wait_playback()
            number = str((target or {}).get("to") or "")
            ok = False
            with contextlib.suppress(Exception):
                ok = await self.control.transfer(number)
            if ok:
                self.result.transferred_to = number
                self._note(f"↪ Звонок переведён на владельца ({number})", {"event": "transfer", "reason": (target or {}).get("reason")})
                await self._end("transferred", hangup=False)
                return
            self._finishing = False
            self._note("Перевод на владельца не удался", {"event": "transfer_failed"})
            self._start_turn(None, scripted=TRANSFER_FAILED[self.language_hint()])
            return
        if "hangup" in actions:
            reason = actions.pop("hangup")
            self._finishing = True
            await self._wait_playback()
            await self._end("agent_hangup" if reason else "agent_hangup", hangup=True)

    async def _watchdog(self) -> None:
        while not self._ended.is_set():
            await asyncio.sleep(0.5)
            now = time.monotonic()
            if now - self._started_at > self.max_call_s and not self._finishing:
                self._finishing = True
                await self._barge_in()
                await self._speak_and_wait(TOO_LONG[self.language_hint()])
                await self._end("max_duration", hangup=True)
                return
            if self._agent_busy() or self._finishing:
                self._last_activity = max(self._last_activity, now) if self._agent_busy() else self._last_activity
                continue
            idle = now - self._last_activity
            if idle > self.silence_hangup_s:
                self._finishing = True
                await self._speak_and_wait(GOODBYE_SILENCE[self.language_hint()])
                await self._end("silence", hangup=True)
                return
            if idle > self.silence_prompt_s and not self._prompted_silence:
                self._prompted_silence = True
                self._start_turn(None, scripted=STILL_THERE[self.language_hint()])

    # ── archive ─────────────────────────────────────────────────────────────
    def _record(self, speaker: str, text: str, meta: Optional[Dict[str, Any]] = None) -> None:
        if not self.ctx.conversation_id:
            return
        conversation_id, call_id = self.ctx.conversation_id, self.ctx.call_id

        def write() -> None:
            try:
                from backend import inbox

                inbox.record_agent_reply(conversation_id, text, speaker=speaker, meta={**(meta or {}), "call_id": call_id})
            except Exception:
                logger.exception("Call %s: could not archive a %s line", call_id, speaker)

        self._submit(write)

    def _note(self, text: str, meta: Dict[str, Any]) -> None:
        self._record("system", text, meta)


_REASON_LABELS = {
    "remote_hangup": "собеседник положил трубку",
    "agent_hangup": "агент завершил разговор",
    "transferred": "переведён на владельца",
    "silence": "собеседник не отвечал",
    "max_duration": "превышена длительность",
    "stt_unavailable": "распознавание недоступно",
}
