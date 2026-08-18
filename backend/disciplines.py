"""Disciplines: what KIND of work a Kanban card is, and who should do it.

A board that only knows "task" and "agent" has no way to send a landing-page
build to the person who does landing pages and a competitor-research job to the
person who does research — auto-assign could only pick whoever was least busy,
which is round-robin wearing a hat. A discipline is the missing middle term: the
card declares one, agents declare which ones they cover, and assignment becomes
a match instead of a coin flip.

Deliberately a small fixed vocabulary rather than free-text tags. Free text
would drift ("web", "webdev", "сайты", "frontend") and silently stop matching;
a closed set keeps the router, the UI picker and the executor's specialist
briefing all reading from the same list. Adding one is a code change on purpose.

Each entry also carries a `briefing` — the standards the executor is held to for
that kind of work. Generic "do a good job" instructions produced generic output
(see the LŪMEN//ÍNDEX rejection, 2026-08-18); telling a site build specifically
what "finished" means for a SITE is what makes the difference.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Order matters: it is the order the picker renders, and — because
# detect_discipline scores in this order and ties go to the first match — the
# priority when a goal mentions several fields at once.
DISCIPLINES: Dict[str, Dict[str, Any]] = {
    "web": {
        "label_ru": "Разработка сайта",
        "label_en": "Web development",
        "keywords_ru": ["сайт", "лендинг", "страниц", "вёрстк", "верстк", "фронтенд",
                        "интерфейс", "ui", "ux", "дизайн сайт", "веб"],
        "keywords_en": ["website", "landing", "web page", "frontend", "front-end", "ui",
                        "ux", "react", "vue", "svelte", "tailwind", "css", "html"],
        "briefing": (
            "This is a WEB BUILD. Ship a real, publishable static site: build it, call "
            "dev_publish_demo on the build output, then dev_review_demo, and fix "
            "everything it reports — including the design critique, not just the "
            "technical errors. Finished means a page you would show a paying client: "
            "real content in every element, real icons from an actual icon library, a "
            "consistent spacing scale, one clear focal point, and a look that matches "
            "the register the brief asks for. Responsive at 375/768/1280 is a "
            "requirement, not a bonus."
        ),
    },
    "analytics": {
        "label_ru": "Аналитика и данные",
        "label_en": "Analytics & data",
        "keywords_ru": ["аналитик", "данн", "отчёт", "отчет", "статистик", "метрик",
                        "дашборд", "исследован", "выгрузк", "график"],
        "keywords_en": ["analytics", "data", "report", "dashboard", "metrics",
                        "statistics", "dataset", "chart", "sql", "csv"],
        "briefing": (
            "This is a DATA/ANALYTICS job. State where every number came from and how "
            "it was derived — an analysis whose source cannot be traced is worthless. "
            "Show the actual query/script that produced each figure, sanity-check "
            "totals against the raw data before reporting them, and call out the "
            "limitations of the dataset explicitly. If the output is a chart or "
            "dashboard, publish it with dev_publish_demo so it can actually be looked "
            "at, and label every axis and unit."
        ),
    },
    "marketing": {
        "label_ru": "Продвижение и маркетинг",
        "label_en": "Marketing & growth",
        "keywords_ru": ["продвижен", "маркетинг", "реклам", "seo", "смм", "smm",
                        "воронк", "конверси", "кампан", "аудитор", "позиционирован"],
        "keywords_en": ["marketing", "promotion", "seo", "advertis", "campaign",
                        "funnel", "conversion", "audience", "positioning", "growth"],
        "briefing": (
            "This is a MARKETING job. Ground every recommendation in something "
            "checkable — an actual competitor page, a real search volume, a specific "
            "audience segment — never in generic best-practice filler. Be concrete "
            "about channel, message and measurable target for each proposal, and say "
            "plainly what you could not verify rather than padding it with plausible-"
            "sounding claims."
        ),
    },
    "content": {
        "label_ru": "Контент и тексты",
        "label_en": "Content & copy",
        "keywords_ru": ["текст", "контент", "стать", "копирайт", "описан", "пост",
                        "рассылк", "документац"],
        "keywords_en": ["content", "copywriting", "article", "blog", "text", "docs",
                        "documentation", "newsletter", "post"],
        "briefing": (
            "This is a CONTENT job. Write for the actual reader named in the brief, in "
            "their language and register — not in AI-house-style. No filler openers, "
            "no restating the prompt back, no padding to hit a length. Every claim "
            "either checks out or is cut. Deliver the finished text itself as a file "
            "in the repo, not a description of the text you would write."
        ),
    },
    "devops": {
        "label_ru": "DevOps и инфраструктура",
        "label_en": "DevOps & infrastructure",
        "keywords_ru": ["деплой", "docker", "сервер", "инфраструктур", "ci", "cd",
                        "мониторинг", "бэкап", "backup", "nginx", "kubernetes"],
        "keywords_en": ["deploy", "docker", "server", "infrastructure", "ci/cd",
                        "pipeline", "monitoring", "backup", "nginx", "kubernetes"],
        "briefing": (
            "This is an INFRASTRUCTURE job. Everything you write must be reproducible "
            "from the repo alone — no manual steps that live only in your head. State "
            "the rollback for every change before you make it. Prefer the least "
            "privileged option that works, and never widen a security boundary as a "
            "shortcut around an error; fix the actual cause."
        ),
    },
    "automation": {
        "label_ru": "Автоматизация и интеграции",
        "label_en": "Automation & integrations",
        "keywords_ru": ["автоматиз", "интеграц", "бот", "api", "вебхук", "webhook",
                        "синхронизац", "парсер", "скрипт"],
        "keywords_en": ["automation", "integration", "bot", "api", "webhook", "scraper",
                        "parser", "sync", "script", "workflow"],
        "briefing": (
            "This is an AUTOMATION job. It has to survive being run unattended: handle "
            "the failure paths (network down, malformed input, partial run) explicitly, "
            "make it safe to re-run without duplicating its effects, and log enough "
            "that someone debugging it at 3am can tell what happened. A script that "
            "only works on the happy path is not done."
        ),
    },
    "backend": {
        "label_ru": "Backend и API",
        "label_en": "Backend & API",
        # "баз" + "данн" as separate stems rather than the phrase "база данн":
        # the phrase does not survive "базы данных"/"базу данных".
        "keywords_ru": ["бэкенд", "backend", "сервис", "баз данн", "базы данн",
                        "базу данн", "миграц", "эндпоинт", "очеред", "схему баз"],
        "keywords_en": ["backend", "service", "database", "migration", "endpoint",
                        "queue", "schema", "rest", "graphql"],
        "briefing": (
            "This is a BACKEND job. Validate at the boundary, never trust caller input, "
            "and make failure modes explicit rather than letting them surface as a 500. "
            "Any schema change must be additive and reversible. Cover the new behaviour "
            "with tests and run them with dev_run_tests before you call it done."
        ),
    },
    "research": {
        "label_ru": "Исследование и разведка",
        "label_en": "Research",
        "keywords_ru": ["исследуй", "изучи", "конкурент", "обзор рынк", "сравни",
                        "разбер", "проанализируй рынок"],
        "keywords_en": ["research", "investigate", "competitor", "market", "compare",
                        "survey", "landscape", "benchmark"],
        "briefing": (
            "This is a RESEARCH job. Cite the source for every factual claim and "
            "separate what you verified from what you inferred. Contradictory sources "
            "get reported as contradictory, not silently resolved in favour of the "
            "tidier answer. An honest 'could not establish this' beats a confident "
            "guess."
        ),
    },
}

# Cards created before disciplines existed, and anything the picker leaves on
# "auto", resolve through detect_discipline() rather than carrying this value.
GENERAL = "general"


def discipline_ids() -> List[str]:
    return list(DISCIPLINES)


def is_valid(discipline: Optional[str]) -> bool:
    return bool(discipline) and discipline in DISCIPLINES


def label(discipline: Optional[str], language: str = "ru") -> str:
    entry = DISCIPLINES.get(discipline or "")
    if not entry:
        return "Общая задача" if language == "ru" else "General"
    return entry["label_ru"] if language == "ru" else entry["label_en"]


def catalog(language: str = "ru") -> List[Dict[str, str]]:
    """Picker payload for the board — id + localized label, in declared order."""
    return [{"id": key, "label": label(key, language)} for key in DISCIPLINES]


# A keyword this short is a substring of ordinary words ("ui" inside "build",
# "ci" inside "specific"), so it only counts as a whole word. Longer stems
# stay substring-matched on purpose: Russian inflects heavily and "аналитик"
# has to catch "аналитика"/"аналитику"/"аналитический" without listing each.
_SHORT_KEYWORD_MAX = 3


def _score(goal: str, entry: Dict[str, Any]) -> int:
    """How strongly one discipline's vocabulary shows up in the goal.

    Weighted by keyword length rather than counting hits, because specificity
    matters more than quantity: a goal mentioning "эндпоинт" is a backend job
    even though "данн" also appears in it and belongs to analytics. Counting
    raw hits made the vaguer discipline win those ties."""
    score = 0
    for keyword in entry["keywords_ru"] + entry["keywords_en"]:
        if len(keyword) <= _SHORT_KEYWORD_MAX:
            if re.search(rf"(?<![a-zа-я0-9]){re.escape(keyword)}(?![a-zа-я0-9])", goal):
                score += len(keyword)
        elif keyword in goal:
            score += len(keyword)
    return score


def detect_discipline(goal: str) -> Optional[str]:
    """Best-guess discipline for a free-text goal, or None if nothing matches.

    Used when the owner leaves the picker on "auto" — most cards are typed as a
    sentence, not classified, and making the picker mandatory would just train
    people to pick the first option."""
    if not goal:
        return None
    lowered = re.sub(r"\s+", " ", goal.lower())
    best, best_score = None, 0
    for key, entry in DISCIPLINES.items():
        score = _score(lowered, entry)
        if score > best_score:
            best, best_score = key, score
    return best


def briefing(discipline: Optional[str]) -> str:
    """The specialist standards the executor is held to for this kind of work."""
    entry = DISCIPLINES.get(discipline or "")
    return entry["briefing"] if entry else ""
