"""Core-owned file selection: which of an item's listed files to mirror and stage.

One rule for every source (§spec:per-item-isolation), following the product's two
goals (§spec:quality-selection): keep the highest-quality copy the source offers
(*best available*) and the highest-quality copy a common player reads (*best
playable*) — one copy when they are the same format, both when they differ — plus
every non-audio file. The archive holds all of it; the library holds the best
playable copy and the non-audio files. Shakedown never converts audio.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

from shakedown.models import Manifest, ManifestFile, SourceFile

# Recorded as the item's restriction_reason when archive.org refuses a download.
DOWNLOAD_REFUSED_REASON = "download refused by archive.org (stream-only or restricted)"
# Recorded on a `complete` show whose lossless files the source refuses (SPEC verbatim).
LOSSLESS_REFUSED_REASON = "lossless refused by archive.org; best lossy copy mirrored instead"
# Recorded on a `complete` show whose only audio no common player reads
# (operator-approved copy).
NOT_PLAYABLE_SHN_REASON = "only in SHN; archive.org offers no playable copy"
NOT_PLAYABLE_UNRECOGNISED_REASON = (
    "only in an unrecognised audio format; archive.org offers no playable copy"
)


class _Class(IntEnum):
    """Quality class of an audio format; higher ranks first."""
    UNRECOGNISED = 0
    LOSSY = 1
    LOSSLESS = 2


# Recognised audio by file extension. An explicit table rather than `mimetypes`:
# the stdlib reads the host's mime tables, so its answer differs between machines.
_LOSSLESS = frozenset({"flac", "shn", "wav", "aif", "aiff"})
_LOSSY = frozenset({"mp3", "ogg", "opus", "aac"})
# `.m4a` holds either Apple Lossless or AAC; the source's format name tells them apart.
_EITHER = frozenset({"m4a"})
# Lossless that common players cannot read.
_UNPLAYABLE = frozenset({"shn"})
# Audio the product does not rank: kept only when nothing recognised covers the show.
_OTHER_AUDIO = frozenset({"ape", "wv", "wma", "mpc", "tta", "ra", "mp2", "dsf", "dff", "au"})
# Among lossless copies, this order first, then larger size per track (operator,
# 2026-11-10): 24-bit FLAC over 16-bit, never WAV over FLAC of the same audio.
_LOSSLESS_ORDER = {"flac": 0, "m4a": 1, "wav": 2, "aif": 3, "aiff": 3, "shn": 4}
# Words in an archive.org format name that mark a ZIP of the show's audio
# ("VBR ZIP", "64Kbps MP3 ZIP"): it repeats the music, so it is left out (operator,
# 2026-11-10).
_AUDIO_ZIP_WORDS = ("mp3", "vbr", "ogg", "flac", "shorten", "wav", "aiff", "kbps", "lossless")


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _audio_class(f: SourceFile) -> _Class | None:
    """The file's audio class, or None for a non-audio file. Decided by extension, so
    archive.org's "Flac FingerPrint" (`.ffp`) stays a non-audio file."""
    ext = _ext(f.name)
    if ext in _LOSSLESS:
        return _Class.LOSSLESS
    if ext in _LOSSY:
        return _Class.LOSSY
    if ext in _EITHER:
        return _Class.LOSSLESS if "lossless" in (f.format or "").lower() else _Class.LOSSY
    if ext in _OTHER_AUDIO:
        return _Class.UNRECOGNISED
    return None


@dataclass(frozen=True)
class _Format:
    """One audio format of a show: the files sharing the source's format name."""
    key: str
    cls: _Class
    files: tuple[SourceFile, ...]

    @property
    def playable(self) -> bool:
        return self.cls is not _Class.UNRECOGNISED and _ext(self.files[0].name) not in _UNPLAYABLE

    @property
    def served(self) -> bool:
        return not any(f.refused for f in self.files)


def _is_audio_zip(f: SourceFile) -> bool:
    fmt = (f.format or "").lower()
    return fmt.endswith("zip") and any(word in fmt for word in _AUDIO_ZIP_WORDS)


def _formats(audio: Sequence[SourceFile]) -> list[_Format]:
    groups: dict[str, list[SourceFile]] = {}
    for f in audio:
        groups.setdefault(f.format or _ext(f.name), []).append(f)
    out: list[_Format] = []
    for key, members in groups.items():
        classes = {c for c in (_audio_class(f) for f in members) if c is not None}
        out.append(_Format(key, min(classes), tuple(members)))
    return out


def _size_per_track(fmt: _Format) -> float:
    """-1 when the source lists no size for some file (ranks last)."""
    if any(f.size is None for f in fmt.files):
        return -1
    return sum(f.size or 0 for f in fmt.files) / len(fmt.files)


def _ranked(candidates: list[_Format]) -> list[_Format]:
    """Best first: lossless, then lossy, then unrecognised.

    Lossless: a playable copy above SHN, then FLAC, Apple Lossless, WAV, AIFF, then
    larger size per track. Lossy: mean per-file bitrate when every compared copy lists
    one, else size per track.
    """
    lossy = [c for c in candidates if c.cls is _Class.LOSSY]
    by_bitrate = bool(lossy) and all(f.bitrate for c in lossy for f in c.files)

    def key(c: _Format) -> tuple[int, int, int, float, str]:
        order, measure = 0, 0.0
        if c.cls is _Class.LOSSLESS:
            order = _LOSSLESS_ORDER.get(_ext(c.files[0].name), len(_LOSSLESS_ORDER))
            measure = _size_per_track(c)
        elif c.cls is _Class.LOSSY:
            measure = (
                sum(f.bitrate or 0 for f in c.files) / len(c.files)
                if by_bitrate
                else _size_per_track(c)
            )
        return (-c.cls, 0 if c.playable else 1, order, -measure, c.key)

    return sorted(candidates, key=key)


@dataclass(frozen=True)
class Selection:
    """The files to mirror (each flagged whether the library stages it) and what the
    core records about the choice."""
    manifest: Manifest
    is_restricted: bool = False
    restriction_reason: str | None = None
    not_playable_reason: str | None = None


def _entry(f: SourceFile, library: bool) -> ManifestFile:
    return ManifestFile(name=f.name, size=f.size, md5=f.md5, library=library)


def select(files: Sequence[SourceFile], restriction_reason: str | None) -> Selection:
    """Choose the files to mirror from a source's full listing for one item."""
    audio = [f for f in files if _audio_class(f) is not None]
    if not audio:
        # Nothing to play: an empty selection, which the planner records as listing no
        # files and keeps the archive copy (a darked or glitched listing).
        return Selection(Manifest(()), bool(restriction_reason), restriction_reason)

    formats = _formats(audio)
    # A format counts only when it covers every track and the source serves it all.
    tracks = max(len(fmt.files) for fmt in formats)
    candidates = [fmt for fmt in formats if len(fmt.files) == tracks and fmt.served]
    if not candidates:
        return Selection(Manifest(()), True, restriction_reason or DOWNLOAD_REFUSED_REASON)

    ranked = _ranked(candidates)
    best = ranked[0]
    playable = next((fmt for fmt in ranked if fmt.playable), None)
    staged = playable or best

    entries = [_entry(f, library=True) for f in files if f in staged.files]
    if best is not staged:
        entries += [_entry(f, library=False) for f in best.files]
    entries += [
        _entry(f, library=True)
        for f in files
        if f not in audio and not f.refused and not _is_audio_zip(f)
    ]
    manifest = Manifest(files=tuple(sorted(entries, key=lambda e: e.name)))

    if restriction_reason:
        return Selection(manifest, True, restriction_reason)
    reason = None
    lossless_refused = any(fmt.cls is _Class.LOSSLESS and not fmt.served for fmt in formats)
    if best.cls is not _Class.LOSSLESS and lossless_refused:
        reason = LOSSLESS_REFUSED_REASON
    not_playable = None
    if playable is None:
        not_playable = (
            NOT_PLAYABLE_UNRECOGNISED_REASON
            if best.cls is _Class.UNRECOGNISED
            else NOT_PLAYABLE_SHN_REASON
        )
    return Selection(manifest, False, reason, not_playable)
