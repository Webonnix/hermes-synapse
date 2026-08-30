"""Media nodes (§13 MEDIA category).

Voice and image finish in seconds and run inline. Video does not: a clip takes
minutes, so `VideoGenerationNode` submits the job, hands the id back and lets
the engine park the node in WAITING_EXTERNAL (§15) until `poll()` says it is
done. That id is written to the database before the first poll, which is what
makes a paid job survive a backend restart instead of being abandoned or
re-submitted.

A storyboard scene fans out into one media call per scene. Those calls run
concurrently, bounded by the provider's own concurrency limit (§92), because
generating twenty clips strictly one after another would make a 45-second video
take most of an hour.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from backend.content_factory.domain import (
    AssetType,
    ContentFactoryError,
    ErrorCode,
    NodeResult,
    PipelineNode,
    resolve_preset,
)
from backend.content_factory.nodes import util
from backend.content_factory.nodes.base import NodeContext, NodeHandler, register
from backend.content_factory.providers.base import JobStatus, ProviderCategory

logger = logging.getLogger("hermes.content_factory.nodes.media")


class VoiceGenerationNode(NodeHandler):
    """§27. One narration track per storyboard scene, plus the concatenated
    text so a subtitle node can align against it."""

    type = "voice_generation"
    provider_category = ProviderCategory.VOICE

    def validate(self, node: PipelineNode) -> list[dict[str, str]]:
        if not (node.config.get("text") or node.config.get("scenes") or node.input_mapping):
            return [{"code": "MISSING_INPUT", "message": "needs text or scenes", "severity": "error"}]
        return []

    async def execute(self, context: NodeContext) -> NodeResult:
        provider = util.pick_provider(context, ProviderCategory.VOICE)
        scenes = context.config("scenes") or []
        node_run_id = context.node_run_id

        if scenes:
            segments = [
                {"id": scene.get("id") or f"scene-{index + 1}", "text": str(scene.get("narration") or "")}
                for index, scene in enumerate(scenes)
            ]
        else:
            segments = [{"id": "full", "text": str(context.require("text"))}]

        voice = context.config("voice") or ""
        rate = float(context.config("rate", 1.0))
        total_cost = 0.0
        assets: list[dict[str, Any]] = []
        durations: list[dict[str, Any]] = []

        for index, segment in enumerate(segments):
            if not segment["text"].strip():
                continue
            result = await util.run_provider(
                provider, {"text": segment["text"], "voice": voice, "rate": rate}
            )
            stored = await util.persist_assets(
                context, result, asset_type=AssetType.VOICE, node_run_id=node_run_id
            )
            util.record_usage(context, node_run_id, result, operation="voice")
            total_cost += result.cost
            assets.extend(stored)
            durations.append(
                {
                    "sceneId": segment["id"],
                    "assetId": stored[0]["id"] if stored else "",
                    "duration": result.output.get("duration"),
                    "alignment": result.output.get("alignment"),
                }
            )
            await context.report(
                int((index + 1) / max(len(segments), 1) * 100), f"voice {index + 1}/{len(segments)}"
            )

        if not assets:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, "Nothing to narrate — every segment was empty", retryable=False
            )

        return NodeResult(
            output={
                "segments": durations,
                "assetIds": [asset["id"] for asset in assets],
                "totalDuration": sum(float(item["duration"] or 0) for item in durations),
            },
            cost=total_cost,
            provider=provider.id,
            model=provider.model,
            assets=[asset["id"] for asset in assets],
        )


class ImageGenerationNode(NodeHandler):
    """§26. One image per scene that asked for a still."""

    type = "image_generation"
    provider_category = ProviderCategory.IMAGE

    async def execute(self, context: NodeContext) -> NodeResult:
        provider = util.pick_provider(context, ProviderCategory.IMAGE)
        node_run_id = context.node_run_id
        preset = resolve_preset(str(context.config("preset", "YOUTUBE_SHORT")), context.config("custom"))
        prompts = _scene_prompts(context, want="image")

        if not prompts:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, "No image prompts to render", retryable=False
            )

        semaphore = asyncio.Semaphore(int(context.config("parallel", 3)))

        async def one(index: int, item: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                result = await util.run_provider(
                    provider,
                    {
                        "prompt": item["prompt"],
                        "width": preset.width,
                        "height": preset.height,
                        "style_preset": context.config("stylePreset", ""),
                        "negative_prompt": context.config("negativePrompt", ""),
                    },
                )
                stored = await util.persist_assets(
                    context, result, asset_type=AssetType.IMAGE, node_run_id=node_run_id
                )
                util.record_usage(context, node_run_id, result, operation="image")
                await context.report(int((index + 1) / len(prompts) * 100), f"image {index + 1}/{len(prompts)}")
                return {
                    "sceneId": item["sceneId"],
                    "assetId": stored[0]["id"] if stored else "",
                    "cost": result.cost,
                }

        produced = await asyncio.gather(*(one(index, item) for index, item in enumerate(prompts)))
        return NodeResult(
            output={"images": produced, "assetIds": [item["assetId"] for item in produced]},
            cost=sum(float(item["cost"]) for item in produced),
            provider=provider.id,
            model=provider.model,
            assets=[item["assetId"] for item in produced if item["assetId"]],
        )


class VideoGenerationNode(NodeHandler):
    """§25. Submitted jobs, polled by the engine.

    One node covers all the clips a storyboard needs: every submitted job id is
    carried in the node's own state, so a restart resumes the whole batch.
    """

    type = "video_generation"
    provider_category = ProviderCategory.VIDEO

    async def execute(self, context: NodeContext) -> NodeResult:
        provider = util.pick_provider(context, ProviderCategory.VIDEO)
        preset = resolve_preset(str(context.config("preset", "YOUTUBE_SHORT")), context.config("custom"))
        prompts = _scene_prompts(context, want="video")
        if not prompts:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, "No video prompts to generate", retryable=False
            )

        if not provider.is_async_job:
            # A provider that answers immediately needs no parking.
            return await self._inline(context, provider, prompts, preset)

        jobs = []
        for item in prompts:
            job = await provider.submit(
                {
                    "prompt": item["prompt"],
                    "duration": item.get("duration") or context.config("clipDuration", 5),
                    "aspect_ratio": preset.aspect_ratio,
                    "model": context.config("model", ""),
                    "version": context.config("version", ""),
                }
            )
            jobs.append({"sceneId": item["sceneId"], "jobId": job.external_job_id})
            await context.report(10, f"submitted {len(jobs)}/{len(prompts)}")

        return NodeResult(
            output={"jobs": jobs, "providerId": provider.id},
            provider=provider.id,
            model=provider.model,
            # A single id is what the engine persists for resume; the full list
            # lives in the output, which is persisted alongside it.
            external_job_id=jobs[0]["jobId"] if jobs else "",
            poll_after=float(context.config("pollInterval", 15)),
        )

    async def poll(self, context: NodeContext) -> Optional[NodeResult]:
        provider = util.pick_provider(context, ProviderCategory.VIDEO)
        node_run_id = context.node_run_id
        jobs = list(context.inputs.get("jobs") or [])
        if not jobs and context.external_job_id:
            jobs = [{"sceneId": "full", "jobId": context.external_job_id}]

        statuses = await asyncio.gather(
            *(provider.get_status(job["jobId"]) for job in jobs), return_exceptions=True
        )
        done, failed = 0, []
        for job, status in zip(jobs, statuses):
            if isinstance(status, Exception):
                # A transient status failure must not fail a job that is very
                # likely still running and already paid for.
                logger.warning("Video status check failed for %s: %s", job["jobId"], status)
                continue
            if status.status == JobStatus.COMPLETED:
                done += 1
            elif status.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                failed.append((job, status))

        if failed:
            job, status = failed[0]
            raise status.error or ContentFactoryError(
                ErrorCode.PROVIDER_ERROR, f"Video job {job['jobId']} failed"
            )
        await context.report(int(done / max(len(jobs), 1) * 100), f"{done}/{len(jobs)} clips ready")
        if done < len(jobs):
            return None

        clips, total_cost = [], 0.0
        for job in jobs:
            result = await provider.get_result(job["jobId"])
            stored = await util.persist_assets(
                context, result, asset_type=AssetType.VIDEO, node_run_id=node_run_id
            )
            util.record_usage(context, node_run_id, result, operation="video")
            total_cost += result.cost
            clips.append(
                {
                    "sceneId": job["sceneId"],
                    "assetId": stored[0]["id"] if stored else "",
                    "duration": result.output.get("duration"),
                }
            )

        return NodeResult(
            output={"clips": clips, "assetIds": [clip["assetId"] for clip in clips]},
            cost=total_cost,
            provider=provider.id,
            model=provider.model,
            assets=[clip["assetId"] for clip in clips if clip["assetId"]],
        )

    async def cancel(self, context: NodeContext) -> None:
        try:
            provider = util.pick_provider(context, ProviderCategory.VIDEO)
        except ContentFactoryError:
            return
        for job in context.inputs.get("jobs") or []:
            await provider.cancel(job.get("jobId", ""))

    async def _inline(
        self, context: NodeContext, provider: Any, prompts: list[dict[str, Any]], preset: Any
    ) -> NodeResult:
        node_run_id = context.node_run_id
        clips, total_cost = [], 0.0
        for index, item in enumerate(prompts):
            result = await util.run_provider(
                provider,
                {
                    "prompt": item["prompt"],
                    "duration": item.get("duration") or context.config("clipDuration", 5),
                    "aspect_ratio": preset.aspect_ratio,
                },
            )
            stored = await util.persist_assets(
                context, result, asset_type=AssetType.VIDEO, node_run_id=node_run_id
            )
            util.record_usage(context, node_run_id, result, operation="video")
            total_cost += result.cost
            clips.append(
                {
                    "sceneId": item["sceneId"],
                    "assetId": stored[0]["id"] if stored else "",
                    "duration": result.output.get("duration"),
                }
            )
            await context.report(int((index + 1) / len(prompts) * 100), f"clip {index + 1}/{len(prompts)}")
        return NodeResult(
            output={"clips": clips, "assetIds": [clip["assetId"] for clip in clips]},
            cost=total_cost,
            provider=provider.id,
            model=provider.model,
            assets=[clip["assetId"] for clip in clips if clip["assetId"]],
        )


class SubtitleGenerationNode(NodeHandler):
    """§34. Word timings when the voice provider supplied them, proportional
    timing when it did not — subtitles that are slightly off beat the
    alternative of a video with no subtitles at all."""

    type = "subtitle_generation"

    async def execute(self, context: NodeContext) -> NodeResult:
        node_run_id = context.node_run_id
        segments = context.config("segments") or []
        scenes = context.config("scenes") or []
        if not segments and not scenes:
            raise ContentFactoryError(
                ErrorCode.VALIDATION_FAILED, "Subtitles need voice segments or scenes", retryable=False
            )

        cues = _build_cues(segments, scenes)
        srt = _to_srt(cues)
        vtt = "WEBVTT\n\n" + _to_srt(cues, vtt=True)

        from backend.content_factory.providers.base import GeneratedAsset, ProviderResult

        result = ProviderResult(
            assets=[
                GeneratedAsset(type=AssetType.SUBTITLE, mime_type="text/plain", data=srt.encode("utf-8"),
                               filename="subtitles.srt"),
                GeneratedAsset(type=AssetType.SUBTITLE, mime_type="text/vtt", data=vtt.encode("utf-8"),
                               filename="subtitles.vtt"),
            ],
            provider="internal",
        )
        stored = await util.persist_assets(
            context, result, asset_type=AssetType.SUBTITLE, node_run_id=node_run_id
        )
        return NodeResult(
            output={"cues": cues, "assetIds": [asset["id"] for asset in stored]},
            assets=[asset["id"] for asset in stored],
            provider="internal",
        )


def _scene_prompts(context: NodeContext, *, want: str) -> list[dict[str, Any]]:
    """Scene visual prompts, filtered to the ones this node should produce.

    A storyboard marks each scene `visualType: image|video`; a node configured
    with `allScenes` ignores that and takes everything, which is how a pipeline
    that only has an image provider still produces a complete video.
    """
    explicit = context.config("prompts")
    if explicit:
        return [
            {"sceneId": item.get("sceneId", f"scene-{index + 1}"), "prompt": str(item.get("prompt", "")),
             "duration": item.get("duration")}
            for index, item in enumerate(explicit)
        ]

    scenes = context.config("scenes") or []
    take_all = bool(context.config("allScenes", False))
    prompts = []
    for index, scene in enumerate(scenes):
        visual_type = str(scene.get("visualType") or "image").lower()
        if not take_all and visual_type != want:
            continue
        prompt = str(scene.get("visualPrompt") or "").strip()
        if not prompt:
            continue
        prompts.append(
            {
                "sceneId": scene.get("id") or f"scene-{index + 1}",
                "prompt": prompt,
                "duration": scene.get("duration"),
            }
        )
    return prompts


def _build_cues(segments: list[dict[str, Any]], scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_scene = {str(segment.get("sceneId")): segment for segment in segments}
    cues: list[dict[str, Any]] = []
    cursor = 0.0
    for index, scene in enumerate(scenes or []):
        scene_id = str(scene.get("id") or f"scene-{index + 1}")
        text = str(scene.get("narration") or "").strip()
        segment = by_scene.get(scene_id) or {}
        duration = float(segment.get("duration") or scene.get("duration") or 0) or 3.0
        alignment = segment.get("alignment") or {}
        if alignment and text:
            cues.extend(_cues_from_alignment(text, alignment, cursor))
        elif text:
            cues.append({"start": round(cursor, 3), "end": round(cursor + duration, 3), "text": text})
        cursor += duration
    if not scenes:
        for segment in segments:
            duration = float(segment.get("duration") or 3.0)
            cues.append({"start": round(cursor, 3), "end": round(cursor + duration, 3),
                         "text": str(segment.get("text") or "")})
            cursor += duration
    return cues


def _cues_from_alignment(text: str, alignment: dict[str, Any], offset: float) -> list[dict[str, Any]]:
    """Groups character timings into short, readable lines rather than one cue
    per word: burned-in subtitles that flicker word by word are harder to read
    than 3-5 word chunks."""
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    characters = alignment.get("characters") or list(text)
    if not starts or len(starts) != len(characters):
        return [{"start": round(offset, 3), "end": round(offset + 3.0, 3), "text": text}]

    cues: list[dict[str, Any]] = []
    chunk, chunk_start, words = [], starts[0], 0
    for index, character in enumerate(characters):
        chunk.append(character)
        if character == " ":
            words += 1
        if words >= 4 or index == len(characters) - 1:
            cues.append(
                {
                    "start": round(offset + chunk_start, 3),
                    "end": round(offset + ends[index], 3),
                    "text": "".join(chunk).strip(),
                }
            )
            chunk, words = [], 0
            if index + 1 < len(starts):
                chunk_start = starts[index + 1]
    return [cue for cue in cues if cue["text"]]


def _to_srt(cues: list[dict[str, Any]], *, vtt: bool = False) -> str:
    def stamp(seconds: float) -> str:
        hours, remainder = divmod(max(0.0, float(seconds)), 3600)
        minutes, secs = divmod(remainder, 60)
        separator = "." if vtt else ","
        return f"{int(hours):02d}:{int(minutes):02d}:{int(secs):02d}{separator}{int((secs % 1) * 1000):03d}"

    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(f"{index}\n{stamp(cue['start'])} --> {stamp(cue['end'])}\n{cue['text']}\n")
    return "\n".join(blocks)


for handler in (
    VoiceGenerationNode(), ImageGenerationNode(), VideoGenerationNode(), SubtitleGenerationNode(),
):
    register(handler)
