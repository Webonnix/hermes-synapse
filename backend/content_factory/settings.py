"""Content Factory settings (§101).

Stored as one JSON document in the existing `app_settings` table rather than in
a table of its own: these are a handful of owner preferences, they are read as a
unit on every settings page load, and giving them a schema would mean a
migration every time a knob is added.

Defaults are the safe end of every choice — review before publishing, private
uploads, mock providers off, budgets set rather than unlimited. A fresh install
must not be able to spend money or publish anything without the owner
deliberately changing something here.

What is *not* here: API keys and OAuth tokens. Those live in Valkey behind
`provider_governance` / the channel credential store (§63), and nothing in this
module may ever hold one.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

logger = logging.getLogger("hermes.content_factory.settings")

SETTING_KEY = "content_factory_settings"

DEFAULTS: dict[str, Any] = {
    "general": {
        # Used by every pipeline that does not override it, and by the wizard
        # as the initial value.
        "defaultLanguage": "ru",
        "timezone": "UTC",
        "defaultPreset": "YOUTUBE_SHORT",
        "defaultDuration": 45,
        "contentNiche": "искусственный интеллект",
        "targetAudience": "широкая аудитория",
        "brandTone": "экспертный и понятный",
    },
    "generation": {
        "maxSources": 6,
        "temperature": 0.4,
        "maxTokens": 4000,
        "imageParallelism": 3,
        "clipDuration": 5,
        "providerTimeoutSeconds": 300,
        "llmFallbackProviders": [],
        "imageFallbackProviders": [],
        "videoFallbackProviders": [],
        "voiceFallbackProviders": [],
    },
    "publishing": {
        # §36: REVIEW_REQUIRED, never FULL_AUTO by default. Switching a channel
        # to fully automatic publication is a decision with an audience on the
        # other end of it.
        "mode": "REVIEW_REQUIRED",
        "defaultPrivacy": "private",
        # Platform rules require synthetic media to be disclosed; the pipeline
        # generates every frame, so this stays on unless the owner takes it off
        # deliberately and knowingly.
        "discloseAiGenerated": True,
        "madeForKids": False,
        "youtubeCategoryId": "22",
        "defaultTags": [],
    },
    "budgets": {
        # §61. Null would mean unlimited; a number that can be raised is safer
        # than no ceiling at all.
        "maxCostPerRun": 2.0,
        "dailyBudget": 25.0,
        "monthlyBudget": 400.0,
        # stop | approval | cheaper
        "onExceeded": "stop",
    },
    "rendering": {
        "subtitlesEnabled": True,
        "subtitleStyle": "default",
        "subtitleFont": "Inter",
        "subtitlePosition": "bottom",
        "musicGainDb": -18,
        "normalizeAudio": True,
        "loudnessTargetLufs": -14,
        "transition": "fade",
        "watermarkAssetId": "",
    },
    "policies": {
        # Words that stop a run at the policy gate before anything is published.
        "denylist": [],
        "minQualityScore": 0.7,
        "requireSources": True,
        "minSources": 2,
        "maxSourceAgeDays": 30,
        "blockProfanity": True,
    },
    "retry": {
        "maxAttempts": 3,
        "initialDelaySeconds": 5,
        "maxDelaySeconds": 60,
        "backoffMultiplier": 2.0,
        "fallbackEnabled": True,
    },
    "network": {
        # The proxy URL itself remains in the encrypted per-channel credential
        # store. These are routing policies only, never secrets.
        "requireProxyForPublishing": False,
        "connectionTimeoutSeconds": 120,
        "analyticsRefreshMinutes": 60,
    },
    "storage": {
        "retentionDays": 90,
        "keepSourceAssets": True,
        "keepFailedRuns": True,
        "signedUrlMinutes": 360,
    },
    "notifications": {
        "onFailure": True,
        "onReviewRequired": True,
        "onBudgetReached": True,
        "onPublished": False,
        "onProviderFallback": True,
        "onPolicyBlocked": True,
    },
    "advanced": {
        "pollSeconds": 5,
        "maxConcurrentRuns": 5,
        "maxParallelNodes": 4,
        "leaseSeconds": 120,
        "logRetentionDays": 30,
    },
}


def get_settings() -> dict[str, Any]:
    """Stored settings merged over the defaults, so a document written by an
    older version never loses a section that was added since."""
    from backend.database import get_setting

    stored: dict[str, Any] = {}
    try:
        raw = get_setting(SETTING_KEY)
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                stored = parsed
    except (TypeError, ValueError):
        logger.warning("Content Factory settings are unreadable; using defaults")

    merged = copy.deepcopy(DEFAULTS)
    for section, values in stored.items():
        if section in merged and isinstance(values, dict):
            merged[section].update(values)
        elif section in merged:
            merged[section] = values
    return merged


def update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """Section-wise merge. Unknown sections and unknown keys are dropped rather
    than stored: a typo in the UI must not silently become a setting nothing
    reads."""
    from backend.database import set_setting

    current = get_settings()
    for section, values in (patch or {}).items():
        if section not in DEFAULTS or not isinstance(values, dict):
            continue
        for key, value in values.items():
            if key in DEFAULTS[section]:
                current[section][key] = value

    _validate(current)
    set_setting(SETTING_KEY, json.dumps(current, ensure_ascii=False))
    return current


def _validate(settings: dict[str, Any]) -> None:
    from backend.content_factory.domain import (
        ContentFactoryError,
        ErrorCode,
        PublishingMode,
        RENDER_PRESETS,
    )

    publishing = settings["publishing"]
    if publishing["mode"] not in PublishingMode.ALL:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, f"Unknown publishing mode: {publishing['mode']}", retryable=False
        )
    if settings["general"]["defaultPreset"] not in RENDER_PRESETS and settings["general"]["defaultPreset"] != "CUSTOM":
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "Unknown render preset", retryable=False
        )
    for key in ("maxCostPerRun", "dailyBudget", "monthlyBudget"):
        value = settings["budgets"][key]
        if value is not None and (not isinstance(value, (int, float)) or value < 0):
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, f"{key} must be a non-negative number or null", retryable=False
            )
    if settings["budgets"]["onExceeded"] not in {"stop", "approval", "cheaper"}:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "onExceeded must be stop, approval or cheaper", retryable=False
        )
    denylist = settings["policies"]["denylist"]
    if not isinstance(denylist, list) or any(not isinstance(item, str) for item in denylist):
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "denylist must be a list of strings", retryable=False
        )

    string_lists = (
        ("publishing", "defaultTags"),
        ("generation", "llmFallbackProviders"),
        ("generation", "imageFallbackProviders"),
        ("generation", "videoFallbackProviders"),
        ("generation", "voiceFallbackProviders"),
    )
    for section, key in string_lists:
        value = settings[section][key]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, f"{section}.{key} must be a list of strings", retryable=False
            )

    ranges = (
        ("general", "defaultDuration", 10, 3600),
        ("generation", "maxSources", 1, 50),
        ("generation", "temperature", 0, 2),
        ("generation", "maxTokens", 128, 128000),
        ("generation", "imageParallelism", 1, 16),
        ("generation", "clipDuration", 1, 60),
        ("generation", "providerTimeoutSeconds", 10, 7200),
        ("rendering", "musicGainDb", -60, 6),
        ("rendering", "loudnessTargetLufs", -30, -5),
        ("policies", "minQualityScore", 0, 1),
        ("policies", "minSources", 0, 50),
        ("policies", "maxSourceAgeDays", 1, 3650),
        ("retry", "maxAttempts", 1, 10),
        ("retry", "initialDelaySeconds", 0, 3600),
        ("retry", "maxDelaySeconds", 0, 86400),
        ("retry", "backoffMultiplier", 1, 10),
        ("network", "connectionTimeoutSeconds", 5, 600),
        ("network", "analyticsRefreshMinutes", 5, 10080),
        ("storage", "retentionDays", 1, 3650),
        ("storage", "signedUrlMinutes", 1, 10080),
        ("advanced", "pollSeconds", 1, 60),
        ("advanced", "maxConcurrentRuns", 1, 64),
        ("advanced", "maxParallelNodes", 1, 64),
        ("advanced", "leaseSeconds", 30, 3600),
        ("advanced", "logRetentionDays", 1, 3650),
    )
    for section, key, minimum, maximum in ranges:
        value = settings[section][key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED,
                f"{section}.{key} must be between {minimum} and {maximum}",
                retryable=False,
            )

    if settings["retry"]["initialDelaySeconds"] > settings["retry"]["maxDelaySeconds"]:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED,
            "retry.initialDelaySeconds cannot exceed retry.maxDelaySeconds",
            retryable=False,
        )
    if settings["rendering"]["subtitlePosition"] not in {"top", "middle", "bottom"}:
        raise ContentFactoryError(
            ErrorCode.VALIDATION_FAILED, "Unknown subtitle position", retryable=False
        )
