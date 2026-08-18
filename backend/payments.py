"""Crypto billing: invoices, the provider webhook, and automatic token issuance.

The manual half of access control lives in ``bot_access.py`` — the owner clicks
"выдать" and hands someone a token. This module is the automatic half: a
customer buys a purchasable plan, pays in crypto, and the token is issued,
subscribed and (when the sale started inside a bot chat) delivered and activated
without anyone touching the dashboard.

The flow, in one line each:

1. :func:`create_invoice` records a pending row and asks the provider for a
   hosted payment page;
2. the customer pays; the provider POSTs an IPN to ``/api/payments/webhook``;
3. :func:`handle_webhook` verifies the signature, and on a terminal *paid*
   status calls :func:`apply_paid_invoice`;
4. that issues a token + subscription (or renews an existing one) exactly once,
   and hands back a delivery instruction for the caller to message the buyer.

**Idempotency is the whole game here.** Providers retry IPNs, and a duplicate
delivery must never mint a second token: the invoice row's ``status`` is the
lock, flipped to ``paid`` in the same transaction that records ``token_id``.

Provider support is deliberately thin — one real integration (NOWPayments) and
a ``manual`` mode where the owner marks an invoice paid by hand after checking
their own wallet. Adding another provider means adding a class with
``create_invoice``/``verify_webhook``/``parse_webhook``, nothing else.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from backend import bot_access
from backend.database import DB_PATH

logger = logging.getLogger("hermes.payments")

INVOICE_STATUSES = ("pending", "paid", "expired", "failed")
PROVIDERS = ("manual", "nowpayments")

DEFAULT_INVOICE_TTL_HOURS = 24

# Settings keys (backend.database app_settings). Secrets go through the
# api_keys table instead — see NOWPAYMENTS_* below.
SETTING_PROVIDER = "payments_provider"
SETTING_PUBLIC_BASE_URL = "payments_public_base_url"
SETTING_SUCCESS_URL = "payments_success_url"

KEY_NOWPAYMENTS_API = "NOWPAYMENTS_API_KEY"
KEY_NOWPAYMENTS_IPN = "NOWPAYMENTS_IPN_SECRET"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _init_schema() -> None:
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS payment_invoices (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                provider_invoice_id TEXT,
                plan_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,
                subagent_id TEXT NOT NULL,
                subscription_id TEXT,
                token_id TEXT,
                amount_usd REAL NOT NULL,
                pay_currency TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                payment_url TEXT NOT NULL DEFAULT '',
                customer_ref TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT 'admin',
                origin_platform TEXT NOT NULL DEFAULT '',
                origin_chat_id TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT 'new',
                last_status TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                paid_at TEXT,
                expires_at TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_invoices_provider_id ON payment_invoices (provider_invoice_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_invoices_origin_chat ON payment_invoices (origin_chat_id, status)"
        )


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


# ── configuration ────────────────────────────────────────────────────────────

def get_config() -> dict[str, Any]:
    """Provider settings, with the two secrets reduced to booleans — this is
    read by the dashboard, which must never receive the values themselves."""
    from backend import database as db

    provider = db.get_setting(SETTING_PROVIDER) or "manual"
    return {
        "provider": provider if provider in PROVIDERS else "manual",
        "public_base_url": db.get_setting(SETTING_PUBLIC_BASE_URL) or "",
        "success_url": db.get_setting(SETTING_SUCCESS_URL) or "",
        "api_key_configured": bool(db.get_api_key(KEY_NOWPAYMENTS_API)),
        "ipn_secret_configured": bool(db.get_api_key(KEY_NOWPAYMENTS_IPN)),
        "providers": list(PROVIDERS),
    }


def set_config(
    provider: Optional[str] = None,
    public_base_url: Optional[str] = None,
    success_url: Optional[str] = None,
) -> dict[str, Any]:
    from backend import database as db

    if provider is not None:
        if provider not in PROVIDERS:
            raise ValueError(f"provider must be one of {PROVIDERS}")
        db.set_setting(SETTING_PROVIDER, provider)
    if public_base_url is not None:
        db.set_setting(SETTING_PUBLIC_BASE_URL, public_base_url.strip().rstrip("/"))
    if success_url is not None:
        db.set_setting(SETTING_SUCCESS_URL, success_url.strip())
    return get_config()


# ── provider: NOWPayments ────────────────────────────────────────────────────
# Verified against the official Node SDK and WooCommerce plugin: invoices are
# created with POST https://api.nowpayments.io/v1/invoice using an `x-api-key`
# header, and IPN callbacks carry an `x-nowpayments-sig` header holding an
# HMAC-SHA512 of the *key-sorted* JSON body, signed with the IPN secret.

NOWPAYMENTS_API_BASE = "https://api.nowpayments.io/v1"
NOWPAYMENTS_SIGNATURE_HEADER = "x-nowpayments-sig"

# Statuses that mean the money is really in. 'confirming'/'confirmed'/'sending'
# are in-flight and only update the invoice's last_status; 'partially_paid' is
# deliberately NOT here — an underpayment must be looked at by a human.
NOWPAYMENTS_PAID_STATUSES = {"finished"}
NOWPAYMENTS_FAILED_STATUSES = {"failed", "refunded", "expired"}


class _RawNumber:
    """Carries a JSON number's original literal through parse → re-serialise.

    ``json.loads`` would turn ``1.0`` into a float that Python re-serialises as
    ``"1.0"`` while JavaScript's ``JSON.stringify`` writes ``"1"`` — and the
    provider signs the JavaScript form. Keeping the literal makes our canonical
    string byte-identical to the one that was actually signed.
    """

    __slots__ = ("literal",)

    def __init__(self, literal: str) -> None:
        self.literal = literal


def _canonical_json(value: Any) -> str:
    """JSON.stringify-equivalent with object keys sorted, used for signing."""
    if isinstance(value, _RawNumber):
        return value.literal
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        inner = ",".join(
            f"{json.dumps(str(key), ensure_ascii=False)}:{_canonical_json(item)}"
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def verify_nowpayments_signature(raw_body: bytes, signature: str, ipn_secret: str) -> bool:
    if not signature or not ipn_secret:
        return False
    try:
        payload = json.loads(raw_body, parse_float=_RawNumber, parse_int=_RawNumber)
    except Exception:
        logger.warning("Payment webhook body was not valid JSON")
        return False
    expected = hmac.new(
        ipn_secret.encode("utf-8"), _canonical_json(payload).encode("utf-8"), hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


async def _nowpayments_create_invoice(invoice: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    from backend import database as db

    api_key = db.get_api_key(KEY_NOWPAYMENTS_API)
    if not api_key:
        raise ValueError(
            "NOWPayments API key is not configured — add NOWPAYMENTS_API_KEY in «Ключи API»."
        )
    config = get_config()
    if not config["public_base_url"]:
        raise ValueError(
            "Set the public base URL in billing settings — the provider needs a reachable "
            "callback address to confirm payments."
        )

    body = {
        "price_amount": round(float(invoice["amount_usd"]), 2),
        "price_currency": "usd",
        "pay_currency": invoice.get("pay_currency") or None,
        "order_id": invoice["id"],
        "order_description": f"{plan['name']} — доступ к боту",
        "ipn_callback_url": f"{config['public_base_url']}/api/payments/webhook",
        "success_url": config["success_url"] or f"{config['public_base_url']}/",
        "cancel_url": config["success_url"] or f"{config['public_base_url']}/",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{NOWPAYMENTS_API_BASE}/invoice",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=body,
        )
    if response.status_code >= 400:
        raise ValueError(f"NOWPayments rejected the invoice ({response.status_code}): {response.text[:300]}")
    data = response.json()
    return {"provider_invoice_id": str(data.get("id") or ""), "payment_url": data.get("invoice_url") or ""}


# ── invoices ─────────────────────────────────────────────────────────────────

def get_invoice(invoice_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute("SELECT * FROM payment_invoices WHERE id = ?", (invoice_id,)).fetchone()
    return _row_to_dict(row) if row else None


def find_invoice_by_provider_id(provider_invoice_id: str) -> Optional[dict[str, Any]]:
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM payment_invoices WHERE provider_invoice_id = ? ORDER BY created_at DESC LIMIT 1",
            (str(provider_invoice_id),),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_invoices(
    status: Optional[str] = None,
    binding_id: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    _init_schema()
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if binding_id:
        clauses.append("binding_id = ?")
        params.append(binding_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT * FROM payment_invoices{where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def find_pending_invoice_for_chat(binding_id: str, platform: str, chat_id: str) -> Optional[dict[str, Any]]:
    """So a customer who messages again mid-payment is handed the same link
    instead of a second invoice."""
    _init_schema()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM payment_invoices
             WHERE binding_id = ? AND origin_platform = ? AND origin_chat_id = ? AND status = 'pending'
             ORDER BY created_at DESC LIMIT 1
            """,
            (binding_id, platform, str(chat_id)),
        ).fetchone()
    if not row:
        return None
    invoice = _row_to_dict(row)
    expires = invoice.get("expires_at")
    if expires and expires <= _now():
        _set_status(invoice["id"], "expired")
        return None
    return invoice


async def create_invoice(
    plan_id: str,
    binding_id: str,
    subagent_id: str,
    customer_ref: str = "",
    pay_currency: str = "",
    origin: str = "admin",
    origin_platform: str = "",
    origin_chat_id: str = "",
    purpose: str = "new",
    subscription_id: Optional[str] = None,
) -> dict[str, Any]:
    """Records a pending invoice and, for a real provider, fetches its hosted
    payment page. ``purpose`` is 'new' for a first purchase or 'renewal' for
    topping up an existing subscription."""
    _init_schema()
    plan = bot_access.get_plan(plan_id)
    if not plan:
        raise KeyError(f"Unknown plan: {plan_id}")
    if not plan["is_active"]:
        raise ValueError("This plan is disabled")
    if not plan.get("price_usd"):
        raise ValueError("This plan has no price — set one before selling it")
    if plan.get("subagent_id") and plan["subagent_id"] != subagent_id:
        raise ValueError(
            f"Plan '{plan['name']}' is assigned to a different agent and cannot be sold for '{subagent_id}'."
        )

    from backend.tool_permissions import MAIN_AGENT_IDS

    if subagent_id in MAIN_AGENT_IDS:
        raise ValueError("The main agent is never sold — Vexa answers to the owner only.")

    config = get_config()
    invoice_id = f"inv_{uuid.uuid4().hex[:16]}"
    now = _now()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=DEFAULT_INVOICE_TTL_HOURS)).isoformat(
        timespec="seconds"
    )
    invoice = {
        "id": invoice_id,
        "provider": config["provider"],
        "amount_usd": float(plan["price_usd"]),
        "pay_currency": (pay_currency or "").strip().lower(),
    }

    provider_invoice_id, payment_url = "", ""
    if config["provider"] == "nowpayments":
        created = await _nowpayments_create_invoice(invoice, plan)
        provider_invoice_id = created["provider_invoice_id"]
        payment_url = created["payment_url"]

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO payment_invoices
                (id, provider, provider_invoice_id, plan_id, binding_id, subagent_id, subscription_id,
                 amount_usd, pay_currency, status, payment_url, customer_ref, origin, origin_platform,
                 origin_chat_id, purpose, created_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invoice_id, config["provider"], provider_invoice_id, plan_id, binding_id, subagent_id,
                subscription_id, invoice["amount_usd"], invoice["pay_currency"], payment_url,
                customer_ref or "", origin, origin_platform, str(origin_chat_id or ""), purpose,
                now, now, expires_at,
            ),
        )
    logger.info(
        "Invoice %s created: plan=%s amount=%.2f provider=%s origin=%s",
        invoice_id, plan_id, invoice["amount_usd"], config["provider"], origin,
    )
    return get_invoice(invoice_id)  # type: ignore[return-value]


def _set_status(invoice_id: str, status: str, last_status: str = "") -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE payment_invoices SET status = ?, last_status = ?, updated_at = ? WHERE id = ?",
            (status, last_status, _now(), invoice_id),
        )


def cancel_invoice(invoice_id: str) -> dict[str, Any]:
    invoice = get_invoice(invoice_id)
    if not invoice:
        raise KeyError(invoice_id)
    if invoice["status"] == "paid":
        raise ValueError("A paid invoice cannot be canceled — refund it with the provider instead")
    _set_status(invoice_id, "failed", "canceled by owner")
    return get_invoice(invoice_id)  # type: ignore[return-value]


# ── the part that actually grants access ─────────────────────────────────────

def apply_paid_invoice(invoice_id: str) -> dict[str, Any]:
    """Issues or renews access for a paid invoice, exactly once.

    Returns a delivery instruction::

        {"invoice": ..., "token": ..., "subscription": ..., "plaintext": ...,
         "already_applied": bool, "deliver_to": {...} | None}

    ``plaintext`` is present only on the *first* successful application — it is
    the one moment the token exists in readable form. A retried webhook gets
    ``already_applied: True`` and no plaintext, which is exactly right: the
    customer already received it.
    """
    _init_schema()
    invoice = get_invoice(invoice_id)
    if not invoice:
        raise KeyError(invoice_id)

    # The claim: flip pending → paid in one statement and check we were the one
    # who moved it. A concurrent retry loses the race and returns early, so no
    # second token is ever minted.
    now = _now()
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE payment_invoices SET status = 'paid', paid_at = ?, updated_at = ? "
            "WHERE id = ? AND status != 'paid'",
            (now, now, invoice_id),
        )
        claimed = cursor.rowcount > 0
    if not claimed:
        applied = get_invoice(invoice_id)
        return {
            "invoice": applied,
            "token": bot_access.get_token(applied["token_id"]) if applied.get("token_id") else None,
            "subscription": (
                bot_access.get_subscription(applied["subscription_id"])
                if applied.get("subscription_id") else None
            ),
            "plaintext": None,
            "already_applied": True,
            "deliver_to": None,
        }

    plan = bot_access.get_plan(invoice["plan_id"])
    if not plan:
        _set_status(invoice_id, "failed", "plan no longer exists")
        raise KeyError(f"Plan {invoice['plan_id']} no longer exists")

    plaintext: Optional[str] = None
    if invoice["purpose"] == "renewal" and invoice.get("subscription_id"):
        subscription = bot_access.renew_subscription(invoice["subscription_id"])
        token = bot_access.get_token(subscription["token_id"])
    else:
        issued = bot_access.issue_token(
            binding_id=invoice["binding_id"],
            subagent_id=invoice["subagent_id"],
            plan_id=plan["id"],
            label=invoice.get("customer_ref") or f"оплачен {invoice['id']}",
            notes=f"Выдан автоматически по счёту {invoice['id']}",
        )
        plaintext = issued.pop("plaintext", None)
        token = bot_access.get_token(issued["id"])
        subscription = bot_access.create_subscription(
            plan=plan,
            token_id=issued["id"],
            binding_id=invoice["binding_id"],
            subagent_id=invoice["subagent_id"],
            customer_ref=invoice.get("customer_ref") or "",
        )

    with _connect() as connection:
        connection.execute(
            "UPDATE payment_invoices SET token_id = ?, subscription_id = ?, updated_at = ? WHERE id = ?",
            (token["id"], subscription["id"], _now(), invoice_id),
        )

    # A sale that started inside a bot chat binds the new token to that chat
    # straight away, so the buyer never has to paste anything.
    deliver_to = None
    if invoice["origin"] == "bot" and invoice["origin_chat_id"] and plaintext:
        try:
            bot_access.redeem(
                invoice["binding_id"], invoice["origin_platform"], invoice["origin_chat_id"],
                plaintext, invoice["origin_chat_id"], invoice.get("customer_ref") or "",
            )
        except bot_access.RedemptionError as exc:
            logger.warning("Auto-redeem after payment failed for %s: %s", invoice_id, exc)
        deliver_to = {
            "binding_id": invoice["binding_id"],
            "platform": invoice["origin_platform"],
            "chat_id": invoice["origin_chat_id"],
        }

    logger.info(
        "Invoice %s applied: token=%s subscription=%s purpose=%s",
        invoice_id, token["id"], subscription["id"], invoice["purpose"],
    )
    return {
        "invoice": get_invoice(invoice_id),
        "token": token,
        "subscription": subscription,
        "plaintext": plaintext,
        "already_applied": False,
        "deliver_to": deliver_to,
    }


def mark_paid_manually(invoice_id: str) -> dict[str, Any]:
    """The owner confirming they saw the transfer in their own wallet. Runs the
    same issuance path a provider webhook would."""
    return apply_paid_invoice(invoice_id)


# ── webhook ──────────────────────────────────────────────────────────────────

def handle_webhook(raw_body: bytes, headers: dict[str, str]) -> dict[str, Any]:
    """Verifies and applies a provider callback.

    Returns ``{"ok": bool, "detail": str, "result": ... | None}``. Never raises
    on bad input — a webhook endpoint that 500s on a malformed body invites the
    provider to retry forever.
    """
    from backend import database as db

    config = get_config()
    if config["provider"] != "nowpayments":
        return {"ok": False, "detail": "No webhook provider is configured", "result": None}

    ipn_secret = db.get_api_key(KEY_NOWPAYMENTS_IPN)
    if not ipn_secret:
        logger.error("Payment webhook received but NOWPAYMENTS_IPN_SECRET is not configured")
        return {"ok": False, "detail": "Webhook secret is not configured", "result": None}

    lowered = {str(key).lower(): value for key, value in headers.items()}
    signature = lowered.get(NOWPAYMENTS_SIGNATURE_HEADER, "")
    if not verify_nowpayments_signature(raw_body, signature, ipn_secret):
        logger.warning("Rejected a payment webhook with a bad signature")
        return {"ok": False, "detail": "Invalid signature", "result": None}

    try:
        payload = json.loads(raw_body)
    except Exception:
        return {"ok": False, "detail": "Malformed body", "result": None}

    status = str(payload.get("payment_status") or "").lower()
    order_id = str(payload.get("order_id") or "")
    invoice = get_invoice(order_id) if order_id else None
    if not invoice and payload.get("invoice_id"):
        invoice = find_invoice_by_provider_id(str(payload["invoice_id"]))
    if not invoice:
        logger.warning("Payment webhook for an unknown invoice (order_id=%s)", order_id)
        return {"ok": False, "detail": "Unknown invoice", "result": None}

    if status in NOWPAYMENTS_PAID_STATUSES:
        result = apply_paid_invoice(invoice["id"])
        return {"ok": True, "detail": "applied", "result": result}

    if status in NOWPAYMENTS_FAILED_STATUSES:
        if invoice["status"] != "paid":
            _set_status(invoice["id"], "failed", status)
        return {"ok": True, "detail": f"recorded {status}", "result": None}

    # waiting / confirming / confirmed / sending / partially_paid — money is not
    # (fully) in yet, so nothing is granted; the status is kept for the admin view.
    with _connect() as connection:
        connection.execute(
            "UPDATE payment_invoices SET last_status = ?, updated_at = ? WHERE id = ?",
            (status, _now(), invoice["id"]),
        )
    return {"ok": True, "detail": f"noted {status}", "result": None}


# ── reporting ────────────────────────────────────────────────────────────────

def revenue_overview() -> dict[str, Any]:
    _init_schema()
    with _connect() as connection:
        totals = connection.execute(
            """
            SELECT COUNT(*) AS invoices,
                   SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid,
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                   COALESCE(SUM(CASE WHEN status = 'paid' THEN amount_usd ELSE 0 END), 0) AS revenue_usd
              FROM payment_invoices
            """
        ).fetchone()
    subscriptions = bot_access.list_subscriptions()
    active = [item for item in subscriptions if item["status"] == "active"]
    mrr = 0.0
    for item in active:
        plan = bot_access.get_plan(item["plan_id"])
        days = (plan or {}).get("duration_days")
        if days and item.get("price_usd"):
            mrr += float(item["price_usd"]) * (30.0 / float(days))
    return {
        "invoices": dict(totals),
        "subscriptions": {
            "total": len(subscriptions),
            "active": len(active),
            "expired": len([i for i in subscriptions if i["status"] == "expired"]),
            "canceled": len([i for i in subscriptions if i["status"] == "canceled"]),
        },
        "mrr_usd": round(mrr, 2),
        "config": get_config(),
    }


_init_schema()
