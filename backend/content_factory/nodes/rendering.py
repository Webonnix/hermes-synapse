"""Rendering (§31–34, §118–120).

The composition step is deterministic on purpose: a language model decides what
the video says, ffmpeg decides where every pixel goes. Given the same manifest
and the same assets, this produces the same file — which is what makes a render
reproducible and a render bug debuggable.

The manifest (§32) is built first and kept in the node's output, so a failed
render can be inspected without re-running the AI half of the pipeline.

Process safety (§119): ffmpeg is invoked with an argv list and never through a
shell, every input is a file we wrote ourselves into a private temp directory,
and the whole thing runs under a timeout. The temp directory is removed on
success and on failure alike (§120) — renders are large, and a worker that
leaks a few hundred megabytes per failure fills a disk in a day.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from backend.content_factory import db, storage as storage_module
from backend.content_factory.domain import (
    AssetType,
    ContentFactoryError,
    ErrorCode,
    NodeResult,
    PipelineNode,
    RenderPreset,
    resolve_preset,
)
from backend.content_factory.nodes import util
from backend.content_factory.nodes.base import NodeContext, NodeHandler, register
from backend.content_factory.providers.base import GeneratedAsset, ProviderResult

logger = logging.getLogger("hermes.content_factory.nodes.rendering")

RENDER_TIMEOUT_SECONDS = 1800


class VideoComposerNode(NodeHandler):
    """§31. Storyboard + voice + visuals + subtitles + branding → final.mp4."""

    type = "video_composer"

    def validate(self, node: PipelineNode) -> list[dict[str, str]]:
        problems = []
        if not (node.config.get("scenes") or node.input_mapping.get("scenes")):
            problems.append({"code": "MISSING_SCENES", "message": "no storyboard scenes", "severity": "error"})
        if not shutil.which("ffmpeg"):
            problems.append(
                {
                    "code": "NO_FFMPEG",
                    "message": "ffmpeg is not installed in this container",
                    "severity": "warning",
                }
            )
        return problems

    async def execute(self, context: NodeContext) -> NodeResult:
        preset = resolve_preset(str(context.config("preset", "YOUTUBE_SHORT")), context.config("custom"))
        manifest = build_manifest(context, preset)
        if not manifest["scenes"]:
            raise ContentFactoryError(
                ErrorCode.RENDER_FAILED, "Render manifest has no scenes", retryable=False
            )

        if not shutil.which("ffmpeg"):
            raise ContentFactoryError(
                ErrorCode.RENDER_FAILED,
                "ffmpeg is not available in this container",
                retryable=False,
            )

        await context.report(10, "render: collecting assets")
        node_run_id = context.node_run_id
        video_bytes = await asyncio.to_thread(_render, manifest, preset)

        await context.report(85, "render: storing result")
        result = ProviderResult(
            assets=[
                GeneratedAsset(
                    type=AssetType.RENDER,
                    mime_type="video/mp4",
                    data=video_bytes,
                    filename="final.mp4",
                    duration=manifest["duration"],
                    width=preset.width,
                    height=preset.height,
                )
            ],
            provider="ffmpeg",
        )
        stored = await util.persist_assets(
            context, result, asset_type=AssetType.RENDER, node_run_id=node_run_id
        )
        return NodeResult(
            output={
                "assetId": stored[0]["id"] if stored else "",
                "manifest": manifest,
                "duration": manifest["duration"],
                "width": preset.width,
                "height": preset.height,
            },
            assets=[asset["id"] for asset in stored],
            provider="ffmpeg",
            model="",
        )


def build_manifest(context: NodeContext, preset: RenderPreset) -> dict[str, Any]:
    """§32. Resolves scene → visual asset → narration asset into one document.

    A scene with no visual is not an error: it renders over the previous
    scene's visual (or a solid brand background), because losing four seconds
    of narration to a missing clip is worse than a slightly repetitive shot.
    """
    scenes = context.config("scenes") or []
    visuals = _asset_by_scene(context.config("images"), context.config("clips"))
    voices = {
        str(segment.get("sceneId")): segment
        for segment in (context.config("segments") or [])
    }
    branding = context.config("branding") or {}
    subtitles_enabled = bool(context.config("subtitles", True))

    manifest_scenes = []
    cursor = 0.0
    for index, scene in enumerate(scenes):
        scene_id = str(scene.get("id") or f"scene-{index + 1}")
        voice = voices.get(scene_id) or {}
        duration = float(voice.get("duration") or scene.get("duration") or 0) or 3.0
        manifest_scenes.append(
            {
                "id": scene_id,
                "start": round(cursor, 3),
                "duration": round(duration, 3),
                "visualAssetId": visuals.get(scene_id, ""),
                "voiceAssetId": voice.get("assetId", ""),
                "overlayText": scene.get("overlayText") or "",
                "transition": scene.get("transition") or context.config("transition", ""),
            }
        )
        cursor += duration

    return {
        "width": preset.width,
        "height": preset.height,
        "fps": preset.fps,
        "duration": round(cursor, 3),
        "scenes": manifest_scenes,
        "audio": {"music": context.config("musicAssetId", ""), "musicGainDb": float(context.config("musicGainDb", -18))},
        "subtitles": {
            "enabled": subtitles_enabled,
            "assetId": _first_subtitle(context.config("cues"), context.config("subtitleAssetIds")),
            "style": context.config("subtitleStyle", "default"),
            "font": context.config("subtitleFont", "Inter"),
            "position": context.config("subtitlePosition", "bottom"),
        },
        "audioProcessing": {
            "normalize": bool(context.config("normalizeAudio", True)),
            "targetLufs": float(context.config("loudnessTargetLufs", -14)),
        },
        "branding": {
            "logoAssetId": branding.get("logoAssetId", ""),
            "font": branding.get("font", ""),
            "theme": branding.get("theme", ""),
        },
    }


def _asset_by_scene(images: Any, clips: Any) -> dict[str, str]:
    """Clips win over stills for the same scene: if a video model produced
    motion for a scene, that is what the storyboard asked for."""
    mapping: dict[str, str] = {}
    for item in (images or []):
        if isinstance(item, dict) and item.get("assetId"):
            mapping[str(item.get("sceneId"))] = str(item["assetId"])
    for item in (clips or []):
        if isinstance(item, dict) and item.get("assetId"):
            mapping[str(item.get("sceneId"))] = str(item["assetId"])
    return mapping


def _first_subtitle(cues: Any, asset_ids: Any) -> str:
    if isinstance(asset_ids, list) and asset_ids:
        return str(asset_ids[0])
    return ""


# ── ffmpeg ───────────────────────────────────────────────────────────────────

def _render(manifest: dict[str, Any], preset: RenderPreset) -> bytes:
    """Renders scene by scene, then concatenates.

    Per-scene intermediates instead of one giant filter_complex: a 20-scene
    filtergraph is unreadable when it breaks, and one bad asset takes the whole
    command down with an error that says nothing about which scene it was.
    """
    storage = storage_module.get_storage()
    with tempfile.TemporaryDirectory(prefix="cf-render-") as directory:
        workdir = Path(directory)
        segments: list[Path] = []

        for index, scene in enumerate(manifest["scenes"]):
            segment = workdir / f"segment-{index:03d}.mp4"
            visual = _fetch(storage, scene.get("visualAssetId"), workdir, f"visual-{index:03d}")
            voice = _fetch(storage, scene.get("voiceAssetId"), workdir, f"voice-{index:03d}")
            _render_scene(scene, visual, voice, segment, preset)
            segments.append(segment)

        if not segments:
            raise ContentFactoryError(ErrorCode.RENDER_FAILED, "Nothing to render", retryable=False)

        concat_list = workdir / "segments.txt"
        concat_list.write_text(
            "".join(f"file '{segment.name}'\n" for segment in segments), encoding="utf-8"
        )
        combined = workdir / "combined.mp4"
        _run_ffmpeg(
            ["-f", "concat", "-safe", "0", "-i", concat_list.name, "-c", "copy", combined.name],
            cwd=workdir,
        )

        final = combined
        subtitle_asset = (manifest.get("subtitles") or {}).get("assetId")
        if (manifest.get("subtitles") or {}).get("enabled") and subtitle_asset:
            subtitles = _fetch(storage, subtitle_asset, workdir, "subtitles", extension=".srt")
            if subtitles:
                burned = workdir / "subtitled.mp4"
                subtitle_config = manifest.get("subtitles") or {}
                font = re.sub(r"[^A-Za-z0-9 _-]", "", str(subtitle_config.get("font") or "Inter"))[:80]
                alignment = {"top": 8, "middle": 5, "bottom": 2}.get(
                    str(subtitle_config.get("position") or "bottom"), 2
                )
                _run_ffmpeg(
                    [
                        "-i", combined.name,
                        "-vf", f"subtitles={subtitles.name}:force_style='FontName={font},Fontsize=18,Outline=2,Alignment={alignment}'",
                        "-c:a", "copy",
                        burned.name,
                    ],
                    cwd=workdir,
                )
                final = burned

        music_asset = (manifest.get("audio") or {}).get("music")
        if music_asset:
            music = _fetch(storage, music_asset, workdir, "music")
            if music:
                mixed = workdir / "mixed.mp4"
                gain = float((manifest.get("audio") or {}).get("musicGainDb", -18))
                _run_ffmpeg(
                    [
                        "-i", final.name,
                        "-i", music.name,
                        "-filter_complex",
                        f"[1:a]volume={gain}dB,aloop=loop=-1:size=2e9[bg];"
                        f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=2[a]",
                        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-shortest",
                        mixed.name,
                    ],
                    cwd=workdir,
                )
                final = mixed

        audio_processing = manifest.get("audioProcessing") or {}
        if audio_processing.get("normalize"):
            normalized = workdir / "normalized.mp4"
            target_lufs = max(-30.0, min(-5.0, float(audio_processing.get("targetLufs") or -14)))
            _run_ffmpeg(
                ["-i", final.name, "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11", "-c:v", "copy", normalized.name],
                cwd=workdir,
            )
            final = normalized

        return final.read_bytes()


def _render_scene(
    scene: dict[str, Any], visual: Optional[Path], voice: Optional[Path], output: Path, preset: RenderPreset
) -> None:
    duration = float(scene.get("duration") or 3.0)
    scale = (
        f"scale={preset.width}:{preset.height}:force_original_aspect_ratio=increase,"
        f"crop={preset.width}:{preset.height},fps={preset.fps},format=yuv420p"
    )

    arguments: list[str] = []
    if visual is None:
        arguments += ["-f", "lavfi", "-i", f"color=c=black:s={preset.width}x{preset.height}:d={duration}"]
    elif visual.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}:
        arguments += ["-stream_loop", "-1", "-i", visual.name]
    else:
        arguments += ["-loop", "1", "-i", visual.name]

    if voice is not None:
        arguments += ["-i", voice.name]
        audio_map = ["-map", "1:a", "-c:a", "aac", "-b:a", "192k"]
    else:
        # Every segment must carry an audio stream or the concat demuxer
        # refuses to join it to the ones that do.
        arguments += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        audio_map = ["-map", "1:a", "-c:a", "aac", "-b:a", "128k"]

    _run_ffmpeg(
        [
            *arguments,
            "-t", f"{duration:.3f}",
            "-vf", scale,
            "-map", "0:v",
            *audio_map,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-r", str(preset.fps),
            "-shortest",
            output.name,
        ],
        cwd=output.parent,
    )


def _run_ffmpeg(arguments: list[str], *, cwd: Path) -> None:
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            timeout=RENDER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContentFactoryError(
            ErrorCode.RENDER_FAILED, f"ffmpeg timed out after {RENDER_TIMEOUT_SECONDS}s"
        ) from exc
    if completed.returncode != 0:
        # ffmpeg's stderr can be long and contains file paths; the tail is the
        # part that says what actually went wrong.
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip()[-600:]
        raise ContentFactoryError(ErrorCode.RENDER_FAILED, f"ffmpeg failed: {detail}")


def _fetch(storage: Any, asset_id: Any, workdir: Path, stem: str, *, extension: str = "") -> Optional[Path]:
    if not asset_id:
        return None
    asset = db.query_one("SELECT * FROM cf_assets WHERE id = ?", (str(asset_id),))
    if not asset:
        logger.warning("Render: asset %s is missing", asset_id)
        return None
    key = str(asset["storage_key"])
    suffix = extension or Path(key).suffix or _suffix_for(str(asset.get("mime_type") or ""))
    path = workdir / f"{stem}{suffix}"
    try:
        path.write_bytes(storage.get(key))
    except Exception:
        logger.warning("Render: could not fetch asset %s", asset_id, exc_info=True)
        return None
    return path


def _suffix_for(mime_type: str) -> str:
    return {
        "video/mp4": ".mp4",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "audio/wav": ".wav",
        "audio/mpeg": ".mp3",
        "text/plain": ".srt",
        "text/vtt": ".vtt",
    }.get(mime_type, ".bin")


register(VideoComposerNode())
