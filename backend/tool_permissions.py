"""Principal-based tool policy — who is allowed to touch what.

Three principals, each strictly narrower than the one above it:

``owner``
    Vexa (the main ``jarvis`` agent) driven by the owner through the
    authenticated dashboard or the owner-gated Telegram bot (backend/bot.py's
    ``_is_allowed_chat``/``admin_only``). Keeps the full tool set, but the
    host-shell guard below still applies — "разрешения, но ограниченные".

``subagent``
    Every other agent in the network, on every internal path (orchestrator
    delegation, schedules, autonomy runs). Never receives a tool that acts on
    the *server itself* — no shell, no dev sandbox, no git, no capability/MCP
    provisioning, no agent creation. Those stay with Vexa.

``public_channel``
    A subagent answering a stranger who redeemed an access token on a messenger
    binding (backend/bot_access.py). Allowlist-only: read-only informational
    tools and nothing that reads or writes the owner's private data.

The policy is enforced in three independent places, so a bypass needs all three
to fail at once:

1. ``backend/agent.py`` filters the tool schema — the model never sees the tool;
2. ``backend/agent.py`` re-checks immediately before dispatch, in case the model
   invents a name that was filtered out;
3. ``backend/control_plane.execute_governed_tool`` refuses before it even
   creates a task, which also covers callers that bypass agent.py entirely.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, Optional, Set

OWNER = "owner"
SUBAGENT = "subagent"
PUBLIC_CHANNEL = "public_channel"

PRINCIPALS = (OWNER, SUBAGENT, PUBLIC_CHANNEL)

# The main agent. Only the owner talks to it (dashboard session auth, or the
# admin-gated Telegram bot) — messenger bindings and access tokens refuse to
# target these ids, see backend/bot_access.py and agent_messenger_governance.py.
MAIN_AGENT_IDS = frozenset({"jarvis", "vexa"})

# ── Tools that act on the server / the owner's infrastructure ────────────────
# Denied to every principal except the owner. These either execute code on the
# backend host, mutate the dev repo, provision new capabilities, or expose the
# machine's internals.
SERVER_ACTION_TOOLS: Set[str] = {
    "execute_command",
    "get_system_stats",
    "diagnose_capabilities",
    "dev_read_file",
    "dev_list_dir",
    "dev_write_file",
    "dev_patch",
    "dev_exec",
    "dev_run_tests",
    "dev_publish_demo",
    "dev_review_demo",
    "git_status",
    "git_diff",
    "git_commit",
    "git_push",
    "create_subagent",
    "request_capability",
    "request_mcp_connection",
    "sync_obsidian_vault",
    # Drives a real browser against arbitrary third-party sites with the
    # owner's egress — an action on infrastructure, not a lookup.
    "browser_task",
}

# ── Tools that read or write the owner's personal data ───────────────────────
# Allowed to internal subagents (they work for the owner) but never exposed on
# a public channel.
OWNER_PRIVATE_TOOLS: Set[str] = {
    "search_obsidian",
    "read_obsidian_note",
    "create_obsidian_note",
    "sync_obsidian_vault",
    "get_calendar_events",
    "add_calendar_event",
    "get_todoist_tasks",
    "add_todoist_task",
    "delete_todoist_task",
    "set_timer",
    "set_alarm",
    "cancel_timer_or_alarm",
    "set_recurring_reminder",
    "add_price_alert",
    "get_github_summary",
    "save_subagent_memory",
    "get_subagent_memory",
    "call_subagent",
    "list_subagents",
}

# ── What a token holder's agent may use ──────────────────────────────────────
# Read-only, non-personal, no side effects outside this conversation.
PUBLIC_CHANNEL_TOOLS: Set[str] = {
    "web_search",
    "get_current_time_israel",
    "get_weather",
    "get_market_prices",
    "get_rss_digest",
}

# Costs real money per call. A plan must name these explicitly in its
# ``allowed_tools`` before a token holder can trigger them; the per-token spend
# limit in bot_access.py is what actually caps the damage.
PUBLIC_CHANNEL_OPTIONAL_TOOLS: Set[str] = {
    "generate_image",
    "browser_read",
}

PUBLIC_CHANNEL_CEILING: Set[str] = PUBLIC_CHANNEL_TOOLS | PUBLIC_CHANNEL_OPTIONAL_TOOLS


def normalize_principal(principal: Optional[str]) -> str:
    """Unknown/absent principal is treated as ``subagent`` — the safe middle
    ground: an unattributed caller never gets host access, but internal paths
    that predate this module keep working."""
    value = (principal or "").strip().lower()
    return value if value in PRINCIPALS else SUBAGENT


def is_tool_allowed(
    principal: Optional[str],
    tool_name: str,
    *,
    plan_allowed_tools: Optional[Iterable[str]] = None,
) -> bool:
    """True if ``principal`` may run ``tool_name``.

    ``plan_allowed_tools`` (from an access plan) can only *narrow* the public
    ceiling — it never grants a tool the principal wouldn't otherwise have.
    """
    who = normalize_principal(principal)
    name = (tool_name or "").strip()

    if who == OWNER:
        return True

    if name in SERVER_ACTION_TOOLS:
        return False

    if who == SUBAGENT:
        return True

    # public_channel
    if name not in PUBLIC_CHANNEL_CEILING or name in OWNER_PRIVATE_TOOLS:
        return False
    if plan_allowed_tools is not None:
        return name in set(plan_allowed_tools)
    return name in PUBLIC_CHANNEL_TOOLS


def filter_tool_names(
    principal: Optional[str],
    tool_names: Iterable[str],
    *,
    plan_allowed_tools: Optional[Iterable[str]] = None,
) -> Set[str]:
    """The subset of ``tool_names`` this principal may see and run."""
    return {
        name
        for name in tool_names
        if is_tool_allowed(principal, name, plan_allowed_tools=plan_allowed_tools)
    }


def denial_payload(principal: Optional[str], tool_name: str) -> str:
    """JSON string handed back to the model in place of a tool result, so the
    turn continues with a clear explanation instead of failing."""
    who = normalize_principal(principal)
    if tool_name in SERVER_ACTION_TOOLS:
        reason = (
            "Server-side actions (shell, dev sandbox, git, capability provisioning) "
            "are reserved for the main agent operated by the owner."
        )
    elif who == PUBLIC_CHANNEL:
        reason = "This tool is not available on a public token-gated channel."
    else:
        reason = "This tool is not available to this agent."
    return json.dumps(
        {
            "status": "forbidden",
            "error": f"Tool '{tool_name}' is blocked by policy for principal '{who}'.",
            "reason": reason,
        },
        ensure_ascii=False,
    )


# ── The owner's own shell, kept "limited" ────────────────────────────────────
# execute_command stays available to Vexa (R4 → two owner approvals in the
# Control Plane), but a set of irreversible / host-wrecking shapes is refused
# outright, before any approval can be granted. Ordered most-specific first;
# the matched pattern name is what gets reported.
_SHELL_DENY_PATTERNS = (
    ("recursive delete of a root path", re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*[rR][a-zA-Z]*\s[^|;&]*(/|/etc|/var|/home|/usr|\*)\s*$")),
    ("filesystem format", re.compile(r"\bmkfs(\.\w+)?\b|\bmke2fs\b")),
    ("raw device write", re.compile(r"\bdd\b[^|;&]*\bof=\s*/dev/")),
    ("disk partitioning", re.compile(r"\b(fdisk|parted|sgdisk|wipefs)\b")),
    ("host power state change", re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b")),
    ("fork bomb", re.compile(r":\s*\(\s*\)\s*\{.*\|.*&.*\}\s*;?\s*:")),
    ("firewall flush", re.compile(r"\b(iptables|nft|ufw)\b.*\b(-F|flush|reset|disable)\b")),
    ("container/volume destruction", re.compile(r"\bdocker\b.*\b(system\s+prune|volume\s+rm|rm\s+-f)\b")),
    ("piping a remote script into a shell", re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k|da)?sh\b")),
    ("write to a system directory", re.compile(r"(>>?|\btee\b)\s*/(etc|boot|sys|proc|lib|usr/lib)/")),
    ("world-writable permissions on a system path", re.compile(r"\bchmod\b\s+(-[a-zA-Z]+\s+)*777\s+/(?!home|tmp)")),
    ("user/credential database change", re.compile(r"\b(passwd|useradd|usermod|userdel|visudo|chpasswd)\b")),
    ("privilege escalation", re.compile(r"\bsudo\b|\bsu\s+-")),
    ("systemd unit change", re.compile(r"\bsystemctl\b\s+(disable|mask|stop)\b")),
    ("SSH key or credential file access", re.compile(r"(~|/root|/home/[^/\s]+)/\.ssh/|/etc/(shadow|sudoers)")),
)

SHELL_TIMEOUT_SECONDS = 15


def check_shell_command(command: str) -> Optional[str]:
    """Returns a human-readable reason when a shell command is refused
    outright, or None when it may proceed to the normal approval flow."""
    text = (command or "").strip()
    if not text:
        return "Empty command."
    if len(text) > 4000:
        return "Command is too long to review safely (over 4000 characters)."
    for label, pattern in _SHELL_DENY_PATTERNS:
        if pattern.search(text):
            return f"Refused: {label}. This shape of command is blocked for the shell tool regardless of approval."
    return None
