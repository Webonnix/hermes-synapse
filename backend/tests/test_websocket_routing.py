"""Addressed WebSocket delivery and resumable run streams.

These cover the failure that used to look like "the agent never answered":
every frame of a run went to every socket, and anything emitted while a socket
was reconnecting was gone for good.
"""

import json

import pytest

from backend import websocket_manager as wsm


class FakeWebSocket:
    """Records the frames a connection received. ``fail_after`` simulates a
    socket that dies mid-send so the manager's cleanup path is exercised."""

    def __init__(self, fail_after: int | None = None):
        self.sent: list[str] = []
        self.accepted_subprotocol = None
        self.fail_after = fail_after

    async def accept(self, subprotocol=None):
        self.accepted_subprotocol = subprotocol

    async def send_text(self, message: str):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("socket is gone")
        self.sent.append(message)

    # Convenience for assertions -------------------------------------------------
    def frames(self, frame_type: str | None = None) -> list[dict]:
        decoded = [json.loads(item) for item in self.sent]
        if frame_type is None:
            return decoded
        return [frame for frame in decoded if frame.get("type") == frame_type]


@pytest.fixture
def manager():
    return wsm.ConnectionManager()


async def _connect(manager, protocol=wsm.PROTOCOL_V2, fail_after=None):
    socket = FakeWebSocket(fail_after=fail_after)
    await manager.connect(socket, subprotocol=protocol)
    return socket


@pytest.mark.asyncio
async def test_chat_traffic_only_reaches_subscribed_connections(manager):
    watcher = await _connect(manager)
    bystander = await _connect(manager)
    manager.subscribe(watcher, ["chat_a"])
    manager.subscribe(bystander, ["chat_b"])

    await manager.send_chat_event("chat_a", {"type": "chat_message", "content": "secret"})

    assert [frame["content"] for frame in watcher.frames("chat_message")] == ["secret"]
    assert bystander.frames("chat_message") == []


@pytest.mark.asyncio
async def test_global_events_still_reach_every_connection(manager):
    first = await _connect(manager)
    second = await _connect(manager)
    manager.subscribe(first, ["chat_a"])

    await manager.broadcast({"type": "config_update", "model": "x"})

    assert first.frames("config_update") and second.frames("config_update")


@pytest.mark.asyncio
async def test_legacy_client_keeps_receiving_everything(manager):
    """A stale frontend bundle negotiates hermes-v1 and never subscribes. It must
    degrade to the old firehose rather than go silent."""
    legacy = await _connect(manager, protocol=wsm.PROTOCOL_V1)
    await manager.send_chat_event("chat_a", {"type": "chat_message", "content": "hi"})
    assert legacy.frames("chat_message")


@pytest.mark.asyncio
async def test_numeric_chat_ids_route_to_string_subscriptions(manager):
    """Telegram producers hold an int chat id; subscriptions arrive from JSON as
    strings. Both sides normalize or the mirror silently stops working."""
    socket = await _connect(manager)
    manager.subscribe(socket, ["12345"])
    await manager.send_chat_event(12345, {"type": "chat_message", "content": "tg"})
    assert socket.frames("chat_message")


@pytest.mark.asyncio
async def test_run_events_carry_a_monotonic_seq(manager):
    socket = await _connect(manager)
    manager.subscribe(socket, ["chat_a"])
    for index in range(3):
        await manager.send_run_event("run1", "chat_a", {"type": "chat_stream_chunk", "content": str(index)})
    assert [frame["seq"] for frame in socket.frames("chat_stream_chunk")] == [1, 2, 3]


@pytest.mark.asyncio
async def test_resume_replays_only_what_the_client_missed(manager):
    live = await _connect(manager)
    manager.subscribe(live, ["chat_a"])
    for index in range(5):
        await manager.send_run_event("run1", "chat_a", {"type": "chat_stream_chunk", "content": str(index)})

    reconnected = await _connect(manager)
    await manager.resume(reconnected, "run1", after_seq=2)

    begin = reconnected.frames("resume_begin")[0]
    assert begin["status"] == "ok"
    assert begin["count"] == 3
    assert [frame["content"] for frame in reconnected.frames("chat_stream_chunk")] == ["2", "3", "4"]
    assert reconnected.frames("resume_end")[0]["last_seq"] == 5
    # Resuming implies interest in the chat, so later live frames arrive too.
    assert "chat_a" in manager.subscriptions_for(reconnected)


@pytest.mark.asyncio
async def test_resume_after_the_answer_completed_still_delivers_it(manager):
    """The reconnect lands after the run finished. Everything, including the
    terminal frame carrying the full answer, must still be replayable — this is
    the case that used to require a page reload."""
    manager_socket = await _connect(manager)
    manager.subscribe(manager_socket, ["chat_a"])
    await manager.send_run_event("run1", "chat_a", {"type": "chat_stream_chunk", "content": "par"})
    await manager.send_run_event("run1", "chat_a", {"type": "chat_message", "role": "assistant", "content": "partial answer"})
    await manager.send_run_event("run1", "chat_a", {"type": "chat_stream_end"})
    manager.close_run("run1")

    reconnected = await _connect(manager)
    await manager.resume(reconnected, "run1", after_seq=1)

    assert reconnected.frames("resume_begin")[0]["status"] == "ok"
    assert reconnected.frames("chat_message")[0]["content"] == "partial answer"
    assert reconnected.frames("chat_stream_end")
    assert reconnected.frames("resume_end")[0]["closed"] is True


@pytest.mark.asyncio
async def test_resume_reports_a_gap_when_the_buffer_no_longer_reaches_the_cursor(manager, monkeypatch):
    monkeypatch.setattr(wsm, "RUN_BUFFER_EVENTS", 4)
    live = await _connect(manager)
    manager.subscribe(live, ["chat_a"])
    for index in range(10):
        await manager.send_run_event("run1", "chat_a", {"type": "chat_stream_chunk", "content": str(index)})

    reconnected = await _connect(manager)
    await manager.resume(reconnected, "run1", after_seq=1)

    begin = reconnected.frames("resume_begin")[0]
    assert begin["status"] == "gap"
    # Everything still retained is replayed, so the client can rebuild from scratch.
    assert begin["count"] == 4
    assert [frame["content"] for frame in reconnected.frames("chat_stream_chunk")] == ["6", "7", "8", "9"]


@pytest.mark.asyncio
async def test_cursor_ahead_of_the_server_replays_from_the_start(manager):
    """A stale or foreign cursor must never be honoured as 'nothing to send' —
    that would silently skip real events."""
    live = await _connect(manager)
    manager.subscribe(live, ["chat_a"])
    for index in range(3):
        await manager.send_run_event("run1", "chat_a", {"type": "chat_stream_chunk", "content": str(index)})

    reconnected = await _connect(manager)
    await manager.resume(reconnected, "run1", after_seq=999)

    begin = reconnected.frames("resume_begin")[0]
    assert begin["status"] == "gap"
    assert begin["count"] == 3


@pytest.mark.asyncio
async def test_resume_for_an_unknown_run_tells_the_client_to_reload(manager):
    socket = await _connect(manager)
    await manager.resume(socket, "never-existed", after_seq=0)
    assert socket.frames("resume_begin")[0]["status"] == "unknown"
    assert socket.frames("resume_end")[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_closed_run_buffers_expire(manager, monkeypatch):
    socket = await _connect(manager)
    manager.subscribe(socket, ["chat_a"])
    await manager.send_run_event("run1", "chat_a", {"type": "chat_stream_chunk", "content": "a"})
    manager.close_run("run1")
    assert manager.run_status("run1") is not None

    clock = [wsm.time.time() + wsm.CLOSED_RUN_TTL_SECONDS + 1]
    monkeypatch.setattr(wsm.time, "time", lambda: clock[0])
    await manager.send_run_event("run2", "chat_a", {"type": "chat_stream_chunk", "content": "b"})
    assert manager.run_status("run1") is None
    assert manager.run_status("run2") is not None


@pytest.mark.asyncio
async def test_tracked_runs_are_capped(manager, monkeypatch):
    monkeypatch.setattr(wsm, "MAX_TRACKED_RUNS", 3)
    socket = await _connect(manager)
    manager.subscribe(socket, ["chat_a"])
    for index in range(6):
        await manager.send_run_event(f"run{index}", "chat_a", {"type": "chat_stream_chunk", "content": "x"})
    tracked = [f"run{index}" for index in range(6) if manager.run_status(f"run{index}")]
    assert len(tracked) == 3
    assert tracked == ["run3", "run4", "run5"]


@pytest.mark.asyncio
async def test_a_dead_socket_is_dropped_and_does_not_block_the_others(manager):
    healthy = await _connect(manager)
    dying = await _connect(manager, fail_after=0)
    manager.subscribe(healthy, ["chat_a"])
    manager.subscribe(dying, ["chat_a"])

    await manager.send_chat_event("chat_a", {"type": "chat_message", "content": "still delivered"})

    assert healthy.frames("chat_message")
    assert manager.active_connections == {healthy}


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery(manager):
    socket = await _connect(manager)
    manager.subscribe(socket, ["chat_a"])
    manager.unsubscribe(socket, ["chat_a"])
    await manager.send_chat_event("chat_a", {"type": "chat_message", "content": "nope"})
    assert socket.frames("chat_message") == []


@pytest.mark.asyncio
async def test_non_owner_cannot_subscribe_to_owner_chats_or_resume_owner_run(manager):
    socket = FakeWebSocket()
    await manager.connect(socket, subprotocol=wsm.PROTOCOL_V1, owner=False)
    manager.subscribe(socket, ['dashboard', 'inbox'])
    await manager.broadcast({'type': 'config_update', 'secret': 'private'})
    await manager.send_chat_event('dashboard', {'type': 'chat_message', 'content': 'private'})
    await manager.send_run_event('private-run', 'dashboard', {'type': 'chat_stream_chunk', 'content': 'private'})
    await manager.resume(socket, 'private-run', 0)
    assert all('private' not in frame.get('content', '') for frame in socket.frames())
    assert not socket.frames('config_update')
    assert socket.frames('resume_begin')[0]['status'] == 'unknown'
    await manager.send_chat_event('inbox', {'type': 'inbox_changed'})
    assert socket.frames('inbox_changed')
