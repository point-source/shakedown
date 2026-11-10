"""Core-owned file selection, applied to every source's raw listing
(SPEC §spec:per-item-isolation: one file-selection rule for every source).

These are the rules the IA plugin used to apply on its own, moved unchanged:
format and exclude filters, refused files left out, and the opt-in fallback for a
show whose wanted files are all refused."""
from __future__ import annotations

from shakedown.config import CollectionConfig
from shakedown.discovery import SourceFile
from shakedown.selection import DOWNLOAD_REFUSED_REASON, FALLBACK_REASON, select


def _coll(**kw) -> CollectionConfig:
    return CollectionConfig(name="c", query="q", **kw)


def _f(name: str, fmt: str | None = None, *, refused: bool = False) -> SourceFile:
    return SourceFile(name=name, size=1, md5=None, format=fmt, refused=refused)


STREAM_ONLY = (
    _f("d1t01.shn", "Shorten", refused=True),
    _f("d1t01.mp3", "VBR MP3"),
    _f("info.txt", "Text"),
)


def _names(selection) -> list[str]:
    return [f.name for f in selection.manifest.files]


def test_no_filters_keeps_every_served_file_sorted_by_name() -> None:
    chosen = select((_f("b.txt"), _f("a.flac")), None, _coll())
    assert _names(chosen) == ["a.flac", "b.txt"]
    assert chosen.is_restricted is False
    assert chosen.restriction_reason is None


def test_format_filter_matches_format_or_extension_case_insensitively() -> None:
    files = (_f("t1.flac", "Flac"), _f("t1_64kb.mp3", "VBR MP3"), _f("t2.FLAC"), _f("x.txt"))
    assert _names(select(files, None, _coll(format_filters=["flac"]))) == ["t1.flac", "t2.FLAC"]
    assert _names(select(files, None, _coll(format_filters=["vbr mp3"]))) == ["t1_64kb.mp3"]


def test_exclude_filters_win_over_format_filters() -> None:
    files = (_f("t1.flac"), _f("t1_sample.flac"))
    chosen = select(files, None, _coll(format_filters=["flac"], exclude_filters=["*_sample.*"]))
    assert _names(chosen) == ["t1.flac"]


def test_manifest_carries_size_and_checksum() -> None:
    files = (SourceFile(name="a.flac", size=10, md5="ab" * 16),)
    (entry,) = select(files, None, _coll()).manifest.files
    assert (entry.size, entry.md5) == (10, "ab" * 16)


def test_item_restriction_marks_the_item_restricted() -> None:
    chosen = select((_f("a.flac"),), "access-restricted", _coll())
    assert chosen.is_restricted is True
    assert chosen.restriction_reason == "access-restricted"


def test_only_refused_files_wanted_makes_the_show_unavailable() -> None:
    chosen = select(STREAM_ONLY, None, _coll(format_filters=["shn"]))
    assert chosen.is_restricted is True
    assert chosen.restriction_reason == DOWNLOAD_REFUSED_REASON


def test_files_the_source_serves_are_still_mirrored() -> None:
    chosen = select(STREAM_ONLY, None, _coll(format_filters=["mp3"]))
    assert chosen.is_restricted is False
    assert _names(chosen) == ["d1t01.mp3"]


def test_refused_files_drop_out_of_a_mixed_format_manifest() -> None:
    files = (_f("d1t01.shn", "Shorten", refused=True), _f("d1t01.flac", "Flac"))
    chosen = select(files, None, _coll(format_filters=["flac", "shn"]))
    assert _names(chosen) == ["d1t01.flac"]
    assert chosen.restriction_reason is None


def test_refused_lossless_falls_back_to_the_opted_in_formats() -> None:
    chosen = select(
        STREAM_ONLY, None, _coll(format_filters=["shn"], fallback_format_filters=["mp3"])
    )
    assert chosen.is_restricted is False
    assert _names(chosen) == ["d1t01.mp3"]
    assert chosen.restriction_reason == FALLBACK_REASON


def test_fallback_is_unused_while_the_wanted_files_are_served() -> None:
    files = (_f("d1t01.shn", "Shorten"), _f("d1t01.mp3", "VBR MP3"))
    chosen = select(files, None, _coll(format_filters=["shn"], fallback_format_filters=["mp3"]))
    assert _names(chosen) == ["d1t01.shn"]
    assert chosen.restriction_reason is None


def test_refused_show_without_a_served_fallback_stays_unavailable() -> None:
    chosen = select(
        STREAM_ONLY, None, _coll(format_filters=["shn"], fallback_format_filters=["flac"])
    )
    assert chosen.is_restricted is True
    assert chosen.restriction_reason == DOWNLOAD_REFUSED_REASON


def test_nothing_matching_and_nothing_refused_is_an_empty_manifest() -> None:
    """Not a refusal: the planner records it as 'lists no files' and keeps the copy."""
    chosen = select((_f("a.txt"),), None, _coll(format_filters=["flac"]))
    assert chosen.manifest.files == ()
    assert chosen.is_restricted is False
