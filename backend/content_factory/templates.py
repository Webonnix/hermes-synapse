"""Pipeline templates (§97, §143).

A template is a function that returns a graph, not a stored pipeline: it is
instantiated into a normal editable pipeline and then belongs to the owner (§97
— "Template создаёт новый editable Pipeline"). Changing a template here never
alters a pipeline someone already created from it, which is the same principle
that makes pipeline versions immutable.

`ai_news_shorts` is the reference graph from §143, and it is what the first
end-to-end vertical slice (§142) is meant to run.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from backend.content_factory import pipelines
from backend.content_factory.domain import ContentFactoryError, ErrorCode


def _node(node_id: str, node_type: str, name: str, position: tuple[int, int], **kwargs: Any) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "type": node_type,
        "name": name,
        "position": {"x": position[0], "y": position[1]},
    }
    node.update(kwargs)
    return node


def ai_news_shorts(options: dict[str, Any]) -> dict[str, Any]:
    """§143 AI NEWS SHORTS: research → script → storyboard → (voice ‖ visuals)
    → render → QA → approval → metadata → publish."""
    language = str(options.get("language", "ru"))
    duration = int(options.get("duration", 45))
    preset = str(options.get("preset", "YOUTUBE_SHORT"))
    niche = str(options.get("niche", "искусственный интеллект"))
    channel_id = str(options.get("channelId", ""))
    retry_policy = {
        "maxAttempts": int(options.get("maxAttempts", 3)),
        "initialDelay": float(options.get("initialDelaySeconds", 5)),
        "maxDelay": float(options.get("maxDelaySeconds", 60)),
        "backoffMultiplier": float(options.get("backoffMultiplier", 2)),
    }
    timeout = int(options.get("providerTimeoutSeconds", 300))
    fallback_enabled = bool(options.get("fallbackEnabled", True))

    def fallbacks(key: str) -> list[str]:
        return [str(item) for item in (options.get(key) or [])] if fallback_enabled else []

    nodes = [
        _node("trigger", "manual_trigger", "Запуск", (0, 200)),
        _node(
            "topic", "topic_discovery", "Поиск темы", (180, 200),
            config={
                "agentId": "cf_topic", "niche": niche, "language": language,
                "temperature": options.get("temperature", 0.4),
                "maxTokens": options.get("maxTokens", 4000),
            },
            inputMapping={"niche": niche},
            retryPolicy=retry_policy, fallbackPolicy=fallbacks("llmFallbackProviders"), timeout=timeout,
        ),
        _node(
            "research", "research", "Исследование", (360, 200),
            config={
                "agentId": "cf_research", "maxSources": int(options.get("maxSources", 6)),
                "temperature": options.get("temperature", 0.4),
                "maxTokens": options.get("maxTokens", 4000),
                "requireSources": options.get("requireSources", True),
                "minSources": options.get("minSources", 2),
                "maxSourceAgeDays": options.get("maxSourceAgeDays", 30),
            },
            inputMapping={"topic": "{{ nodes.topic.output.topic }}"},
            retryPolicy=retry_policy, fallbackPolicy=fallbacks("llmFallbackProviders"), timeout=timeout,
        ),
        _node(
            "script", "script", "Сценарий", (540, 200),
            config={
                "agentId": "cf_script",
                "duration": duration,
                "language": language,
                "platform": preset,
                "audience": options.get("targetAudience", ""),
                "tone": options.get("brandTone", ""),
                "temperature": options.get("temperature", 0.4),
                "maxTokens": options.get("maxTokens", 4000),
            },
            inputMapping={"research": "{{ nodes.research.output }}"},
            retryPolicy=retry_policy, fallbackPolicy=fallbacks("llmFallbackProviders"), timeout=timeout,
        ),
        _node(
            "storyboard", "storyboard", "Раскадровка", (720, 200),
            config={"agentId": "cf_storyboard", "duration": duration, "preset": preset},
            inputMapping={"script": "{{ nodes.script.output }}"},
            retryPolicy=retry_policy, fallbackPolicy=fallbacks("llmFallbackProviders"), timeout=timeout,
        ),
        # §19: voice and visuals fan out from the storyboard and run together.
        _node(
            "voice", "voice_generation", "Озвучка", (900, 120),
            config={"voice": options.get("voice", "")},
            inputMapping={"scenes": "{{ nodes.storyboard.output.scenes }}"},
            retryPolicy=retry_policy, fallbackPolicy=fallbacks("voiceFallbackProviders"), timeout=timeout,
        ),
        _node(
            "visuals", "image_generation", "Визуал", (900, 280),
            config={"preset": preset, "allScenes": True, "parallel": int(options.get("imageParallelism", 3))},
            inputMapping={"scenes": "{{ nodes.storyboard.output.scenes }}"},
            retryPolicy=retry_policy, fallbackPolicy=fallbacks("imageFallbackProviders"), timeout=timeout,
        ),
        _node(
            "subtitles", "subtitle_generation", "Субтитры", (1080, 120),
            inputMapping={
                "scenes": "{{ nodes.storyboard.output.scenes }}",
                "segments": "{{ nodes.voice.output.segments }}",
            },
        ),
        _node(
            "render", "video_composer", "Сборка", (1260, 200),
            config={
                "preset": preset,
                "subtitles": bool(options.get("subtitlesEnabled", True)),
                "subtitleStyle": options.get("subtitleStyle", "default"),
                "subtitleFont": options.get("subtitleFont", "Inter"),
                "subtitlePosition": options.get("subtitlePosition", "bottom"),
                "musicGainDb": options.get("musicGainDb", -18),
                "branding": {"logoAssetId": options.get("watermarkAssetId", "")},
                "normalizeAudio": options.get("normalizeAudio", True),
                "loudnessTargetLufs": options.get("loudnessTargetLufs", -14),
                "transition": options.get("transition", "fade"),
            },
            inputMapping={
                "scenes": "{{ nodes.storyboard.output.scenes }}",
                "segments": "{{ nodes.voice.output.segments }}",
                "images": "{{ nodes.visuals.output.images }}",
                "subtitleAssetIds": "{{ nodes.subtitles.output.assetIds }}",
            },
            timeout=max(1800, timeout), retryPolicy=retry_policy,
        ),
        _node(
            "tech_qa", "technical_qa", "Тех. проверка", (1440, 200),
            config={"minDuration": max(5, duration - 15), "maxDuration": duration + 20},
            inputMapping={"assetId": "{{ nodes.render.output.assetId }}"},
        ),
        _node(
            "ai_qa", "ai_qa", "Контент-проверка", (1620, 200),
            config={
                "agentId": "cf_qa", "minScore": options.get("minQualityScore", 0.7),
                "temperature": options.get("temperature", 0.4),
            },
            inputMapping={
                "script": "{{ nodes.script.output }}",
                "research": "{{ nodes.research.output }}",
            },
            retryPolicy=retry_policy, fallbackPolicy=fallbacks("llmFallbackProviders"), timeout=timeout,
        ),
        _node(
            "metadata", "metadata", "Метаданные", (1800, 200),
            config={
                "agentId": "cf_seo", "language": language, "platform": preset,
                "temperature": options.get("temperature", 0.4),
            },
            inputMapping={"script": "{{ nodes.script.output }}"},
            retryPolicy=retry_policy, fallbackPolicy=fallbacks("llmFallbackProviders"), timeout=timeout,
        ),
        _node(
            "policy", "policy_check", "Проверка политик", (1890, 200),
            config={
                "denylist": options.get("denylist", []),
                "blockProfanity": options.get("blockProfanity", True),
            },
            inputMapping={
                "title": "{{ nodes.metadata.output.title }}",
                "description": "{{ nodes.metadata.output.description }}",
                "script": "{{ nodes.script.output }}",
            },
        ),
        _node("approval", "human_approval", "Проверка владельцем", (1980, 200)),
        _node(
            "publish", "publish", "Публикация", (2160, 200),
            config={
                "channelId": channel_id,
                "privacy": options.get("privacy", "private"),
                "tags": options.get("defaultTags", []),
                "madeForKids": options.get("madeForKids", False),
                "categoryId": options.get("youtubeCategoryId", "22"),
            },
            inputMapping={
                "assetId": "{{ nodes.render.output.assetId }}",
                "title": "{{ nodes.metadata.output.title }}",
                "description": "{{ nodes.metadata.output.description }}",
                "tags": "{{ nodes.metadata.output.hashtags }}",
            },
            retryPolicy=retry_policy, timeout=timeout,
        ),
    ]

    edges = [
        {"source": "trigger", "target": "topic"},
        {"source": "topic", "target": "research"},
        {"source": "research", "target": "script"},
        {"source": "script", "target": "storyboard"},
        {"source": "storyboard", "target": "voice"},
        {"source": "storyboard", "target": "visuals"},
        {"source": "voice", "target": "subtitles"},
        {"source": "subtitles", "target": "render"},
        {"source": "visuals", "target": "render"},
        {"source": "render", "target": "tech_qa"},
        # QA failures do not silently publish: only the PASS branch continues.
        {"source": "tech_qa", "target": "ai_qa", "branch": "PASS"},
        {"source": "ai_qa", "target": "metadata", "branch": "PASS"},
        {"source": "metadata", "target": "policy"},
        {"source": "policy", "target": "approval", "branch": "PASS"},
        {"source": "approval", "target": "publish", "branch": "APPROVED"},
    ]
    return {"nodes": nodes, "edges": edges}


def manual_video(options: dict[str, Any]) -> dict[str, Any]:
    """Smallest useful graph: a topic in, a rendered video out, no publishing.
    Useful for trying providers out without touching a channel."""
    duration = int(options.get("duration", 30))
    preset = str(options.get("preset", "YOUTUBE_SHORT"))
    nodes = [
        _node("trigger", "manual_trigger", "Запуск", (0, 160)),
        _node(
            "script", "script", "Сценарий", (200, 160),
            config={"agentId": "cf_script", "duration": duration, "language": options.get("language", "ru")},
            inputMapping={"topic": "{{ trigger.topic }}"},
        ),
        _node(
            "storyboard", "storyboard", "Раскадровка", (400, 160),
            config={"agentId": "cf_storyboard", "duration": duration, "preset": preset},
            inputMapping={"script": "{{ nodes.script.output }}"},
        ),
        _node(
            "voice", "voice_generation", "Озвучка", (600, 80),
            inputMapping={"scenes": "{{ nodes.storyboard.output.scenes }}"},
        ),
        _node(
            "visuals", "image_generation", "Визуал", (600, 240),
            config={"preset": preset, "allScenes": True},
            inputMapping={"scenes": "{{ nodes.storyboard.output.scenes }}"},
        ),
        _node(
            "render", "video_composer", "Сборка", (800, 160),
            config={"preset": preset, "subtitles": False},
            inputMapping={
                "scenes": "{{ nodes.storyboard.output.scenes }}",
                "segments": "{{ nodes.voice.output.segments }}",
                "images": "{{ nodes.visuals.output.images }}",
            },
            timeout=1800,
        ),
    ]
    edges = [
        {"source": "trigger", "target": "script"},
        {"source": "script", "target": "storyboard"},
        {"source": "storyboard", "target": "voice"},
        {"source": "storyboard", "target": "visuals"},
        {"source": "voice", "target": "render"},
        {"source": "visuals", "target": "render"},
    ]
    return {"nodes": nodes, "edges": edges}


def educational_shorts(options: dict[str, Any]) -> dict[str, Any]:
    """Explainer format: no news hunt, a fixed subject supplied at trigger time,
    and video clips instead of stills where the storyboard asks for motion."""
    graph = ai_news_shorts(options)
    nodes = [node for node in graph["nodes"] if node["id"] != "topic"]
    for node in nodes:
        if node["id"] == "research":
            node["inputMapping"] = {"topic": "{{ trigger.topic }}"}
    nodes.append(
        _node(
            "clips", "video_generation", "Видео-клипы", (900, 400),
            config={"preset": options.get("preset", "YOUTUBE_SHORT"), "clipDuration": 5},
            inputMapping={"scenes": "{{ nodes.storyboard.output.scenes }}"},
            timeout=1800,
        )
    )
    edges = [edge for edge in graph["edges"] if edge["source"] != "topic" and edge["target"] != "topic"]
    edges.append({"source": "trigger", "target": "research"})
    edges.append({"source": "storyboard", "target": "clips"})
    edges.append({"source": "clips", "target": "render"})
    for node in nodes:
        if node["id"] == "render":
            node["inputMapping"]["clips"] = "{{ nodes.clips.output.clips }}"
    return {"nodes": nodes, "edges": edges}


TEMPLATES: dict[str, dict[str, Any]] = {
    "ai_news_shorts": {
        "id": "ai_news_shorts",
        "name": "AI NEWS SHORTS",
        "description": "Ежедневный новостной ролик: поиск темы, факт-чекинг по источникам, "
                       "сценарий, озвучка, визуал, сборка, двойной QA, approval, публикация.",
        "build": ai_news_shorts,
        "defaults": {"language": "ru", "duration": 45, "preset": "YOUTUBE_SHORT"},
    },
    "educational_shorts": {
        "id": "educational_shorts",
        "name": "Educational Shorts",
        "description": "Объясняющий ролик по заданной теме с видео-клипами вместо статичных кадров.",
        "build": educational_shorts,
        "defaults": {"language": "ru", "duration": 60, "preset": "YOUTUBE_SHORT"},
    },
    "manual_video": {
        "id": "manual_video",
        "name": "Manual Video Generation",
        "description": "Минимальный конвейер без публикации — проверить провайдеров и качество.",
        "build": manual_video,
        "defaults": {"language": "ru", "duration": 30, "preset": "YOUTUBE_SHORT"},
    },
}


def list_templates() -> list[dict[str, Any]]:
    return [
        {key: value for key, value in template.items() if key != "build"}
        for template in TEMPLATES.values()
    ]


def get_graph(template_id: str, options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    template = TEMPLATES.get(template_id)
    if not template:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"Unknown template: {template_id}", retryable=False
        )
    merged = {**template["defaults"], **(options or {})}
    builder: Callable[[dict[str, Any]], dict[str, Any]] = template["build"]
    return builder(merged)


def _apply_global_node_defaults(graph: dict[str, Any], options: dict[str, Any]) -> None:
    """Apply operational defaults to every template, including small/manual
    templates that do not spell out the full configuration themselves."""
    retry = {
        "maxAttempts": int(options.get("maxAttempts", 3)),
        "initialDelay": float(options.get("initialDelaySeconds", 5)),
        "maxDelay": float(options.get("maxDelaySeconds", 60)),
        "backoffMultiplier": float(options.get("backoffMultiplier", 2)),
    }
    fallback_key = {
        "topic_discovery": "llmFallbackProviders", "research": "llmFallbackProviders",
        "script": "llmFallbackProviders", "storyboard": "llmFallbackProviders",
        "ai_qa": "llmFallbackProviders", "metadata": "llmFallbackProviders",
        "image_generation": "imageFallbackProviders", "video_generation": "videoFallbackProviders",
        "voice_generation": "voiceFallbackProviders",
    }
    external_types = set(fallback_key) | {"publish"}
    for node in graph.get("nodes") or []:
        node_type = str(node.get("type") or "")
        config = node.setdefault("config", {})
        if node_type in external_types:
            node.setdefault("retryPolicy", retry)
            node.setdefault("timeout", int(options.get("providerTimeoutSeconds", 300)))
        key = fallback_key.get(node_type)
        if key and options.get("fallbackEnabled", True):
            node.setdefault("fallbackPolicy", [str(item) for item in (options.get(key) or [])])
        if node_type in {"topic_discovery", "research", "script", "storyboard", "ai_qa", "metadata"}:
            config.setdefault("temperature", options.get("temperature", 0.4))
            config.setdefault("maxTokens", options.get("maxTokens", 4000))
        if node_type == "image_generation":
            config.setdefault("parallel", int(options.get("imageParallelism", 3)))
        if node_type == "video_generation":
            config.setdefault("clipDuration", int(options.get("clipDuration", 5)))
        if node_type == "video_composer":
            config.update({
                "subtitles": options.get("subtitlesEnabled", config.get("subtitles", True)),
                "subtitleStyle": options.get("subtitleStyle", "default"),
                "subtitleFont": options.get("subtitleFont", "Inter"),
                "subtitlePosition": options.get("subtitlePosition", "bottom"),
                "musicGainDb": options.get("musicGainDb", -18),
                "normalizeAudio": options.get("normalizeAudio", True),
                "loudnessTargetLufs": options.get("loudnessTargetLufs", -14),
                "transition": options.get("transition", "fade"),
            })


def instantiate(
    template_id: str,
    *,
    name: str = "",
    project_id: Optional[str] = None,
    options: Optional[dict[str, Any]] = None,
    created_by: str = "owner",
) -> dict[str, Any]:
    """Creates a DRAFT pipeline from the template.

    Draft, not active: a template cannot know which channel to publish to or
    what the owner's budget is, so it must be reviewed and activated
    deliberately (§105) rather than starting to produce on a schedule the
    moment it is created.
    """
    template = TEMPLATES.get(template_id)
    if not template:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"Unknown template: {template_id}", retryable=False
        )
    from backend.content_factory import settings as cf_settings

    configured = cf_settings.get_settings()
    general = configured["general"]
    generation = configured["generation"]
    publishing = configured["publishing"]
    rendering = configured["rendering"]
    policies = configured["policies"]
    retry = configured["retry"]
    effective_options = {
        "language": general["defaultLanguage"],
        "duration": general["defaultDuration"],
        "preset": general["defaultPreset"],
        "niche": general["contentNiche"],
        "targetAudience": general["targetAudience"],
        "brandTone": general["brandTone"],
        "privacy": publishing["defaultPrivacy"],
        "madeForKids": publishing["madeForKids"],
        "youtubeCategoryId": publishing["youtubeCategoryId"],
        "defaultTags": publishing["defaultTags"],
        **generation,
        **rendering,
        **policies,
        **retry,
        **(options or {}),
    }
    graph = get_graph(template_id, effective_options)
    _apply_global_node_defaults(graph, effective_options)
    pipeline = pipelines.create_pipeline(
        name or str(template["name"]),
        project_id=project_id,
        description=str(template["description"]),
        nodes=graph["nodes"],
        edges=graph["edges"],
        publishing_mode=str(effective_options.get("publishingMode") or publishing["mode"]),
        max_cost_per_run=effective_options.get("maxCostPerRun", configured["budgets"]["maxCostPerRun"]),
        max_concurrent_runs=int(effective_options.get("maxConcurrentRuns") or 1),
        variables={"templateId": template_id, **effective_options},
        created_by=created_by,
    )
    version_id = pipeline.get("draft_version_id")
    return {
        "pipeline": pipeline,
        "validation": pipelines.validate_version(version_id) if version_id else None,
    }
