"""Process-wide priority queue in front of the local llama.cpp server.

llama.cpp runs with --parallel 1, so it serves one request at a time in
arrival order anyway. Queueing here instead costs no throughput but decides
the order: interactive chat goes first, the privacy gateway's checks wait
behind it, background work last. A waiter can also give up after a queue
timeout, which the privacy pass uses to fall back instead of stalling an
external call behind a long local generation.

Capacity comes from LLAMACPP_MAX_CONCURRENCY (default 1, matching
--parallel 1); raise both together.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, List, Optional, Tuple

PRIORITY_INTERACTIVE = 0
PRIORITY_PRIVACY = 5
PRIORITY_BACKGROUND = 10


class GateTimeout(asyncio.TimeoutError):
    """Gave up waiting for a free slot."""


class PriorityGate:
    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self._active = 0
        self._waiters: List[Tuple[int, int, asyncio.Future]] = []
        self._seq = itertools.count()

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return sum(1 for _, _, fut in self._waiters if not fut.done())

    async def acquire(self, priority: int, timeout: Optional[float] = None) -> None:
        if self._active < self.capacity and not self.waiting:
            self._active += 1
            return
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, next(self._seq), fut))
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if fut.done() and not fut.cancelled():
                # Granted in the same tick we gave up: hand the slot on.
                self.release()
            else:
                fut.cancel()
            if isinstance(exc, asyncio.TimeoutError):
                raise GateTimeout(f"local model busy for more than {timeout:.0f}s") from None
            raise

    def release(self) -> None:
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                fut.set_result(None)  # slot passes straight to this waiter
                return
        self._active = max(0, self._active - 1)

    @asynccontextmanager
    async def slot(self, priority: int = PRIORITY_INTERACTIVE,
                   timeout: Optional[float] = None) -> AsyncIterator[None]:
        await self.acquire(priority, timeout)
        try:
            yield
        finally:
            self.release()


_gates: dict = {}


def llamacpp_gate() -> PriorityGate:
    """The gate for the current event loop (tests run one loop per test)."""
    loop = asyncio.get_running_loop()
    gate = _gates.get(id(loop))
    if gate is None or gate[0] is not loop:
        try:
            capacity = int(os.getenv("LLAMACPP_MAX_CONCURRENCY", "1") or 1)
        except ValueError:
            capacity = 1
        gate = (loop, PriorityGate(capacity))
        _gates.clear()
        _gates[id(loop)] = gate
    return gate[1]
