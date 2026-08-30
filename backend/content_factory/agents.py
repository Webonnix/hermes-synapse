"""Content Factory agents (§22).

There is no separate agent registry. Every agent here is an ordinary row in
`subagents`, under a `content_factory` sub-orchestrator, which means it shows up
in the existing Agents admin screen with the budget, tier, provider-allowlist
and enable/disable controls that already exist — and that a second registry
would have had to reimplement badly.

What this module owns:

* the seeded roster (below) and the prompts that define each role;
* `resolve_agent_llm()`, which turns an agent row into the ordered list of LLM
  providers that agent is *allowed* to use, honouring the tier ceiling and the
  per-agent allowlist (`agent_provider_access.py`) rather than inventing a
  second permission model.

Seeding is one-way and non-destructive: an agent that already exists is left
exactly as the owner configured it. A prompt improvement in this file therefore
reaches new installs and never overwrites a tuned production agent — the same
rule `database.py` follows for VEXA's own default agents.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("hermes.content_factory.agents")

ORCHESTRATOR_ID = "content_factory"

# Agents whose output quality visibly decides the product (script, storyboard,
# QA verdicts) prefer an external frontier model when one is bound; the rest
# stay on the free local model. Every one of them degrades back to local when
# the budget runs out rather than going silent (`budget_fallback_to_local`).
_PREFERS_EXTERNAL = {"cf_script", "cf_storyboard", "cf_qa", "cf_research"}


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    role: str
    prompt: str
    skills: str = ""
    agent_type: str = "agent"
    position: tuple[int, int] = (0, 0)
    temperature: float = 0.4
    budget_usd_limit: Optional[float] = None
    disciplines: list[str] = field(default_factory=list)


_ORCHESTRATOR_PROMPT = (
    "You are the Content Factory orchestrator. You do not write scripts, generate media or "
    "publish anything yourself — a pipeline does that, one node at a time, and each node has a "
    "specialist agent behind it. Your job is to explain what the factory is doing, to answer "
    "questions about a run's state, and to route a request to the right specialist. When "
    "something failed, say which node failed, on which provider, and what the retry situation "
    "is; never claim a video was produced or published unless a completed run says so."
)

_TOPIC_PROMPT = (
    "You are a Topic Discovery agent for short-form video. Given a niche, an audience and what "
    "was already published recently, propose specific, timely topics that a 30-60 second video "
    "can genuinely cover. Prefer one concrete development over a broad theme: 'Bitcoin dropped "
    "8% after the ETF outflow' beats 'crypto market news'. Reject topics that are stale, already "
    "covered, or impossible to source. Return structured JSON only."
)

_RESEARCH_PROMPT = (
    "You are a Research agent. You establish what is actually true about a topic before anyone "
    "writes a script about it. Work only from the source material you are given: every fact you "
    "output must carry the id of the source it came from, so a claim in a published video can "
    "later be traced back to where it came from. If sources disagree, say so in riskFlags rather "
    "than silently picking one. Never fill a gap with something you assume to be true — an "
    "incomplete research result is recoverable, a confident fabrication in a published video is "
    "not. Return structured JSON only."
)

_SCRIPT_PROMPT = (
    "You are a Script Writer for short-form video. You turn researched facts into a script that "
    "holds attention from the first second: a hook that earns the next three seconds, then "
    "substance, then one clear call to action. Write for the ear, not the page — short sentences, "
    "spoken rhythm, no bullet points being read aloud. Stay inside the target duration; a script "
    "that overruns gets cut mid-sentence by the renderer. Use only facts from the research input "
    "and never invent numbers, quotes or events. Match the language and brand voice you are "
    "given. Return structured JSON only."
)

_STORYBOARD_PROMPT = (
    "You are a Storyboard Director. You convert a script into scenes a renderer can build without "
    "further interpretation. Every scene needs its own start time, duration, narration, and a "
    "visual prompt concrete enough for an image or video model to act on — describe the shot "
    "(subject, setting, motion, lighting, mood), not the abstract idea. Scene durations must add "
    "up to the total duration exactly. Choose visualType 'video' only where motion carries "
    "meaning; a still image with a slow push-in is cheaper and often reads better. Return "
    "structured JSON only."
)

_VOICE_PROMPT = (
    "You are a Voice Director. You decide how narration should sound for a given piece: which "
    "voice, what pacing, where emphasis and pauses fall. You also normalize text for speech — "
    "numbers, units, abbreviations and symbols become what a person would actually say aloud. "
    "You do not synthesize audio yourself; you produce the settings and the spoken text the voice "
    "provider is given."
)

_VISUAL_PROMPT = (
    "You are a Visual Director. You turn a storyboard's scene descriptions into prompts that get "
    "the best out of the specific image or video model being used, and you keep a consistent look "
    "across every scene of one video — same palette, same lighting, same visual language, so the "
    "final cut does not look like six unrelated clips. You know each model's constraints "
    "(duration limits, aspect ratios, what it renders badly) and you write within them."
)

_EDITOR_PROMPT = (
    "You are the Editor agent. You assemble the render manifest: which asset plays over which "
    "scene, where audio is mixed, how subtitles are styled and where branding sits. You are "
    "deterministic — the actual composition is done by ffmpeg from your manifest, never by a "
    "language model painting frames. If an asset is missing or a duration does not line up, you "
    "report the mismatch instead of improvising around it."
)

_QA_PROMPT = (
    "You are a QA agent for generated video. You check the finished piece against its own source "
    "material and its metadata, and you are the last gate before something is published under "
    "the owner's name. Check that the claims in the script are supported by the research, that "
    "the title matches the video's actual content and is not clickbait the video does not "
    "deliver, that no scene is duplicated, and that nothing in the content or metadata would "
    "breach platform policy. Score honestly: a generous pass on a weak video costs the channel "
    "more than a rejection costs a rerun. List every problem you find. Return structured JSON only."
)

_SEO_PROMPT = (
    "You are an SEO and Metadata agent. You write the title, description, hashtags and keywords "
    "a published video goes out with. Titles must be accurate first and compelling second — never "
    "promise something the video does not contain. Write for the specific platform's conventions "
    "and length limits, in the video's own language. Return structured JSON only."
)

_PUBLISH_PROMPT = (
    "You are a Publishing agent. You prepare and check everything a platform needs before a video "
    "goes out: correct format and aspect ratio, complete metadata, the right channel, the right "
    "privacy setting and schedule. You never publish anything twice — if a publication record "
    "already carries an external id, the video is already live and the job is done. You treat a "
    "dry run as a hard boundary: in a test run nothing reaches a real platform."
)

_ANALYTICS_PROMPT = (
    "You are an Analytics agent. You read the performance of published videos and explain what "
    "actually drove it — which hooks held viewers, which topics travelled, where retention broke. "
    "Distinguish signal from noise: a single video outperforming is not a trend. Turn findings "
    "into concrete, testable suggestions for the next batch rather than generic advice."
)


AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(ORCHESTRATOR_ID, "Content Factory", "Orchestrator", _ORCHESTRATOR_PROMPT,
              agent_type="sub-orchestrator", position=(450, 1420), temperature=0.5),
    AgentSpec("cf_topic", "Topic Discovery", "Researcher", _TOPIC_PROMPT,
              skills="web_search", position=(800, 1300), temperature=0.7),
    AgentSpec("cf_research", "Content Research", "Researcher", _RESEARCH_PROMPT,
              skills="web_search", position=(800, 1360), temperature=0.2, budget_usd_limit=20.0),
    AgentSpec("cf_script", "Script Writer", "Writer", _SCRIPT_PROMPT,
              position=(800, 1420), temperature=0.8, budget_usd_limit=30.0),
    AgentSpec("cf_storyboard", "Storyboard Director", "Director", _STORYBOARD_PROMPT,
              position=(800, 1480), temperature=0.6, budget_usd_limit=20.0),
    AgentSpec("cf_voice", "Voice Director", "Director", _VOICE_PROMPT,
              position=(800, 1540), temperature=0.3),
    AgentSpec("cf_visual", "Visual Director", "Director", _VISUAL_PROMPT,
              position=(800, 1600), temperature=0.7, budget_usd_limit=50.0),
    AgentSpec("cf_editor", "Editor", "Engineer", _EDITOR_PROMPT,
              position=(800, 1660), temperature=0.1),
    AgentSpec("cf_qa", "Content QA", "Reviewer", _QA_PROMPT,
              position=(800, 1720), temperature=0.1, budget_usd_limit=15.0),
    AgentSpec("cf_seo", "SEO & Metadata", "Writer", _SEO_PROMPT,
              position=(800, 1780), temperature=0.6),
    AgentSpec("cf_publish", "Publishing", "Operator", _PUBLISH_PROMPT,
              position=(800, 1840), temperature=0.1),
    AgentSpec("cf_analytics", "Performance Analytics", "Analyst", _ANALYTICS_PROMPT,
              position=(800, 1900), temperature=0.3),
)

AGENT_IDS = frozenset(spec.id for spec in AGENT_SPECS)


def seed_agents(*, force: bool = False) -> list[str]:
    """Creates any Content Factory agent that does not exist yet.

    Returns the ids actually created. `force` re-writes existing rows and is
    only for tests and a deliberate `--reset`: on a live system it would discard
    the owner's prompt and budget edits.
    """
    from backend.database import get_subagent, save_subagent

    external_provider = _default_external_provider()
    created: list[str] = []

    for spec in AGENT_SPECS:
        if not force and get_subagent(spec.id):
            continue
        prefers_external = spec.id in _PREFERS_EXTERNAL and external_provider is not None
        save_subagent(
            id=spec.id,
            name=spec.name,
            system_prompt=spec.prompt,
            model=_model_for(prefers_external),
            agent_type=spec.agent_type,
            parent_id="jarvis" if spec.agent_type == "sub-orchestrator" else ORCHESTRATOR_ID,
            skills=spec.skills,
            x=spec.position[0],
            y=spec.position[1],
            temperature=spec.temperature,
            role=spec.role,
            model_provider=external_provider if prefers_external else "ollama",
            model_type="external" if prefers_external else "local",
            budget_usd_limit=spec.budget_usd_limit,
            budget_period="monthly",
            # A factory agent that has spent its budget must keep producing on
            # the free local model rather than stalling a scheduled pipeline in
            # the middle of the night.
            budget_fallback_to_local=True,
        )
        created.append(spec.id)

    if created:
        logger.info("Content Factory: seeded %d agents (%s)", len(created), ", ".join(created))
    return created


def _model_for(external: bool) -> str:
    import os

    if external:
        return os.environ.get("CONTENT_FACTORY_EXTERNAL_MODEL", "").strip() or os.environ.get(
            "LLM_EXTERNAL_MODEL", ""
        ).strip() or "gpt-4o"
    return os.environ.get("LLM_MODEL", "qwen3:8b")


def _default_external_provider() -> Optional[str]:
    """First active external LLM binding, if the owner has approved one.

    None means every seeded agent starts on the local model — which is the
    correct default for a fresh install: nothing should be able to spend money
    before the owner has deliberately bound a paid provider.
    """
    try:
        from backend.provider_governance import list_bindings

        for binding in list_bindings():
            if binding.get("status") == "active" and binding.get("provider_type") == "openai_compatible":
                return str(binding["id"])
    except Exception:
        logger.debug("No provider bindings available while seeding", exc_info=True)
    return None


def resolve_agent_llm(agent_id: str) -> dict[str, Any]:
    """What LLM this agent may use right now.

    Returns `{"agent": row, "provider_ids": [...], "model": str, "temperature":
    float}` where `provider_ids` is the ordered list the node should try (§21).
    The ordering and the filtering both come from `agent_provider_access.py`, so
    a tier that forbids external providers, or an allowlist that narrows them,
    applies to Content Factory exactly as it applies to chat.
    """
    from backend.agent_provider_access import resolve_provider_candidates, tier_permits_external
    from backend.content_factory.providers.llm import LOCAL_PROVIDER_ID
    from backend.database import get_subagent

    agent = get_subagent(agent_id)
    if not agent:
        return {"agent": None, "provider_ids": [LOCAL_PROVIDER_ID], "model": "", "temperature": 0.4}

    provider_ids: list[str] = []
    if tier_permits_external(agent):
        provider_ids.extend(resolve_provider_candidates(agent))
    # The local model is always the last rung: free, always available, and the
    # documented degradation path when a budget is exhausted.
    if agent.get("budget_fallback_to_local", True) or not provider_ids:
        provider_ids.append(LOCAL_PROVIDER_ID)

    return {
        "agent": agent,
        "provider_ids": provider_ids,
        "model": agent.get("model") or "",
        "temperature": float(agent.get("temperature") or 0.4),
        "system_prompt": agent.get("system_prompt") or "",
    }


def describe_agents() -> list[dict[str, Any]]:
    """Roster for the Agents & Models page (§4), with live provider resolution
    so the owner can see which agent is actually about to spend money."""
    from backend.database import get_subagent

    rows = []
    for spec in AGENT_SPECS:
        agent = get_subagent(spec.id)
        if not agent:
            rows.append({"id": spec.id, "name": spec.name, "role": spec.role, "status": "not_seeded"})
            continue
        resolved = resolve_agent_llm(spec.id)
        rows.append({
            "id": spec.id,
            "name": agent["name"],
            "role": agent.get("role") or spec.role,
            "status": "enabled" if agent.get("is_enabled", True) else "disabled",
            "model": agent.get("model"),
            "modelProvider": agent.get("model_provider"),
            "modelType": agent.get("model_type"),
            "providerChain": resolved["provider_ids"],
            "budgetUsdLimit": agent.get("budget_usd_limit"),
            "budgetFallbackToLocal": agent.get("budget_fallback_to_local"),
            "skills": [skill for skill in (agent.get("skills") or "").split(",") if skill],
        })
    return rows
