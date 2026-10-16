"""Upload naming safety and content snapshots."""

import io
from pathlib import Path

import pytest

from backend import media_snapshots as ms


@pytest.mark.parametrize("raw, expected", [
    ("report.csv", "report.csv"),
    ("../../etc/passwd", "passwd"),
    ("/etc/passwd", "passwd"),
    ("..\\..\\windows\\system32\\cfg.ini", "cfg.ini"),
    ("отчёт за июль.xlsx", "отчёт за июль.xlsx"),
    ("with\x00null.csv", "with_null.csv"),
    ("..", "upload.bin"),
    (".", "upload.bin"),
    ("", "upload.bin"),
    (None, "upload.bin"),
    (".hidden", "hidden"),
    ("a:b*c?.txt", "a_b_c_.txt"),
])
def test_filenames_are_reduced_to_one_safe_segment(raw, expected):
    result = ms.safe_filename(raw)
    assert result == expected
    assert "/" not in result and "\\" not in result
    assert result not in ("", ".", "..")


def test_long_names_are_truncated_but_keep_their_extension():
    name = ("x" * 400) + ".csv"
    result = ms.safe_filename(name)
    assert len(result) <= ms.MAX_NAME_LENGTH
    assert result.endswith(".csv")


def test_resolution_is_checked_after_the_filesystem_resolves_it(tmp_path):
    """A textual check is not enough: '..' and symlinks are resolved by the
    filesystem, so containment has to be asserted on the resolved path."""
    assert ms.resolve_within(tmp_path, "ok.csv").parent == tmp_path.resolve()
    with pytest.raises(ValueError):
        ms.resolve_within(tmp_path, "../escaped.csv")
    with pytest.raises(ValueError):
        ms.resolve_within(tmp_path, "/etc/passwd")


def test_a_traversing_name_can_never_write_outside_the_directory(tmp_path):
    uploads = tmp_path / "uploads"
    sentinel = tmp_path / "passwd"
    sentinel.write_text("original")

    stored = ms.store_upload(uploads, "../passwd", io.BytesIO(b"attacker"))

    assert sentinel.read_text() == "original"
    assert Path(stored["path"]).parent == uploads.resolve()
    assert stored["filename"] == "passwd"


def test_reupload_preserves_the_bytes_it_replaces(tmp_path):
    """Uploads are served by name, so overwriting one silently changes what
    every earlier chat message referencing that name now shows."""
    uploads = tmp_path / "uploads"
    ms.store_upload(uploads, "report.csv", io.BytesIO(b"month,revenue\nJan,100\n"))
    second = ms.store_upload(uploads, "report.csv", io.BytesIO(b"month,revenue\nJan,999\n"))

    assert (uploads / "report.csv").read_bytes() == b"month,revenue\nJan,999\n"
    assert second["replaced_snapshot"] is not None
    archived = uploads / ms.SNAPSHOT_DIR_NAME / second["replaced_snapshot"]
    assert archived.read_bytes() == b"month,revenue\nJan,100\n"


def test_first_upload_of_a_name_archives_nothing(tmp_path):
    stored = ms.store_upload(tmp_path / "uploads", "new.csv", io.BytesIO(b"x"))
    assert stored["replaced_snapshot"] is None


def test_identical_reupload_does_not_pile_up_copies(tmp_path):
    uploads = tmp_path / "uploads"
    for _ in range(3):
        ms.store_upload(uploads, "same.csv", io.BytesIO(b"identical"))
    snapshots = list((uploads / ms.SNAPSHOT_DIR_NAME).iterdir())
    assert len(snapshots) == 1


def test_a_failed_write_leaves_no_partial_file_under_the_real_name(tmp_path):
    uploads = tmp_path / "uploads"
    ms.store_upload(uploads, "good.csv", io.BytesIO(b"complete"))

    class Exploding(io.BytesIO):
        def read(self, *args, **kwargs):
            raise OSError("connection reset")

    with pytest.raises(OSError):
        ms.store_upload(uploads, "good.csv", Exploding(b""))

    # The previous complete file is still intact and there is no stray partial.
    assert (uploads / "good.csv").read_bytes() == b"complete"
    assert [p.name for p in uploads.iterdir() if p.name.startswith(".")] == []


def test_snapshotting_a_missing_file_is_a_no_op(tmp_path):
    assert ms.snapshot_existing(tmp_path / "nope.csv") is None


def test_a_snapshot_failure_never_blocks_the_upload(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    ms.store_upload(uploads, "a.csv", io.BytesIO(b"first"))
    monkeypatch.setattr(ms, "_digest", lambda path: (_ for _ in ()).throw(OSError("no read")))

    stored = ms.store_upload(uploads, "a.csv", io.BytesIO(b"second"))

    assert stored["replaced_snapshot"] is None
    assert (uploads / "a.csv").read_bytes() == b"second"
