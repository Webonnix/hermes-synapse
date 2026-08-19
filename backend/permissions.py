"""Dashboard RBAC for the commercial client module.

The dashboard has always run as a single owner principal (one session token,
full access). The clients module adds money to the dashboard, and money needs
an answer to "who may see this" that does not depend on the frontend hiding a
button — §63 of the spec: someone without `clients.financials.view` must not be
able to *fetch* a price, not merely not see one.

So: a permission registry, roles as named permission sets, and one enforcement
point (`require`) that every new endpoint calls. The owner role holds every
permission, and any session issued without an explicit role is the owner — the
existing login flow is therefore unchanged, while a narrower role is now
expressible and, crucially, enforced server-side.

Custom roles can be defined at runtime (stored in app_settings under
`dashboard_roles`), which is what keeps this from being a hardcoded two-role
system when more people get logins.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Set

logger = logging.getLogger("hermes.permissions")

# ── the registry ─────────────────────────────────────────────────────────────
CLIENTS_VIEW = "clients.view"
CLIENTS_CREATE = "clients.create"
CLIENTS_EDIT = "clients.edit"
CLIENTS_ARCHIVE = "clients.archive"
CLIENTS_AGENTS_VIEW = "clients.agents.view"
CLIENTS_AGENTS_CONNECT = "clients.agents.connect"
CLIENTS_AGENTS_DISCONNECT = "clients.agents.disconnect"
CLIENTS_BILLING_VIEW = "clients.billing.view"
CLIENTS_BILLING_EDIT = "clients.billing.edit"
CLIENTS_INVOICES_VIEW = "clients.invoices.view"
CLIENTS_INVOICES_CREATE = "clients.invoices.create"
CLIENTS_INVOICES_EDIT = "clients.invoices.edit"
CLIENTS_INVOICES_MARK_PAID = "clients.invoices.mark_paid"
CLIENTS_PAYMENTS_VIEW = "clients.payments.view"
CLIENTS_PAYMENTS_CREATE = "clients.payments.create"
CLIENTS_FINANCIALS_VIEW = "clients.financials.view"
SETTINGS_CURRENCY_VIEW = "settings.currency.view"
SETTINGS_CURRENCY_EDIT = "settings.currency.edit"

ALL_PERMISSIONS: tuple = (
    CLIENTS_VIEW, CLIENTS_CREATE, CLIENTS_EDIT, CLIENTS_ARCHIVE,
    CLIENTS_AGENTS_VIEW, CLIENTS_AGENTS_CONNECT, CLIENTS_AGENTS_DISCONNECT,
    CLIENTS_BILLING_VIEW, CLIENTS_BILLING_EDIT,
    CLIENTS_INVOICES_VIEW, CLIENTS_INVOICES_CREATE, CLIENTS_INVOICES_EDIT,
    CLIENTS_INVOICES_MARK_PAID,
    CLIENTS_PAYMENTS_VIEW, CLIENTS_PAYMENTS_CREATE,
    CLIENTS_FINANCIALS_VIEW,
    SETTINGS_CURRENCY_VIEW, SETTINGS_CURRENCY_EDIT,
)

OWNER_ROLE = "owner"

# Built-in roles. `owner` is special-cased in `permissions_for` (it holds
# everything, including permissions added after this file was written), so it
# is intentionally absent here.
BUILTIN_ROLES: Dict[str, tuple] = {
    # Runs the client book day to day: full commercial access, but the FX rates
    # the whole company's numbers depend on stay with the owner.
    "manager": (
        CLIENTS_VIEW, CLIENTS_CREATE, CLIENTS_EDIT, CLIENTS_ARCHIVE,
        CLIENTS_AGENTS_VIEW, CLIENTS_AGENTS_CONNECT, CLIENTS_AGENTS_DISCONNECT,
        CLIENTS_BILLING_VIEW, CLIENTS_BILLING_EDIT,
        CLIENTS_INVOICES_VIEW, CLIENTS_INVOICES_CREATE, CLIENTS_INVOICES_EDIT,
        CLIENTS_INVOICES_MARK_PAID,
        CLIENTS_PAYMENTS_VIEW, CLIENTS_PAYMENTS_CREATE,
        CLIENTS_FINANCIALS_VIEW,
        SETTINGS_CURRENCY_VIEW,
    ),
    # Support: sees who the clients are and whether their agents are healthy,
    # and no money at all — no prices, no MRR, no invoices.
    "operator": (
        CLIENTS_VIEW, CLIENTS_AGENTS_VIEW, CLIENTS_AGENTS_CONNECT,
        CLIENTS_AGENTS_DISCONNECT, SETTINGS_CURRENCY_VIEW,
    ),
    "viewer": (CLIENTS_VIEW, CLIENTS_AGENTS_VIEW, SETTINGS_CURRENCY_VIEW),
}

SETTING_CUSTOM_ROLES = "dashboard_roles"


class PermissionDenied(PermissionError):
    """Raised by `require`. The API layer turns this into 403."""

    def __init__(self, permission: str, role: str):
        self.permission = permission
        self.role = role
        super().__init__(f"Role '{role}' is missing permission '{permission}'")


def _custom_roles() -> Dict[str, List[str]]:
    """Roles defined at runtime, from app_settings. Unreadable or malformed
    config must not lock anyone out of the built-in roles, so it degrades to an
    empty map with a log line."""
    try:
        from backend.database import get_setting
        raw = get_setting(SETTING_CUSTOM_ROLES)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("dashboard_roles setting is not valid JSON; ignoring it")
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: Dict[str, List[str]] = {}
    for role, granted in parsed.items():
        if isinstance(granted, list):
            result[str(role)] = [str(item) for item in granted if str(item) in ALL_PERMISSIONS]
    return result


def permissions_for(role: Optional[str]) -> Set[str]:
    name = (role or OWNER_ROLE).strip() or OWNER_ROLE
    if name == OWNER_ROLE:
        return set(ALL_PERMISSIONS)
    custom = _custom_roles()
    if name in custom:
        return set(custom[name])
    return set(BUILTIN_ROLES.get(name, ()))


def has_permission(role: Optional[str], permission: str) -> bool:
    return permission in permissions_for(role)


def role_from_request(request) -> str:
    """The role behind an authenticated FastAPI request.

    The auth middleware has already validated the bearer token by the time any
    route runs, so this only has to map token → role.
    """
    from backend.auth import role_for_session
    header = request.headers.get("Authorization") if request is not None else None
    token = header.split(" ", 1)[1].strip() if header and header.startswith("Bearer ") else None
    return role_for_session(token)


def require(request, *required: str) -> str:
    """Enforcement point: every permission in `required` must be held, or the
    request is refused with 403. Returns the role, which callers pass along as
    the audit actor."""
    from fastapi import HTTPException
    role = role_from_request(request)
    granted = permissions_for(role)
    for permission in required:
        if permission not in granted:
            raise HTTPException(
                status_code=403,
                detail=f"Недостаточно прав: требуется «{permission}»",
            )
    return role


def actor_id(request) -> str:
    """Who to record in the audit log. Single-admin today, so the role name is
    the most specific identity available; when logins become per-user this is
    the one place that has to start returning a user id."""
    return role_from_request(request)


def can_view_financials(role: Optional[str]) -> bool:
    """True when this role may see money fields at all (§63). List endpoints
    strip amounts from their response rather than 403 the whole page — a
    support user should still see the client list, just without prices."""
    return has_permission(role, CLIENTS_FINANCIALS_VIEW)
