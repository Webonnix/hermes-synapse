"""Scout: turn the approved sources into stored, deduplicated candidates.

Facts come from the collector, never from a model: URL, title, text, labels,
the source's own state and timestamps, and the time we fetched them. A reward
is recorded only as text the source itself contains (``claimed_reward``) — it
is a claim, not a commitment, and nothing here can upgrade it.

A source that fails is reported in the run (``sources_report``) and the run
continues with the others; it never invents a replacement.
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

from backend.earner import budget, egress, store

logger = logging.getLogger("hermes.earner.scout")

EXCERPT_CHARS = 4000

DEMO_URL = "https://example.invalid/hermes-earner-demo/csv-column-stats"
DEMO_CANDIDATE = {
    "url": DEMO_URL,
    "title": "[ДЕМО] Функция статистики по столбцу CSV (Python, stdlib)",
    "excerpt": (
        "Учебная задача для проверки конвейера Hermes Earner. Это НЕ реальный заказ: заказчика нет, "
        "за неё никто не платит.\n\n"
        "Нужно: модуль csvstats.py с функцией column_stats(csv_text: str, column: str) -> dict. "
        "Функция разбирает CSV модулем csv (первая строка — заголовок), берёт значения указанного "
        "столбца, пропускает пустые ячейки и возвращает словарь с ключами count, min, max, mean, median. "
        "Нечисловое значение — ValueError с номером строки. Неизвестный столбец — KeyError. "
        "Если чисел нет — count=0, остальные значения None.\n\n"
        "Критерии приёмки: только стандартная библиотека Python 3.11; тесты unittest покрывают обычный "
        "случай, пустые ячейки, нечисловое значение, неизвестный столбец и пустой столбец; README.md с "
        "примером использования."
    ),
    "labels": ["demo", "python", "stdlib"],
    "claimed_reward": "",
    "source_state": "demo",
    "source_created_at": "",
    "is_demo": 1,
}

_CLAIM = re.compile(
    r"(?:US\$|\$|€|£)\s?\d[\d,]*(?:\.\d+)?\s?[kK]?\b"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:USD|USDC|USDT|EUR|GBP|UAH|RUB)\b"
    r"|\b\d{1,3}(?:[ \u00a0]\d{3})+(?:\s?(?:₽|руб\.?|рублей|грн))|\b\d+\s?(?:₽|руб\.?|рублей|грн)", re.I)


def clean(text: Any, limit: int) -> str:
    from backend.agent_learning import clean_untrusted_text

    return clean_untrusted_text(str(text or ""), limit)


def claimed_reward(*texts: str) -> str:
    found: List[str] = []
    for text in texts:
        for match in _CLAIM.findall(text or ""):
            value = " ".join(match.split())
            if value not in found:
                found.append(value)
            if len(found) >= 3:
                return ", ".join(found)
    return ", ".join(found)


def parse_github(body: bytes, limit: int) -> List[Dict[str, Any]]:
    data = json.loads(body.decode("utf-8", errors="replace"))
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("ответ GitHub без списка items")
    result = []
    for item in items:
        if not isinstance(item, dict) or "pull_request" in item:
            continue
        url = str(item.get("html_url") or "")
        if not url.startswith("https://github.com/") or "/issues/" not in url:
            continue
        title = clean(item.get("title"), 300)
        body_text = clean(item.get("body"), EXCERPT_CHARS)
        labels = [clean((label or {}).get("name"), 50) for label in (item.get("labels") or [])
                  if isinstance(label, dict)][:10]
        result.append({
            "url": url[:500], "title": title or url, "excerpt": body_text, "labels": labels,
            "claimed_reward": claimed_reward(title, " ".join(labels), body_text),
            "source_state": str(item.get("state") or "")[:20],
            "source_created_at": str(item.get("created_at") or "")[:40],
            "is_demo": 0,
        })
        if len(result) >= limit:
            break
    return result


def parse_feed(body: bytes, limit: int) -> List[Dict[str, Any]]:
    head = body[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in body.lower():
        # Entity declarations are how XML bombs and external-entity reads start;
        # a news feed has no use for them.
        raise ValueError("лента содержит DOCTYPE/ENTITY — не разбирается")
    root = ET.fromstring(body)
    atom = "{http://www.w3.org/2005/Atom}"
    entries: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        entries.append({"title": item.findtext("title"), "url": item.findtext("link"),
                        "text": item.findtext("description"), "date": item.findtext("pubDate")})
    for entry in root.iter(f"{atom}entry"):
        link = entry.find(f"{atom}link")
        entries.append({"title": entry.findtext(f"{atom}title"),
                        "url": link.get("href") if link is not None else "",
                        "text": entry.findtext(f"{atom}summary") or entry.findtext(f"{atom}content"),
                        "date": entry.findtext(f"{atom}updated")})
    result = []
    for entry in entries:
        url = str(entry.get("url") or "").strip()
        if urlparse(url).scheme != "https":
            continue
        title = clean(entry.get("title"), 300)
        text = clean(entry.get("text"), EXCERPT_CHARS)
        result.append({"url": url[:500], "title": title or url, "excerpt": text, "labels": [],
                       "claimed_reward": claimed_reward(title, text), "source_state": "",
                       "source_created_at": str(entry.get("date") or "")[:40], "is_demo": 0})
        if len(result) >= limit:
            break
    return result


def parse_freelancer(body: bytes, limit: int) -> List[Dict[str, Any]]:
    """Freelancer.com public projects API (fixed-price, active)."""
    data = json.loads(body.decode("utf-8", errors="replace"))
    projects = ((data.get("result") or {}).get("projects")) if isinstance(data, dict) else None
    if not isinstance(projects, list):
        raise ValueError("ответ Freelancer.com без списка projects")
    result = []
    for item in projects:
        if not isinstance(item, dict) or item.get("status") != "active":
            continue
        seo = str(item.get("seo_url") or "")
        if not re.match(r"^[A-Za-z0-9_\-/]{3,200}$", seo):
            continue
        budget = item.get("budget") or {}
        currency = ((item.get("currency") or {}).get("code") or "")[:5]
        low, high = budget.get("minimum"), budget.get("maximum")
        reward = f"{low:g}–{high:g} {currency}".strip() if isinstance(low, (int, float)) and isinstance(high, (int, float)) else ""
        title = clean(item.get("title"), 300)
        result.append({"url": f"https://www.freelancer.com/projects/{seo}", "title": title or seo,
                       "excerpt": clean(item.get("description") or item.get("preview_description"), EXCERPT_CHARS),
                       "labels": [str(item.get("type") or "")[:20]], "claimed_reward": reward,
                       "source_state": "open", "source_created_at": str(item.get("time_submitted") or "")[:40],
                       "is_demo": 0})
        if len(result) >= limit:
            break
    return result


def parse_freelancehunt(body: bytes, limit: int) -> List[Dict[str, Any]]:
    """Freelancehunt public API v2 (JSON:API)."""
    data = json.loads(body.decode("utf-8", errors="replace"))
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("ответ Freelancehunt без списка data")
    result = []
    for item in items:
        attributes = (item or {}).get("attributes") or {}
        url = str((((item or {}).get("links") or {}).get("self") or {}).get("web") or "")
        if not url.startswith("https://freelancehunt.com/"):
            continue
        status = (attributes.get("status") or {}).get("id")
        budget = attributes.get("budget") or {}
        reward = (f"{budget.get('amount')} {str(budget.get('currency') or '')[:5]}"
                  if isinstance(budget.get("amount"), (int, float)) else "")
        result.append({"url": url[:500], "title": clean(attributes.get("name"), 300) or url,
                       "excerpt": clean(attributes.get("description"), EXCERPT_CHARS),
                       "labels": [clean(skill.get("name"), 50) for skill in attributes.get("skills") or []
                                  if isinstance(skill, dict)][:8],
                       "claimed_reward": reward, "source_state": "open" if status == 11 else "closed",
                       "source_created_at": str(attributes.get("published_at") or "")[:40], "is_demo": 0})
        if len(result) >= limit:
            break
    return result


_GITHUB_ISSUE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})/issues/(\d{1,9})$")


def parse_opire(body: bytes, limit: int) -> List[Dict[str, Any]]:
    """Opire rewards: GitHub issues with a reward someone committed to on Opire."""
    data = json.loads(body.decode("utf-8", errors="replace"))
    if not isinstance(data, list):
        raise ValueError("ответ Opire не список")
    result = []
    for item in data:
        url = str((item or {}).get("url") or "")
        if (item or {}).get("platform") != "GitHub" or not _GITHUB_ISSUE.match(url):
            continue
        price = (item.get("pendingPrice") or {})
        reward = (f"${price['value'] / 100:g}" if price.get("unit") == "USD_CENT" and isinstance(price.get("value"), int)
                  else "")
        result.append({"url": url, "title": clean(item.get("title"), 300) or url, "excerpt": "",
                       "labels": ["opire"] + [clean(lang, 30) for lang in item.get("programmingLanguages") or []][:5],
                       "claimed_reward": reward, "source_state": "open", "source_created_at": "", "is_demo": 0})
        if len(result) >= limit:
            break
    return result


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,150}$")
SUPERTEAM_AGENT_ACCESS = ("AGENT_ALLOWED", "AGENT_ONLY")


def _superteam_reward(item: Dict[str, Any]) -> str:
    token = re.sub(r"[^A-Za-z0-9]", "", str(item.get("token") or ""))[:10]
    amount = item.get("rewardAmount")
    if isinstance(amount, (int, float)) and amount > 0:
        return f"{amount:g} {token}".strip()
    low, high = item.get("minRewardAsk"), item.get("maxRewardAsk")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and high > 0:
        return f"{low:g}–{high:g} {token}".strip()
    return ""


def parse_superteam(body: bytes, limit: int, types: List[str]) -> List[Dict[str, Any]]:
    """Superteam Earn Agent API (GET /api/agents/listings/live). The server
    already returns only listings open to agents; we check it again."""
    data = json.loads(body.decode("utf-8", errors="replace"))
    if not isinstance(data, list):
        raise ValueError("ответ Superteam Earn не список")
    result = []
    for item in data:
        if not isinstance(item, dict) or item.get("agentAccess") not in SUPERTEAM_AGENT_ACCESS:
            continue
        slug = str(item.get("slug") or "")
        if not _SLUG.match(slug) or item.get("type") not in types or item.get("status") != "OPEN":
            continue
        sponsor = item.get("sponsor") if isinstance(item.get("sponsor"), dict) else {}
        result.append({
            "url": f"https://{store.SUPERTEAM_HOST}/earn/listing/{slug}", "title": clean(item.get("title"), 300) or slug,
            "excerpt": "", "slug": slug,
            "labels": [str(item.get("type"))[:20], str(item.get("agentAccess"))[:20],
                       clean(sponsor.get("name"), 60)][:3],
            "claimed_reward": _superteam_reward(item), "source_state": "open",
            "source_created_at": str(item.get("deadline") or "")[:40], "is_demo": 0,
        })
        if len(result) >= limit:
            break
    return result


def superteam_details_text(body: bytes) -> str:
    data = json.loads(body.decode("utf-8", errors="replace"))
    if not isinstance(data, dict) or data.get("agentAccess") not in SUPERTEAM_AGENT_ACCESS:
        return ""
    parts = [str(data.get("description") or ""), str(data.get("requirements") or "")]
    skills = data.get("skills")
    if isinstance(skills, list):
        names = [str((s or {}).get("skills") or "") if isinstance(s, dict) else str(s) for s in skills][:10]
        parts.append("Skills: " + ", ".join(n for n in names if n))
    if data.get("deadline"):
        parts.append(f"Deadline: {data['deadline']}")
    return clean("\n\n".join(p for p in parts if p), EXCERPT_CHARS)


def _superteam_key() -> str:
    from backend import database

    return database.get_api_key("EARNER_SUPERTEAM_API_KEY") or ""


def github_issue_api(url: str) -> Optional[str]:
    match = _GITHUB_ISSUE.match(url)
    return f"https://api.github.com/repos/{match.group(1)}/{match.group(2)}/issues/{match.group(3)}" if match else None


Fetcher = Callable[..., egress.FetchResult]


def collect(plan: Dict[str, Any], fetch: Fetcher = egress.fetch) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    limits = plan["limits"]
    max_candidates = int(limits["max_candidates_per_run"])
    max_fetches = int(limits["max_fetches_per_run"])
    candidates: List[Dict[str, Any]] = []
    report: List[Dict[str, Any]] = []
    fetches = 0
    sources = plan["sources"]
    for position, source in enumerate(sources):
        # Fair share: one busy feed must not take the whole run's candidate
        # budget; whatever earlier sources left unused passes to later ones.
        left = max_candidates - len(candidates)
        remaining = min(left, -(-left // (len(sources) - position)))
        entry: Dict[str, Any] = {"source_id": source["id"], "kind": source["kind"], "ok": False,
                                 "found": 0, "checked_at": store.iso()}
        if remaining <= 0:
            entry["error"] = "лимит кандидатов на запуск исчерпан"
            report.append(entry)
            continue
        try:
            if source["kind"] == "demo":
                found = [dict(DEMO_CANDIDATE)]
            else:
                if fetches >= max_fetches:
                    raise egress.EgressError("лимит запросов на запуск исчерпан")
                with store.tx() as connection:
                    budget.reserve(connection, "fetches")
                fetches += 1
                if source["kind"] == "freelancer_com":
                    url = "https://www.freelancer.com/api/projects/0.1/projects/active/?" + urlencode({
                        "query": source["query"], "limit": min(remaining, 30), "compact": "true",
                        "full_description": "true", "project_types[]": "fixed"})
                    result = fetch(url, plan["allowed_hosts"])
                    if result.status != 200:
                        raise egress.EgressError(f"источник ответил HTTP {result.status}")
                    found = parse_freelancer(result.body, remaining)
                elif source["kind"] == "freelancehunt":
                    url = "https://api.freelancehunt.com/v2/projects?" + urlencode(
                        {"filter[skill_id]": ",".join(str(i) for i in source["skill_ids"])})
                    result = fetch(url, plan["allowed_hosts"])
                    if result.status != 200:
                        raise egress.EgressError(f"источник ответил HTTP {result.status}")
                    found = parse_freelancehunt(result.body, remaining)
                elif source["kind"] == "opire":
                    result = fetch("https://api.opire.dev/rewards", plan["allowed_hosts"])
                    if result.status != 200:
                        raise egress.EgressError(f"источник ответил HTTP {result.status}")
                    found = parse_opire(result.body, remaining)
                    # Opire lists only titles: read each issue's text from GitHub while the run's
                    # request budget and the allowlist permit it; otherwise the title stays alone.
                    for candidate in found:
                        api_url = github_issue_api(candidate["url"])
                        if not api_url or "api.github.com" not in plan["allowed_hosts"] or fetches >= max_fetches:
                            continue
                        try:
                            with store.tx() as connection:
                                budget.reserve(connection, "fetches")
                        except budget.BudgetExceeded:
                            break
                        fetches += 1
                        try:
                            issue = fetch(api_url, plan["allowed_hosts"], accept="application/vnd.github+json")
                            if issue.status == 200:
                                issue_data = json.loads(issue.body.decode("utf-8", errors="replace"))
                                candidate["excerpt"] = clean(issue_data.get("body"), EXCERPT_CHARS)
                                candidate["source_state"] = str(issue_data.get("state") or "open")[:20]
                        except (egress.EgressError, ValueError):
                            continue
                elif source["kind"] == "superteam":
                    key = _superteam_key()
                    if not key:
                        raise egress.EgressError("не задан ключ агента EARNER_SUPERTEAM_API_KEY (Настройки → API-ключи)")
                    headers = {"Authorization": f"Bearer {key}"}
                    types = source.get("types") or ["bounty"]
                    query = {"take": min(max(remaining * 2, 5), 50)}
                    if len(types) == 1:
                        query["type"] = types[0]
                    result = fetch(f"https://{store.SUPERTEAM_HOST}/api/agents/listings/live?" + urlencode(query),
                                   plan["allowed_hosts"], extra_headers=headers)
                    if result.status != 200:
                        raise egress.EgressError(f"источник ответил HTTP {result.status}")
                    found = parse_superteam(result.body, remaining, types)
                    # The list has no description: read each listing's details while
                    # the run's request budget allows; without them the title stays alone.
                    for candidate in found:
                        if fetches >= max_fetches:
                            break
                        try:
                            with store.tx() as connection:
                                budget.reserve(connection, "fetches")
                        except budget.BudgetExceeded:
                            break
                        fetches += 1
                        try:
                            details = fetch(f"https://{store.SUPERTEAM_HOST}/api/agents/listings/details/"
                                            f"{candidate['slug']}", plan["allowed_hosts"], extra_headers=headers)
                            if details.status == 200:
                                candidate["excerpt"] = superteam_details_text(details.body)
                        except (egress.EgressError, ValueError):
                            continue
                    for candidate in found:
                        candidate.pop("slug", None)
                elif source["kind"] == "github_issues":
                    url = "https://api.github.com/search/issues?" + urlencode({
                        "q": source["query"], "sort": "created", "order": "desc",
                        "per_page": min(remaining, 30)})
                    result = fetch(url, plan["allowed_hosts"], accept="application/vnd.github+json")
                    if result.status != 200:
                        raise egress.EgressError(f"источник ответил HTTP {result.status}")
                    found = parse_github(result.body, remaining)
                else:
                    result = fetch(source["url"], plan["allowed_hosts"],
                                   accept="application/rss+xml, application/atom+xml, application/xml, text/xml")
                    if result.status != 200:
                        raise egress.EgressError(f"источник ответил HTTP {result.status}")
                    found = parse_feed(result.body, remaining)
                entry["address"] = result.address
            fetched_at = store.iso()
            for candidate in found[:remaining]:
                candidate.update({"source_id": source["id"], "source_kind": source["kind"], "fetched_at": fetched_at})
                candidates.append(candidate)
            entry.update({"ok": True, "found": len(found[:remaining])})
        except budget.BudgetExceeded as exc:
            entry["error"] = str(exc)
        except (egress.EgressError, ValueError, ET.ParseError) as exc:
            entry["error"] = str(exc)[:300] or type(exc).__name__
        report.append(entry)
    return candidates, report, fetches


def store_candidates(run_id: str, candidates: List[Dict[str, Any]]) -> Dict[str, int]:
    """Upsert by URL: a candidate seen again refreshes last_seen/state and is
    flagged stale once its source no longer reports it open."""
    counts = {"new": 0, "seen": 0}
    with store.tx() as connection:
        for candidate in candidates:
            key = store.url_hash(candidate["url"])
            stale = 1 if candidate["source_state"] not in ("", "open", "demo") else 0
            existing = connection.execute(
                "SELECT id FROM earner_opportunities WHERE url_hash = ?", (key,)).fetchone()
            if existing:
                connection.execute(
                    "UPDATE earner_opportunities SET last_seen_at = ?, source_state = ?, stale = ?, updated_at = ? "
                    "WHERE id = ?", (candidate["fetched_at"], candidate["source_state"], stale, store.iso(),
                                     existing["id"]))
                counts["seen"] += 1
                continue
            opp_id = store.new_id("opp")
            connection.execute(
                "INSERT INTO earner_opportunities (id, run_id, source_id, source_kind, url, url_hash, title, excerpt, "
                "labels, claimed_reward, source_state, source_created_at, fetched_at, last_seen_at, is_demo, stale, "
                "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (opp_id, run_id, candidate["source_id"], candidate["source_kind"], candidate["url"], key,
                 candidate["title"], candidate["excerpt"], store.dumps(candidate["labels"]),
                 candidate["claimed_reward"], candidate["source_state"], candidate["source_created_at"],
                 candidate["fetched_at"], candidate["fetched_at"], candidate["is_demo"], stale, store.NEW,
                 store.iso(), store.iso()))
            store.log_event(connection, "opportunity", opp_id, "found",
                            f"Найдено Scout в источнике {candidate['source_id']}", {"run_id": run_id})
            counts["new"] += 1
    return counts
