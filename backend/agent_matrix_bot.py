"""Per-agent Element/Matrix bot runtime.

Mirrors backend/agent_bot.py's AgentBotManager exactly, one nio.AsyncClient per
active binding in agent_messenger_bindings (platform='matrix'). Messages sent to
that Matrix account go straight to that one agent via
backend.agent.agent_instance._respond_as_subagent — same call every other channel
manager uses.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from typing import Dict, List, Optional

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    LocalProtocolError,
    MatrixRoom,
    MegolmEvent,
    RoomMessageText,
    SyncError,
)

try:  # True only when nio's [e2e] extra is installed (vodozemac since nio 0.25,
    # no system libolm needed). Without it nio silently degrades to
    # "cannot read or write encrypted rooms".
    from nio.crypto import ENCRYPTION_ENABLED
except Exception:  # pragma: no cover - defensive, nio always ships nio.crypto
    ENCRYPTION_ENABLED = False

logger = logging.getLogger("hermes.agent_matrix_bot")

MATRIX_TEXT_LIMIT = 8000

# Lives under backend/data/, which is the one path already bind-mounted from the
# host — the olm store MUST survive container rebuilds, or the bot comes back as
# a brand-new device every deploy and loses access to every room key it was sent.
MATRIX_STORE_ROOT = os.getenv(
    "MATRIX_STORE_ROOT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "matrix_store")
)


def _store_path(binding_id: str, device_id: str) -> Optional[str]:
    """Per-device store directory, or None if it cannot be created (in which case
    the bot still runs — just deaf and mute in encrypted rooms)."""
    path = os.path.join(MATRIX_STORE_ROOT, binding_id, device_id)
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        logger.exception("Matrix store directory %s is not writable — encryption disabled", path)
        return None


def forget_store(binding_id: str) -> None:
    """Drops a binding's olm identity. Only for when the binding itself goes away
    — calling this on a live binding would orphan every room key it holds."""
    shutil.rmtree(os.path.join(MATRIX_STORE_ROOT, binding_id), ignore_errors=True)


def _split_text(text: str, limit: int = MATRIX_TEXT_LIMIT) -> List[str]:
    """Same paragraph/line-boundary splitting as backend/agent_bot.py's _split_text."""
    remaining = (text or "").strip()
    if not remaining:
        return ["Ответ модели пуст."]
    chunks = []
    while len(remaining) > limit:
        split_at = max(
            remaining.rfind("\n\n", 0, limit),
            remaining.rfind("\n", 0, limit),
            remaining.rfind(" ", 0, limit),
        )
        if split_at < limit // 3:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        chunks.append(remaining)
    return chunks


class AgentMatrixBotManager:
    def __init__(self) -> None:
        self._clients: Dict[str, AsyncClient] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._renewals: Dict[str, asyncio.Task] = {}
        self._allowed_room_ids: Dict[str, List[str]] = {}
        self._overrides: Dict[str, dict] = {}
        self._response_modes: Dict[str, str] = {}
        self._credentials: Dict[str, dict] = {}
        self._pause_minutes: Dict[str, int] = {}
        # {binding_id: {room_id: unix_ts the pause expires at}} — the owner
        # typed in this room personally, so the bot backs off until then.
        self._human_active_until: Dict[str, Dict[str, float]] = {}

    def _note_human_takeover(self, binding_id: str, room_id: str) -> None:
        """The owner's own account IS this binding's identity (see
        [[matrix-mas-device-login]]), so a message from that same user id in a
        DM room can only mean one thing: the owner just typed there personally,
        from Element or another device. Nothing else on this binding can send
        as that user id."""
        minutes = self._pause_minutes.get(binding_id, 0)
        if minutes <= 0:  # 0 = feature explicitly turned off for this channel
            return
        until = time.time() + minutes * 60
        self._human_active_until.setdefault(binding_id, {})[room_id] = until
        from backend import channel_activity

        channel_activity.record(
            binding_id, "matrix", "info",
            f"Владелец сам ответил в этом чате — автоответы приостановлены на {minutes} мин.",
            room_id,
        )

    def _human_takeover_active(self, binding_id: str, room_id: str) -> bool:
        until = self._human_active_until.get(binding_id, {}).get(room_id)
        return bool(until and time.time() < until)

    def set_human_takeover_pause(self, binding_id: str, minutes: int) -> None:
        """Applies immediately, including to a pause already in progress — e.g.
        raising it from 60 to 240 extends the current silence instead of only
        affecting the next time the owner types."""
        self._pause_minutes[binding_id] = max(0, int(minutes))

    def _make_callback(self, binding_id: str, subagent_id: str):
        async def callback(room: MatrixRoom, event: RoomMessageText) -> None:
            client = self._clients.get(binding_id)
            if not client:
                return
            if event.sender == client.user_id:
                self._note_human_takeover(binding_id, room.room_id)
                return
            # Owner wants this account to act as a personal assistant that only
            # answers direct messages — never auto-reply into a named/group room,
            # regardless of allowed_room_ids. is_group + 2 members is the same
            # DM heuristic nio itself uses internally (see MatrixRoom.gen_avatar_url).
            if not (room.is_group and room.member_count == 2):
                from backend import channel_activity

                channel_activity.record(
                    binding_id, "matrix", "ignored_non_dm",
                    "Сообщение не из личного чата (DM) — проигнорировано", room.room_id, event.sender,
                )
                return

            if self._human_takeover_active(binding_id, room.room_id):
                from backend import channel_activity

                channel_activity.record(
                    binding_id, "matrix", "ignored_owner_active",
                    "Владелец сейчас сам общается в этом чате — сообщение получено, автоответ пропущен",
                    room.room_id, event.sender,
                )
                return

            async def _say(text: str) -> None:
                await self.send_text(binding_id, room.room_id, text)

            from backend import bot_access_gate

            decision = await asyncio.to_thread(
                bot_access_gate.authorize,
                binding_id, "matrix", room.room_id, event.body, event.sender, event.sender,
            )
            if decision.action == bot_access_gate.IGNORE:
                return
            if decision.action == bot_access_gate.REPLY:
                await _say(decision.reply_text)
                return

            from backend.database import get_subagent

            subagent = get_subagent(subagent_id)
            if not subagent or not subagent.get("is_enabled"):
                await _say(
                    bot_access_gate.AGENT_OFFLINE_MESSAGE
                    if decision.scope == "public"
                    else "Этот агент сейчас отключён, Альберт."
                )
                return

            from backend.agent_messenger_governance import apply_binding_overrides
            subagent = apply_binding_overrides(subagent, self._overrides.get(binding_id))
            subagent = bot_access_gate.apply_subscriber_context(subagent, decision)

            from backend.agent import agent_instance

            session_id = decision.session_id or f"matrixbot:{binding_id}:{room.room_id}"
            try:
                response_text = await agent_instance._respond_as_subagent(
                    event.body, subagent, chat_id=session_id
                )
            except Exception:
                logger.exception("Agent matrix bot %s: error handling message", binding_id)
                response_text = "Произошла ошибка при обработке запроса."
            bot_access_gate.record_turn(
                decision, agent_instance.last_run_metadata.get(session_id), response_text
            )

            mode = bot_access_gate.effective_response_mode(
                decision, self._response_modes.get(binding_id, "draft")
            )
            if mode == "auto_labeled":
                from backend.agent_messenger_governance import auto_reply_disclosure
                await _say(response_text + auto_reply_disclosure(binding_id))
                return

            # draft mode: never send anything to the other person automatically —
            # queue it and let the owner review/send from the dashboard.
            from backend.channel_replies import create_pending_reply
            create_pending_reply(
                binding_id=binding_id,
                platform="matrix",
                subagent_id=subagent_id,
                chat_id=room.room_id,
                incoming_text=event.body,
                drafted_reply=response_text,
                incoming_from=event.sender,
            )

        return callback

    def _warn_no_encryption(self, binding_id: str, device_id: str) -> None:
        from backend import channel_activity

        reason = (
            "Шифрование выключено: у подключения нет device_id — переподключитесь кнопкой «Войти через браузер»."
            if not device_id else
            "Шифрование выключено: в образе не установлен matrix-nio[e2e]."
        )
        logger.warning("Agent matrix bot %s: %s", binding_id, reason)
        channel_activity.record(binding_id, "matrix", "info", reason + " Зашифрованные чаты агент читать не сможет.")

    async def send_text(self, binding_id: str, room_id: str, text: str) -> None:
        """The single outbound path for this binding. Everything sends through the
        *running* client on purpose: it is the one that holds the olm store, so a
        reply into an encrypted room goes out encrypted instead of landing as a
        plaintext message with a red shield next to it."""
        client = self._clients.get(binding_id)
        if not client:
            raise RuntimeError("Matrix-канал сейчас не запущен — включите его и повторите отправку.")
        for piece in _split_text(text):
            await client.room_send(
                room_id, message_type="m.room.message", content={"msgtype": "m.text", "body": piece},
                ignore_unverified_devices=True,
            )

    def _make_undecryptable_callback(self, binding_id: str):
        """An encrypted message we have no key for. Normal for anything sent
        before this device existed; for anything newer it means the sender's
        client is not sharing keys with us, which is invisible otherwise — the
        chat just looks silent from the dashboard."""
        async def callback(room: MatrixRoom, event: MegolmEvent) -> None:
            from backend import channel_activity

            channel_activity.record(
                binding_id, "matrix", "error",
                "Не удалось расшифровать сообщение — у агента нет ключа комнаты. "
                "Подтвердите его сессию в Element (Настройки → Сессии), либо напишите ему новое сообщение после подключения.",
                room.room_id, event.sender,
            )

        return callback

    def _make_verification_callback(self, binding_id: str):
        """Lets the owner verify this bot device from their own Element via the
        emoji (SAS) flow, which is what removes the red shields and satisfies
        clients configured to share keys only with verified sessions.

        Deliberately narrow: only verifications started by the account's own user
        id are answered. Auto-confirming a stranger's verification would let them
        pass themselves off as a trusted device of this account."""
        async def callback(event) -> None:
            client = self._clients.get(binding_id)
            if not client or event.sender != client.user_id:
                return
            from backend import channel_activity

            try:
                if isinstance(event, KeyVerificationStart):
                    if "emoji" not in event.short_authentication_string:
                        await client.cancel_key_verification(event.transaction_id, reject=True)
                        return
                    await client.accept_key_verification(event.transaction_id)
                    sas = client.key_verifications[event.transaction_id]
                    await client.to_device(sas.share_key())
                elif isinstance(event, KeyVerificationKey):
                    await client.confirm_short_auth_string(event.transaction_id)
                elif isinstance(event, KeyVerificationMac):
                    sas = client.key_verifications[event.transaction_id]
                    try:
                        message = sas.get_mac()
                    except LocalProtocolError:
                        return  # our side isn't done yet; Element will re-send
                    await client.to_device(message)
                    channel_activity.record(binding_id, "matrix", "info", "Сессия агента подтверждена из Element")
            except Exception:
                logger.exception("Agent matrix bot %s: key verification failed", binding_id)

        return callback

    def _make_invite_callback(self, binding_id: str):
        """A new DM arrives as a room invite, not a message — nio never delivers
        that room's timeline (and so never fires RoomMessageText) until the
        account actually joins it. Without this, the first message from anyone
        new just sits as a pending invite and the bot never sees it."""
        async def callback(room: MatrixRoom, event: InviteMemberEvent) -> None:
            client = self._clients.get(binding_id)
            if not client or event.state_key != client.user_id or event.membership != "invite":
                return
            from backend import channel_activity

            try:
                await client.join(room.room_id)
                channel_activity.record(binding_id, "matrix", "invite", "Принято приглашение в комнату", room.room_id, event.sender)
            except Exception as exc:
                logger.exception("Agent matrix bot %s: failed to join invited room %s", binding_id, room.room_id)
                channel_activity.record(binding_id, "matrix", "error", f"Не удалось вступить в комнату: {exc}", room.room_id, event.sender)

        return callback

    async def refresh_binding_token(self, binding_id: str) -> bool:
        """MAS-backed homeservers (matrix.senla.eu and every Element-hosted one)
        issue access tokens that expire within minutes by design. If this binding
        was created with a refreshable credential — the device-code login, or a
        password login that asked for a refresh token — exchange it for a fresh
        pair and hand it to the live client without dropping the sync loop.
        Returns True if the sync loop can just keep going with the new token."""
        client = self._clients.get(binding_id)
        credentials = self._credentials.get(binding_id) or {}
        if not client or not credentials.get("refresh_token"):
            return False

        from backend import matrix_oauth

        try:
            updated = await asyncio.to_thread(matrix_oauth.refresh_credentials, credentials)
        except Exception as exc:
            logger.error("Agent matrix bot %s: token refresh failed: %s", binding_id, exc)
            return False

        client.access_token = updated["access_token"]
        self._credentials[binding_id] = updated

        from backend.agent_messenger_governance import update_matrix_binding_credentials

        await asyncio.to_thread(update_matrix_binding_credentials, binding_id, updated)

        from backend import channel_activity

        channel_activity.record(binding_id, "matrix", "info", "Access token автоматически обновлён через refresh_token")
        logger.info("Agent matrix bot %s: refreshed Matrix access token", binding_id)
        return True

    def _make_renewal_loop(self, binding_id: str):
        """Renews the access token slightly *before* the homeserver expires it.
        The reactive path below still exists as a backstop, but waiting for
        M_UNKNOWN_TOKEN means every token lifetime ends with a failed sync — on a
        MAS server that is every few minutes."""
        from backend import matrix_oauth

        async def loop() -> None:
            while True:
                credentials = self._credentials.get(binding_id)
                if credentials is None or binding_id not in self._clients:
                    return
                delay = matrix_oauth.seconds_until_refresh(credentials)
                if delay is None:  # no expiry advertised — nothing to pre-empt
                    return
                await asyncio.sleep(delay)
                if binding_id not in self._clients:
                    return
                if not await self.refresh_binding_token(binding_id):
                    # Keep trying until the token actually dies; the SyncError
                    # callback is what turns a truly dead session into a visible
                    # failure, and it also clears the state this loop reads.
                    await asyncio.sleep(30)

        return loop

    def _make_sync_error_callback(self, binding_id: str):
        """Matrix access tokens can die mid-flight (e.g. short-lived tokens
        issued by an OIDC/MAS-backed homeserver) without nio ever raising —
        sync_forever just silently retries the same failing request forever.
        This turns that into a loud, visible failure instead — unless a
        refresh_token lets us quietly renew and keep the sync loop running."""
        async def callback(response: SyncError) -> None:
            status = getattr(response, "status_code", None)
            if status not in ("M_UNKNOWN_TOKEN", "M_MISSING_TOKEN", "M_FORBIDDEN"):
                return
            client = self._clients.get(binding_id)
            if not client:
                return

            credentials = self._credentials.get(binding_id) or {}
            if await self.refresh_binding_token(binding_id):
                return

            had_refresh_token = bool(credentials.get("refresh_token"))
            reason = (
                f"Matrix отклонил токен доступа ({status}), обновить его через refresh_token тоже не удалось. "
                "Переподключите канал заново."
                if had_refresh_token else
                f"Matrix отклонил токен доступа ({status}) — он истёк или был отозван. "
                "Переподключите канал кнопкой «Войти через браузер», чтобы токен продлевался сам."
            )
            logger.error("Agent matrix bot %s: %s — stopping sync loop", binding_id, reason)
            client.stop_sync_forever()
            # Drop our own bookkeeping (but don't touch the client/task lifecycle
            # from inside this callback — we're on that same task's call stack).
            # Without this, start()'s "if binding_id in self._clients: return"
            # guard would silently refuse every future retry/reconnect attempt.
            self._clients.pop(binding_id, None)
            self._tasks.pop(binding_id, None)
            renewal = self._renewals.pop(binding_id, None)
            if renewal:
                renewal.cancel()
            self._allowed_room_ids.pop(binding_id, None)
            self._overrides.pop(binding_id, None)
            self._response_modes.pop(binding_id, None)
            self._credentials.pop(binding_id, None)
            self._pause_minutes.pop(binding_id, None)
            self._human_active_until.pop(binding_id, None)

            from backend import channel_activity

            channel_activity.record(binding_id, "matrix", "error", reason)

            from backend.agent_messenger_governance import mark_binding_failed

            await asyncio.to_thread(mark_binding_failed, binding_id, reason)

        return callback

    async def start(
        self, binding_id: str, subagent_id: str, credentials: dict, allowed_room_ids: Optional[List[str]] = None,
        overrides: Optional[dict] = None, response_mode: str = "draft",
    ) -> None:
        if binding_id in self._clients:
            return
        # Most real conversations on a modern homeserver are end-to-end encrypted.
        # Without an olm store the bot receives them as undecryptable MegolmEvents
        # and never sees a single word — so encryption is on whenever the runtime
        # can support it. It needs a stable device_id, which only the browser
        # (device-code) and password logins produce; a pasted access token has none.
        device_id = (credentials.get("device_id") or "").strip()
        store_path = _store_path(binding_id, device_id) if (ENCRYPTION_ENABLED and device_id) else None
        client = AsyncClient(
            credentials["homeserver_url"], credentials["user_id"],
            device_id=device_id or None, store_path=store_path,
            config=AsyncClientConfig(encryption_enabled=bool(store_path), store_sync_tokens=False),
        )
        client.access_token = credentials["access_token"]
        client.user_id = credentials["user_id"]
        if store_path:
            # Restoring a session rather than logging in, so nio needs to be told
            # to open the store explicitly.
            client.load_store()
            client.add_to_device_callback(self._make_verification_callback(binding_id), (
                KeyVerificationStart, KeyVerificationKey, KeyVerificationMac,
            ))
        else:
            self._warn_no_encryption(binding_id, device_id)
        client.add_event_callback(self._make_callback(binding_id, subagent_id), RoomMessageText)
        client.add_event_callback(self._make_invite_callback(binding_id), InviteMemberEvent)
        client.add_event_callback(self._make_undecryptable_callback(binding_id), MegolmEvent)
        client.add_response_callback(self._make_sync_error_callback(binding_id), SyncError)

        # Establish a sync token before subscribing so history predating this binding
        # never gets replayed through the agent.
        first_sync = await client.sync(timeout=30000, full_state=True)
        if isinstance(first_sync, SyncError):
            # A MAS access token can easily have expired between the approval and
            # this start (they live minutes) — renew once before declaring the
            # whole binding dead.
            self._clients[binding_id] = client
            self._credentials[binding_id] = dict(credentials)
            renewed = await self.refresh_binding_token(binding_id)
            credentials = self._credentials.pop(binding_id, credentials)
            self._clients.pop(binding_id, None)
            if renewed:
                first_sync = await client.sync(timeout=30000, full_state=True)
            if isinstance(first_sync, SyncError):
                await client.close()
                raise RuntimeError(f"Matrix rejected these credentials: {first_sync.status_code} {first_sync.message}")

        self._clients[binding_id] = client
        self._allowed_room_ids[binding_id] = list(allowed_room_ids or [])
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode
        self._credentials[binding_id] = dict(credentials)
        if binding_id not in self._pause_minutes:
            from backend.agent_messenger_governance import DEFAULT_HUMAN_TAKEOVER_PAUSE_MINUTES

            self._pause_minutes[binding_id] = DEFAULT_HUMAN_TAKEOVER_PAUSE_MINUTES
        self._human_active_until.pop(binding_id, None)
        self._tasks[binding_id] = asyncio.create_task(client.sync_forever(timeout=30000))
        self._renewals[binding_id] = asyncio.create_task(self._make_renewal_loop(binding_id)())
        logger.info("Agent matrix bot started: binding=%s subagent=%s mode=%s", binding_id, subagent_id, response_mode)

    def update_live_settings(self, binding_id: str, overrides: Optional[dict], response_mode: str) -> None:
        """Applies an edited prompt/mode to an already-running bot without restarting
        its sync loop — called from agent_messenger_governance.update_binding_settings."""
        if binding_id not in self._clients:
            return
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode

    async def stop(self, binding_id: str) -> None:
        task = self._tasks.pop(binding_id, None)
        renewal = self._renewals.pop(binding_id, None)
        client = self._clients.pop(binding_id, None)
        self._allowed_room_ids.pop(binding_id, None)
        self._overrides.pop(binding_id, None)
        self._response_modes.pop(binding_id, None)
        self._credentials.pop(binding_id, None)
        self._pause_minutes.pop(binding_id, None)
        self._human_active_until.pop(binding_id, None)
        if renewal:
            renewal.cancel()
        if task:
            task.cancel()
        if client:
            try:
                await client.close()
            except Exception:
                logger.exception("Agent matrix bot %s: error during shutdown", binding_id)
        logger.info("Agent matrix bot stopped: binding=%s", binding_id)

    async def start_all_active(self) -> None:
        from backend.agent_messenger_governance import list_matrix_bindings, resolve_matrix_binding_credentials

        for binding in list_matrix_bindings():
            if binding.get("status") != "active":
                continue
            credentials = resolve_matrix_binding_credentials(binding["id"])
            if not credentials:
                logger.warning(
                    "Agent matrix bot %s: active binding has no resolvable credentials, skipping", binding["id"]
                )
                continue
            overrides = {
                "system_prompt": binding.get("system_prompt_override"),
                "model": binding.get("model_override"),
                "model_provider": binding.get("model_provider_override"),
            }
            try:
                from backend.agent_messenger_governance import _clean_pause_minutes

                await self.start(
                    binding["id"], binding["subagent_id"], credentials, binding.get("allowed_chat_ids"),
                    overrides, binding.get("response_mode") or "draft",
                )
                self.set_human_takeover_pause(
                    binding["id"], _clean_pause_minutes(binding.get("human_takeover_pause_minutes"))
                )
            except Exception as exc:
                logger.exception("Failed to start agent matrix bot %s at startup", binding["id"])
                # Without this the binding stays 'active' in the DB/UI while its
                # bot never actually started — indistinguishable from "working
                # but silent" until someone reads the backend logs.
                from backend.agent_messenger_governance import mark_binding_failed

                await asyncio.to_thread(mark_binding_failed, binding["id"], str(exc))

    async def stop_all(self) -> None:
        for binding_id in list(self._clients.keys()):
            await self.stop(binding_id)


manager = AgentMatrixBotManager()
