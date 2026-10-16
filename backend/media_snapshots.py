"""Safe upload naming and content snapshots.

Two problems with writing an upload straight to ``uploads/<client filename>``:

1. **The name is attacker-controlled.** ``os.path.join(base, name)`` does not
   contain anything: ``../../x`` walks out of the directory, and an absolute
   name discards ``base`` entirely. The name has to be reduced to a safe
   basename and then verified to still resolve inside the directory — checking
   the string is not enough, because symlinks and ``..`` are resolved by the
   filesystem, not by the check.

2. **A re-upload rewrites history.** Files are served by name, so uploading a
   new ``report.csv`` silently changes what every earlier chat message that
   referenced ``report.csv`` now shows. The old bytes are gone and the user
   loses the before/after they had.

The fix for (2) keeps the current name working — the agent and its prompts
address uploads by their human name — while preserving the bytes it is about to
replace under a content-addressed snapshot, so an old reference can still be
resolved and nothing is silently lost.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, BinaryIO, Dict, Optional

logger = logging.getLogger("hermes.media")

SNAPSHOT_DIR_NAME = "_snapshots"
MAX_NAME_LENGTH = 180
# Control characters, path separators, and the Windows-reserved set. Anything
# else is allowed: a user's file can legitimately be named in any language.
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


def safe_filename(raw: Optional[str], fallback: str = "upload.bin") -> str:
    """Reduce a client-supplied name to a single safe path segment.

    Never returns '', '.', '..', a name starting with '.', or anything holding a
    path separator.
    """
    candidate = (raw or "").strip()
    # Take the last segment under BOTH separators: a Windows client sends
    # backslashes, which posixpath treats as an ordinary character.
    candidate = candidate.replace("\\", "/").split("/")[-1]
    candidate = _UNSAFE_CHARS.sub("_", candidate).strip().strip(".")
    if not candidate or candidate in (".", ".."):
        return fallback
    if len(candidate) > MAX_NAME_LENGTH:
        stem, dot, suffix = candidate.rpartition(".")
        if dot and len(suffix) <= 12:
            candidate = stem[: MAX_NAME_LENGTH - len(suffix) - 1] + "." + suffix
        else:
            candidate = candidate[:MAX_NAME_LENGTH]
    return candidate


def resolve_within(directory: Path, filename: str) -> Path:
    """Resolve ``filename`` inside ``directory`` or raise.

    The check runs on the *resolved* path, because ``..`` and symlinks are
    resolved by the filesystem and a purely textual check would miss both.
    """
    directory = Path(directory).resolve()
    target = (directory / filename).resolve()
    if target != directory and directory not in target.parents:
        raise ValueError(f"Refusing to write outside the upload directory: {filename!r}")
    return target


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def snapshot_existing(path: Path) -> Optional[str]:
    """Preserve the bytes currently at ``path`` before they are overwritten.

    Returns the snapshot's filename, or None when there is nothing to preserve.
    Content-addressed, so re-uploading identical bytes does not pile up copies.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        digest = _digest(path)[:12]
        snapshot_dir = path.parent / SNAPSHOT_DIR_NAME
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_name = f"{digest}-{path.name}"
        snapshot_path = snapshot_dir / snapshot_name
        if not snapshot_path.exists():
            shutil.copy2(path, snapshot_path)
        return snapshot_name
    except Exception as exc:
        # Best effort: failing to archive the previous copy must not block the
        # new upload, but it must be visible.
        logger.warning("Could not snapshot %s before overwrite: %s", path, exc)
        return None


def store_upload(uploads_dir: Any, raw_filename: Optional[str], source: BinaryIO) -> Dict[str, Any]:
    """Write an upload safely and archive whatever it replaces.

    Returns ``{filename, path, replaced_snapshot}``. ``replaced_snapshot`` is the
    archived copy of the previous file with that name, or None.
    """
    directory = Path(uploads_dir)
    directory.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(raw_filename)
    target = resolve_within(directory, filename)

    replaced = snapshot_existing(target)
    # Write to a temp file in the same directory and rename into place, so a
    # reader never sees a half-written file under a name that already worked.
    staging = target.with_name(f".{target.name}.partial")
    try:
        with staging.open("wb") as handle:
            shutil.copyfileobj(source, handle)
        os.replace(staging, target)
    finally:
        if staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass
    if replaced:
        logger.info("Upload %s replaced an earlier file; previous bytes kept as %s", filename, replaced)
    return {"filename": filename, "path": str(target), "replaced_snapshot": replaced}
