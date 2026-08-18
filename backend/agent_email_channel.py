"""Per-agent email channel runtime: polls one mailbox over IMAP, drafts (or
auto-sends) replies over SMTP. Same shape as every other channel manager here
(agent_bot.py, agent_matrix_bot.py, agent_discord_bot.py, agent_slack_bot.py):
one manager, per-binding state, response_mode governs draft-queue vs
auto-labeled send. See backend/agent_messenger_governance.py for the
governance/approval side.

Unlike the chat platforms, an inbox has no built-in concept of "who's allowed
to talk to me" — every stranger's newsletter lands in the same place a real
person's email does. So this module is stricter by default: bulk/automated
mail (anything carrying List-Unsubscribe or Precedence: bulk/junk) is always
skipped, never drafted, never auto-replied — regardless of the allowed_senders
list.
"""

from __future__ import annotations

import asyncio
import email
import email.header
import email.utils
import imaplib
import logging
import smtplib
from email.message import EmailMessage
from typing import Dict, List, Optional

logger = logging.getLogger("hermes.agent_email_channel")

POLL_INTERVAL_SECONDS = 60


def _decode_header_value(raw: Optional[str]) -> str:
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
        return "".join(
            part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
            for part, enc in parts
        )
    except Exception:
        return raw


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="replace").strip()
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", errors="replace").strip()
    except Exception:
        return ""


def _looks_like_bulk_mail(msg: email.message.Message) -> bool:
    if msg.get("List-Unsubscribe"):
        return True
    precedence = (msg.get("Precedence") or "").strip().lower()
    if precedence in {"bulk", "junk", "list"}:
        return True
    if (msg.get("Auto-Submitted") or "").strip().lower() not in ("", "no"):
        return True
    return False


def _sender_address(msg: email.message.Message) -> str:
    _, address = email.utils.parseaddr(msg.get("From", ""))
    return address.lower()


class AgentEmailChannelManager:
    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}
        self._allowed_senders: Dict[str, List[str]] = {}
        self._overrides: Dict[str, dict] = {}
        self._response_modes: Dict[str, str] = {}

    def _fetch_unseen(self, credentials: dict) -> List[email.message.Message]:
        """Blocking IMAP call — run via asyncio.to_thread. Fetches with
        BODY.PEEK so nothing is marked \\Seen until we've actually queued or
        replied to it (a crash mid-poll shouldn't silently lose a message)."""
        messages: List[email.message.Message] = []
        imap = imaplib.IMAP4_SSL(credentials["imap_host"], int(credentials["imap_port"]))
        try:
            imap.login(credentials["address"], credentials["password"])
            imap.select("INBOX")
            status, data = imap.search(None, "UNSEEN")
            if status != "OK":
                return messages
            for num in (data[0].split() if data and data[0] else []):
                status, msg_data = imap.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                msg["_imap_uid"] = num.decode() if isinstance(num, bytes) else str(num)
                messages.append(msg)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        return messages

    def _mark_seen(self, credentials: dict, uid: str) -> None:
        imap = imaplib.IMAP4_SSL(credentials["imap_host"], int(credentials["imap_port"]))
        try:
            imap.login(credentials["address"], credentials["password"])
            imap.select("INBOX")
            imap.store(uid, "+FLAGS", "\\Seen")
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _send_smtp(self, credentials: dict, to_address: str, subject: str, body: str,
                    in_reply_to: str = "", references: str = "") -> None:
        message = EmailMessage()
        message["From"] = credentials["address"]
        message["To"] = to_address
        message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
        message.set_content(body)

        smtp = smtplib.SMTP(credentials["smtp_host"], int(credentials["smtp_port"]), timeout=15)
        try:
            smtp.starttls()
            smtp.login(credentials["address"], credentials["password"])
            smtp.send_message(message)
        finally:
            smtp.quit()

    async def _poll_loop(self, binding_id: str, subagent_id: str, credentials: dict) -> None:
        while True:
            try:
                messages = await asyncio.to_thread(self._fetch_unseen, credentials)
                for msg in messages:
                    await self._handle_message(binding_id, subagent_id, credentials, msg)
            except Exception:
                logger.exception("Agent email channel %s: poll cycle failed", binding_id)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _handle_message(self, binding_id: str, subagent_id: str, credentials: dict, msg: email.message.Message) -> None:
        uid = msg.get("_imap_uid", "")
        if _looks_like_bulk_mail(msg):
            await asyncio.to_thread(self._mark_seen, credentials, uid)
            return

        sender = _sender_address(msg)
        body = _extract_body(msg)
        if not body:
            await asyncio.to_thread(self._mark_seen, credentials, uid)
            return

        subject = _decode_header_value(msg.get("Subject", ""))
        message_id = msg.get("Message-ID", "")
        references = msg.get("References", "")

        from backend import bot_access_gate

        # The subject line matters here: people paste an access token into the
        # subject as often as into the body, so the gate sees both.
        decision = await asyncio.to_thread(
            bot_access_gate.authorize, binding_id, "email", sender, f"{subject}\n{body}", sender, sender,
        )
        if decision.action == bot_access_gate.IGNORE:
            logger.info("Agent email channel %s: ignoring message from %s", binding_id, sender)
            return  # left unseen deliberately — an owner reviewing the inbox should still notice it
        if decision.action == bot_access_gate.REPLY:
            try:
                await asyncio.to_thread(
                    self._send_smtp, credentials, sender, subject, decision.reply_text, message_id, references,
                )
                await asyncio.to_thread(self._mark_seen, credentials, uid)
            except Exception:
                logger.exception("Agent email channel %s: failed to send the access reply", binding_id)
            return

        from backend.database import get_subagent

        subagent = get_subagent(subagent_id)
        if not subagent or not subagent.get("is_enabled"):
            return  # leave unseen; nothing sensible to do while the agent is disabled

        from backend.agent_messenger_governance import apply_binding_overrides
        subagent = apply_binding_overrides(subagent, self._overrides.get(binding_id))
        subagent = bot_access_gate.apply_subscriber_context(subagent, decision)

        from backend.agent import agent_instance

        session_id = decision.session_id or f"emailchannel:{binding_id}:{sender}"
        try:
            response_text = await agent_instance._respond_as_subagent(body, subagent, chat_id=session_id)
        except Exception:
            logger.exception("Agent email channel %s: error generating reply", binding_id)
            bot_access_gate.record_turn(decision, agent_instance.last_run_metadata.get(session_id), "")
            return
        bot_access_gate.record_turn(
            decision, agent_instance.last_run_metadata.get(session_id), response_text
        )

        mode = bot_access_gate.effective_response_mode(
            decision, self._response_modes.get(binding_id, "draft")
        )
        if mode == "auto_labeled":
            from backend.agent_messenger_governance import auto_reply_disclosure
            try:
                await asyncio.to_thread(
                    self._send_smtp, credentials, sender, subject, response_text + auto_reply_disclosure(binding_id),
                    message_id, references,
                )
            except Exception:
                logger.exception("Agent email channel %s: failed to send auto-reply", binding_id)
                return
            await asyncio.to_thread(self._mark_seen, credentials, uid)
            return

        import json
        from backend.channel_replies import create_pending_reply
        create_pending_reply(
            binding_id=binding_id,
            platform="email",
            subagent_id=subagent_id,
            chat_id=json.dumps({"to": sender, "subject": subject, "message_id": message_id, "references": references}),
            incoming_text=body[:2000],
            drafted_reply=response_text,
            incoming_from=sender,
        )
        await asyncio.to_thread(self._mark_seen, credentials, uid)

    async def start(
        self, binding_id: str, subagent_id: str, credentials: dict,
        allowed_senders: Optional[List[str]] = None, overrides: Optional[dict] = None,
        response_mode: str = "draft",
    ) -> None:
        if binding_id in self._tasks:
            return
        self._allowed_senders[binding_id] = [s.lower() for s in (allowed_senders or [])]
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode
        self._tasks[binding_id] = asyncio.create_task(self._poll_loop(binding_id, subagent_id, credentials))
        logger.info("Agent email channel started: binding=%s subagent=%s mode=%s", binding_id, subagent_id, response_mode)

    def update_live_settings(self, binding_id: str, overrides: Optional[dict], response_mode: str) -> None:
        if binding_id not in self._tasks:
            return
        self._overrides[binding_id] = overrides or {}
        self._response_modes[binding_id] = response_mode

    async def stop(self, binding_id: str) -> None:
        task = self._tasks.pop(binding_id, None)
        self._allowed_senders.pop(binding_id, None)
        self._overrides.pop(binding_id, None)
        self._response_modes.pop(binding_id, None)
        if task:
            task.cancel()
        logger.info("Agent email channel stopped: binding=%s", binding_id)

    async def start_all_active(self) -> None:
        from backend.agent_messenger_governance import list_email_bindings, resolve_email_binding_credentials

        for binding in list_email_bindings():
            if binding.get("status") != "active":
                continue
            credentials = resolve_email_binding_credentials(binding["id"])
            if not credentials:
                logger.warning("Agent email channel %s: active binding has no resolvable credentials, skipping", binding["id"])
                continue
            overrides = {
                "system_prompt": binding.get("system_prompt_override"),
                "model": binding.get("model_override"),
                "model_provider": binding.get("model_provider_override"),
            }
            try:
                await self.start(
                    binding["id"], binding["subagent_id"], credentials, binding.get("allowed_chat_ids"),
                    overrides, binding.get("response_mode") or "draft",
                )
            except Exception:
                logger.exception("Failed to start agent email channel %s at startup", binding["id"])

    async def stop_all(self) -> None:
        for binding_id in list(self._tasks.keys()):
            await self.stop(binding_id)


manager = AgentEmailChannelManager()
