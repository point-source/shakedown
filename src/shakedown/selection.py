"""Core-owned file selection: which of an item's listed files to mirror.

One rule for every source (SPEC §spec:per-item-isolation). This is the interim
rule — the collection's `format_filters`, `exclude_filters`, and opt-in
`fallback_format_filters` — until the quality rule replaces it
(§spec:quality-selection).
"""
from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from dataclasses import dataclass

from shakedown.config import CollectionConfig
from shakedown.models import Manifest, ManifestFile, SourceFile

# Recorded as the item's restriction_reason when archive.org refuses a download.
DOWNLOAD_REFUSED_REASON = "download refused by archive.org (stream-only or restricted)"
# Recorded on a show mirrored from the collection's fallback formats instead.
FALLBACK_REASON = "lossless refused by archive.org; MP3 copy mirrored instead"


@dataclass(frozen=True)
class Selection:
    manifest: Manifest
    is_restricted: bool = False
    restriction_reason: str | None = None


def _matches(f: SourceFile, formats: Sequence[str], excludes: Sequence[str]) -> bool:
    """Excludes (fnmatch globs on the name) first, then the format/extension keep-list."""
    if any(fnmatch.fnmatch(f.name, pat) for pat in excludes):
        return False
    if not formats:
        return True
    fmt = (f.format or "").lower()
    ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
    return any(want.lower() in (fmt, ext) for want in formats)


def _manifest(files: Sequence[SourceFile], formats: Sequence[str], excludes: Sequence[str]) -> Manifest:
    """The served files the filters want — what 'do I have it?' compares."""
    entries = sorted(
        (
            ManifestFile(name=f.name, size=f.size, md5=f.md5)
            for f in files
            if not f.refused and _matches(f, formats, excludes)
        ),
        key=lambda e: e.name,
    )
    return Manifest(files=tuple(entries))


def select(
    files: Sequence[SourceFile],
    restriction_reason: str | None,
    collection: CollectionConfig,
) -> Selection:
    """Choose the files to mirror from a source's full listing for one item."""
    manifest = _manifest(files, collection.format_filters, collection.exclude_filters)
    if restriction_reason:
        return Selection(manifest, True, restriction_reason)
    if manifest.files or not any(
        f.refused and _matches(f, collection.format_filters, collection.exclude_filters)
        for f in files
    ):
        return Selection(manifest)
    # Every wanted file is one the source refuses: mirror the collection's fallback
    # formats if it opted in and they are served.
    if collection.fallback_format_filters:
        fallback = _manifest(files, collection.fallback_format_filters, collection.exclude_filters)
        if fallback.files:
            return Selection(fallback, False, FALLBACK_REASON)
    return Selection(manifest, True, DOWNLOAD_REFUSED_REASON)
