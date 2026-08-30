"""Keeps the active Ollama model resident in VRAM.

``keep_alive: -1`` is not what it sounds like. It is a property of a *request*,
not of the server: it tells Ollama "after answering this, don't start an expiry
timer for the model". Nothing about it survives the model leaving VRAM, so the
2x3090 sits empty in all three of these cases, and the next message pays the
full 30-60s reload:

* **Ollama restarted** (container update, host reboot, driver reset, OOM). VRAM
  comes back empty and stays empty until somebody happens to send a message —
  there is no boot-time load anywhere in the stack.
* **A request asked for a short expiry.** ``agent.ollama_keep_alive`` starts
  from ``OLLAMA_KEEP_ALIVE`` but is then overwritten by whatever is saved in
  ``app_settings`` (agent.py's ``_load_persisted_runtime_config``), so a "5m"
  saved once from the dashboard quietly beats the ``-1`` in docker-compose.yml
  on every restart from then on. One such request re-arms the timer, and five
  idle minutes later the model is gone.
* **Somebody unloaded it** — the dashboard's Ollama panel has that button, and
  it sends ``keep_alive: 0``.

So this module does the part ``keep_alive`` cannot: it periodically checks
whether the model the agent is actually configured to use is loaded with a
far-off expiry, and if it isn't, loads it with an empty prompt (no generation,
just weights into VRAM). One tick after any of the events above, the model is
back — without a human noticing it was gone.

Deliberately cheap: ``/api/ps`` on every tick, and a load request only when the
answer says one is needed. It follows ``agent_instance.model`` rather than the
env, so switching models in the dashboard moves the pin instead of fighting it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from backend.ollama_client import normalize_keep_alive, resolve_installed_model

logger = logging.getLogger("hermes.model_keeper")

INTERVAL_SECONDS = max(15, int(os.getenv("OLLAMA_KEEP_RESIDENT_INTERVAL", "60") or 60))
# Loading a 35B model into two cards takes tens of seconds; the tick that does
# it must not time out halfway and leave a half-loaded runner behind.
LOAD_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_KEEP_RESIDENT_LOAD_TIMEOUT", "900") or 900)
# Re-pin while the current expiry is still this many ticks away, so a model
# loaded with a short keep_alive is renewed well before it can actually fall
# out of VRAM.
_EXPIRY_MARGIN_TICKS = 3

_ISO_FRACTION_RE = re.compile(r"\.(\d+)")

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def keep_resident_enabled() -> bool:
    """``OLLAMA_KEEP_RESIDENT`` if set, otherwise inferred from keep_alive.

    Unset defaults to following the intent already declared elsewhere:
    ``OLLAMA_KEEP_ALIVE=-1`` *asks* for permanent residency, and until this
    module existed nothing in the stack actually delivered it. A deployment
    that asked for a finite expiry keeps getting one — pinning a GPU is not a
    default anyone should get by surprise."""
    raw = (os.getenv("OLLAMA_KEEP_RESIDENT", "") or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return normalize_keep_alive(os.getenv("OLLAMA_KEEP_ALIVE", "5m")) == -1


def _expires_in_seconds(entry: dict[str, Any], now: Optional[datetime] = None) -> Optional[float]:
    """Seconds until Ollama would evict this model, or None if it can't be read.

    ``expires_at`` is RFC 3339, and for a ``-1`` pin Ollama reports a date
    centuries out — which is exactly what "no expiry" looks like from here, so
    no special case is needed for it."""
    raw = str(entry.get("expires_at") or entry.get("expiresAt") or "").strip()
    if not raw:
        return None
    # Python 3.10's fromisoformat rejects "Z", and every version rejects the
    # nanosecond precision Ollama emits — trim both down to what it accepts.
    text = _ISO_FRACTION_RE.sub(lambda match: "." + match.group(1)[:6].ljust(6, "0"), raw.replace("Z", "+00:00"), count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed - (now or datetime.now(timezone.utc))).total_seconds()


def needs_pin(
    running: list[dict[str, Any]], model: str, horizon_seconds: float, num_ctx: Optional[int] = None,
) -> bool:
    """Whether `model` has to be (re)loaded to still be resident in a while.

    True when it isn't loaded at all, when its expiry falls inside the horizon,
    and when the expiry can't be parsed — an unreadable answer is treated as
    "about to expire" on purpose: a redundant load is one cheap request, a
    missed one costs the next message a full reload.

    A runner loaded at a different context size counts as "not loaded": Ollama
    would reload the whole model on the agent's first chat anyway, so leaving
    the wrong one pinned means paying for the load twice and keeping VRAM
    occupied by a runner nobody will use."""
    if not model:
        return False
    names = [str(item.get("name") or item.get("model") or "") for item in running or []]
    match = resolve_installed_model(model, [name for name in names if name])
    if not match:
        return True
    for item in running or []:
        name = str(item.get("name") or item.get("model") or "")
        if name != match:
            continue
        loaded_ctx = item.get("context_length")
        if num_ctx and isinstance(loaded_ctx, int) and loaded_ctx != int(num_ctx):
            return True
        remaining = _expires_in_seconds(item)
        return remaining is None or remaining <= horizon_seconds
    return True


async def pin_once(client: Any = None, agent: Any = None) -> str:
    """One check-and-load pass. Returns what it did, for logs and tests."""
    if agent is None:
        from backend.agent import agent_instance as agent
    provider = (getattr(agent, "provider", "") or "").strip().lower()
    if provider != "ollama":
        return "skipped:provider"
    model = (getattr(agent, "model", "") or "").strip()
    if not model:
        return "skipped:no-model"

    if client is None:
        from backend.ollama_client import OllamaClient

        client = OllamaClient(agent.ollama_base_url, timeout=LOAD_TIMEOUT_SECONDS)

    num_ctx = getattr(agent, "ollama_num_ctx", None)
    running = await client.list_running()
    if not needs_pin(running, model, INTERVAL_SECONDS * _EXPIRY_MARGIN_TICKS, num_ctx):
        return "resident"

    await client.load_model(model, keep_alive=-1, num_ctx=num_ctx)
    logger.info("Pinned %s in VRAM (keep_alive=-1, num_ctx=%s).", model, num_ctx)
    return "pinned"


async def keeper_loop() -> None:
    """Background task started from the app lifespan when keep-resident is on."""
    from backend.agent import agent_instance

    effective = normalize_keep_alive(getattr(agent_instance, "ollama_keep_alive", ""))
    if effective != -1:
        # Not fatal — every tick below re-pins with -1 regardless — but this is
        # the setting people think they changed in docker-compose.yml, so say
        # out loud that a persisted dashboard value is overriding it.
        logger.warning(
            "Keep-resident is on, but the agent's effective keep_alive is %r (a value saved in "
            "app_settings overrides OLLAMA_KEEP_ALIVE). Requests will keep re-arming an expiry; "
            "this task will keep cancelling it every %ds.",
            effective, INTERVAL_SECONDS,
        )
    logger.info("Ollama keep-resident started (checking every %ds).", INTERVAL_SECONDS)
    while True:
        try:
            await pin_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Ollama being down is the normal case here, not an anomaly worth a
            # stack trace every minute — the next tick loads the model as soon
            # as it comes back.
            logger.warning("Keep-resident tick failed (%s) — retrying in %ds.", exc, INTERVAL_SECONDS)
        await asyncio.sleep(INTERVAL_SECONDS)
