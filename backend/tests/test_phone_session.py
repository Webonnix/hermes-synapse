"""The call loop: turn-taking, barge-in, actions, silence — with every edge faked, so
the real session logic runs against a scripted caller, agent, voice and line."""

import asyncio
import time

import pytest

from backend import phone_session, phone_speech
from backend.phone_context import CallContext


class FakeSTT(phone_speech.STTStream):
    name = "fake"

    def __init__(self, has_partials=True, fail=False):
        super().__init__()
        self.has_partials = has_partials
        self.fail = fail
        self.frames = 0

    async def start(self):
        if self.fail:
            raise phone_speech.SpeechError("down")

    async def send(self, frame):
        self.frames += 1


class FakeTTS(phone_speech.TTSEngine):
    name = "fake-tts"

    def __init__(self, bytes_per_char=160, delay=0.0):
        self.bytes_per_char = bytes_per_char
        self.delay = delay
        self.phrases = []

    async def synthesize(self, text):
        self.phrases.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        data = b"\x01\x00" * (len(text) * self.bytes_per_char // 2)
        for start in range(0, len(data), 1600):
            yield data[start:start + 1600]
            await asyncio.sleep(0)


class FakeAudioOut:
    """Plays in simulated real time: pending = audio queued minus time elapsed."""

    def __init__(self):
        self.played = 0
        self.cleared = 0
        self._playhead = time.monotonic()

    async def play(self, pcm):
        now = time.monotonic()
        self._playhead = max(self._playhead, now) + len(pcm) / 16000
        self.played += len(pcm)

    async def clear(self):
        self.cleared += 1
        self._playhead = time.monotonic()

    def pending_ms(self):
        return max(0, int((self._playhead - time.monotonic()) * 1000))


class FakeControl:
    def __init__(self, transfer_ok=True):
        self.hangups = []
        self.transfers = []
        self.transfer_ok = transfer_ok

    async def hangup(self, reason=""):
        self.hangups.append(reason)

    async def transfer(self, number):
        self.transfers.append(number)
        return self.transfer_ok


class Session(phone_session.CallSession):
    def __init__(self, **kwargs):
        self.lines = []
        super().__init__(**kwargs)

    def _record(self, speaker, text, meta=None):
        self.lines.append((speaker, text, dict(meta or {})))

    def _remember_interrupted(self, heard):
        self.remembered = heard


def _ctx(direction="inbound", owner_number="+15550009"):
    return CallContext(call_id="call-1", line_id="line-1", conversation_id="ibx-1", session_id="phone:line-1:+1555",
                       direction=direction, remote_number="+15550001", owner_number=owner_number, language="ru")


def _session(agent_turn, *, stt=None, tts=None, out=None, control=None, greeting="", **kwargs):
    return Session(ctx=kwargs.pop("ctx", _ctx()), agent_id="agent-1", audio_out=out or FakeAudioOut(),
                   control=control or FakeControl(), stt=stt or FakeSTT(), tts=tts or FakeTTS(),
                   greeting=greeting, agent_turn=agent_turn, **kwargs)


def streaming_agent(text, delay=0.0, action=None):
    async def turn(session, caller_text, on_token):
        turn.calls.append(caller_text)
        for word in text.split(" "):
            await on_token(word + " ")
            if delay:
                await asyncio.sleep(delay)
        if action:
            session.ctx.actions.update(action)
        return text
    turn.calls = []
    return turn


async def _until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_a_caller_turn_is_answered_spoken_and_archived_with_its_latency():
    agent = streaming_agent("Да, мы работаем в субботу с десяти утра. Чем ещё помочь?")
    tts = FakeTTS(bytes_per_char=20)
    session = _session(agent, tts=tts, greeting="Здравствуйте! Вы позвонили в магазин.")
    runner = asyncio.create_task(session.run())
    await _until(lambda: any(s == "agent" for s, *_ in session.lines))
    session.stt.emit("final", "Вы работаете в субботу?")
    await _until(lambda: len([l for l in session.lines if l[0] == "agent"]) == 2)

    assert agent.calls == ["Вы работаете в субботу?"]
    assert ("customer", "Вы работаете в субботу?", {}) in session.lines
    assert tts.phrases[-2:] == ["Да, мы работаем в субботу с десяти утра.", "Чем ещё помочь?"]
    assert session.result.turns == 2 and len(session.result.latencies_ms) == 2
    await session.on_remote_hangup()
    await runner
    assert session.result.end_reason == "remote_hangup" and session.control.hangups == []


@pytest.mark.asyncio
async def test_the_caller_cutting_in_stops_the_voice_and_only_what_was_heard_is_stored():
    long_answer = "Первое предложение. " + " ".join(["Очень длинное продолжение ответа."] * 12)
    agent = streaming_agent(long_answer, delay=0.01)
    out = FakeAudioOut()
    session = _session(agent, out=out, tts=FakeTTS(bytes_per_char=160))
    runner = asyncio.create_task(session.run())
    session.stt.emit("final", "Расскажите подробнее")
    await _until(lambda: out.played > 0)
    await asyncio.sleep(0.05)
    session.stt.emit("partial", "Стоп, подождите")
    await _until(lambda: session.result.interruptions == 1)

    assert out.cleared == 1
    interrupted = [l for l in session.lines if l[0] == "agent"]
    assert not interrupted or interrupted[-1][2].get("interrupted")
    assert all("продолжение" not in l[1] for l in interrupted)

    session.stt.emit("final", "Сколько это стоит?")
    await _until(lambda: agent.calls[-1] == "Сколько это стоит?")
    await session.on_remote_hangup()
    await runner


@pytest.mark.asyncio
async def test_a_short_noise_does_not_interrupt_the_agent():
    agent = streaming_agent("Длинный ответ " * 30, delay=0.005)
    out = FakeAudioOut()
    session = _session(agent, out=out)
    runner = asyncio.create_task(session.run())
    session.stt.emit("final", "Вопрос")
    await _until(lambda: out.played > 0)
    session.stt.emit("partial", "а")
    await asyncio.sleep(0.05)
    assert session.result.interruptions == 0
    await session.on_remote_hangup()
    await runner


@pytest.mark.asyncio
async def test_without_partials_speech_start_is_enough_to_barge_in():
    agent = streaming_agent("Длинный ответ " * 30, delay=0.005)
    out = FakeAudioOut()
    session = _session(agent, out=out, stt=FakeSTT(has_partials=False))
    runner = asyncio.create_task(session.run())
    session.stt.emit("final", "Вопрос")
    await _until(lambda: out.played > 0)
    session.stt.emit("speech_started")
    await _until(lambda: session.result.interruptions == 1)
    await session.on_remote_hangup()
    await runner


@pytest.mark.asyncio
async def test_end_call_waits_for_the_goodbye_then_hangs_up():
    agent = streaming_agent("Спасибо за звонок, до свидания!", action={"hangup": "done"})
    out = FakeAudioOut()
    control = FakeControl()
    session = _session(agent, out=out, control=control, tts=FakeTTS(bytes_per_char=40))
    runner = asyncio.create_task(session.run())
    session.stt.emit("final", "Всё, спасибо")
    await asyncio.wait_for(runner, 5)
    assert control.hangups and session.result.end_reason == "agent_hangup"
    assert out.pending_ms() == 0


@pytest.mark.asyncio
async def test_transfer_hands_the_call_to_the_owner_and_ends_the_ai_side():
    agent = streaming_agent("Соединяю с владельцем.", action={"transfer": {"to": "+15550009", "reason": "договор"}})
    control = FakeControl(transfer_ok=True)
    session = _session(agent, control=control, tts=FakeTTS(bytes_per_char=10))
    runner = asyncio.create_task(session.run())
    session.stt.emit("final", "Соедините с Альбертом")
    await asyncio.wait_for(runner, 5)
    assert control.transfers == ["+15550009"] and control.hangups == []
    assert session.result.end_reason == "transferred" and session.result.transferred_to == "+15550009"


@pytest.mark.asyncio
async def test_a_failed_transfer_keeps_the_ai_on_the_line_and_offers_a_message():
    agent = streaming_agent("Соединяю.", action={"transfer": {"to": "+15550009"}})
    tts = FakeTTS(bytes_per_char=10)
    session = _session(agent, control=FakeControl(transfer_ok=False), tts=tts)
    runner = asyncio.create_task(session.run())
    session.stt.emit("final", "Позовите человека")
    # The scripted apology is spoken phrase by phrase, so look for its first sentence.
    await _until(lambda: any("Не получилось соединить" in phrase for phrase in tts.phrases))
    assert not runner.done()
    await session.on_remote_hangup()
    await runner


@pytest.mark.asyncio
async def test_a_slow_agent_is_covered_by_a_short_filler():
    async def slow(session, text, on_token):
        await asyncio.sleep(0.3)
        await on_token("Нашёл свободное время в четверг.")
        return ""
    tts = FakeTTS(bytes_per_char=10)
    session = _session(slow, tts=tts, filler_after_s=0.05)
    runner = asyncio.create_task(session.run())
    session.stt.emit("final", "Когда можно встретиться?")
    await _until(lambda: len(tts.phrases) >= 2)
    assert tts.phrases[0] == "Секунду." and "четверг" in tts.phrases[1]
    await session.on_remote_hangup()
    await runner


@pytest.mark.asyncio
async def test_a_silent_caller_is_asked_once_then_the_call_is_closed():
    tts = FakeTTS(bytes_per_char=5)
    control = FakeControl()
    session = _session(streaming_agent("ok"), tts=tts, control=control, silence_prompt_s=0.2, silence_hangup_s=0.6)
    await asyncio.wait_for(session.run(), 5)
    assert tts.phrases[0] == phone_session.STILL_THERE["ru"]
    assert tts.phrases[-1] == phone_session.GOODBYE_SILENCE["ru"]
    assert session.result.end_reason == "silence" and control.hangups


@pytest.mark.asyncio
async def test_a_reply_that_did_not_stream_is_still_spoken():
    async def non_streaming(session, text, on_token):
        return "Лимит расходов исчерпан. Обратитесь к владельцу."
    tts = FakeTTS(bytes_per_char=5)
    session = _session(non_streaming, tts=tts)
    runner = asyncio.create_task(session.run())
    session.stt.emit("final", "Алло")
    await _until(lambda: len(tts.phrases) == 2)
    assert tts.phrases == ["Лимит расходов исчерпан.", "Обратитесь к владельцу."]
    await session.on_remote_hangup()
    await runner


@pytest.mark.asyncio
async def test_no_speech_recognition_means_an_apology_and_a_hangup_not_dead_air():
    tts = FakeTTS(bytes_per_char=5)
    control = FakeControl()
    session = _session(streaming_agent("x"), stt=FakeSTT(fail=True), tts=tts, control=control)
    await asyncio.wait_for(session.run(), 5)
    assert tts.phrases == [phone_session.TECH_TROUBLE["ru"]]
    assert control.hangups and session.result.end_reason == "stt_unavailable"


@pytest.mark.asyncio
async def test_an_outbound_call_opens_with_the_agent_introducing_itself():
    agent = streaming_agent("Здравствуйте! Это ассистент магазина, звоню подтвердить заказ.")
    session = _session(agent, ctx=_ctx(direction="outbound"))
    runner = asyncio.create_task(session.run())
    await _until(lambda: agent.calls)
    assert agent.calls[0].startswith("[Собеседник снял трубку")
    assert not any(s == "customer" for s, *_ in session.lines)
    await session.on_remote_hangup()
    await runner


@pytest.mark.asyncio
async def test_remote_hangup_mid_answer_cancels_the_turn_and_reports_once():
    finished = []

    async def on_finished(result):
        finished.append(result.end_reason)

    agent = streaming_agent("Очень длинный ответ " * 50, delay=0.01)
    stt = FakeSTT()
    session = _session(agent, stt=stt, on_finished=on_finished)
    runner = asyncio.create_task(session.run())
    stt.emit("final", "Вопрос")
    await asyncio.sleep(0.05)
    await session.on_remote_hangup()
    await session.on_remote_hangup()
    await asyncio.wait_for(runner, 3)
    assert finished == ["remote_hangup"] and stt.closed
