"""Library folder rendering and metadata sidecar helpers. PRD §8.

What gets linked where, and what gets removed, is `shakedown.placement`'s job
(§spec:library-ownership, §spec:library-cleanup)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from shakedown.config import CollectionConfig, Config
from shakedown.filesystem import is_within
from shakedown.models import Item, Manifest
from shakedown.utils.templates import render

log = logging.getLogger(__name__)

PASSTHROUGH = "passthrough"

# Name of the per-item source-metadata sidecar (§spec:metadata-preservation). Written by
# the core into the archive item directory at fetch time and hardlinked into the library
# staging directory beside the media. Deliberately excluded from the recorded manifest, so
# an upstream metadata edit never flips an item to `changed-upstream` (§spec:sync-identity).
METADATA_SIDECAR = "metadata.json"


def manifest_reserves_sidecar_name(manifest: Manifest) -> bool:
    """True if a media file in the manifest legitimately claims the sidecar's reserved
    name. When it does, the sidecar must NOT be written: overwriting the media would make
    the archive silently disagree with its recorded manifest (§spec:sync-identity). Media
    integrity wins over preserved context — the pathological name loses the sidecar, not
    its bytes.
    """
    return any(mf.name == METADATA_SIDECAR for mf in manifest.files)


def write_sidecar(path: Path, metadata: dict[str, Any]) -> None:
    """Serialize an item's raw source metadata to a `metadata.json` sidecar at `path`.

    Written in place (same inode across rewrites) so a hardlinked library copy tracks the
    archive copy without a relink. `ensure_ascii=False` preserves multilingual archival
    metadata verbatim, matching the conservative-sanitization stance of §spec:library-staging.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True))


class UnsafeLibraryPathError(ValueError):
    """A library path would land outside the collection's library folder.

    Layout fields and manifest file names come from the source, so a crafted value
    must never place, replace, or remove a file outside that folder."""


def staging_dir_for(
    config: Config,
    collection: CollectionConfig,
    source_name: str,
    item_metadata: dict[str, Any],
    archive_path: Path,
) -> Path:
    """Resolve the per-item staging directory.

    For passthrough layout, mirror the archive layout exactly.
    For a custom template, render fields from item metadata; the template renders
    the *directory* path, not the filenames (PRD §8: filenames inside the deepest
    directory match the archive).
    """
    base = config.library_root / source_name / collection.name
    layout = collection.library_layout
    if layout == PASSTHROUGH:
        # Mirror /data/archive/<source>/<collection>/<identifier>/ structure 1:1.
        rel = archive_path.relative_to(config.archive_root / source_name / collection.name)
        staging_dir = base / rel
    else:
        staging_dir = base / render(layout, item_metadata).strip("/")
    if not is_within(base, staging_dir):
        raise UnsafeLibraryPathError(
            f"refusing library folder {str(staging_dir)!r}: it is outside {str(base)!r}"
        )
    return staging_dir


def ensure_archive_sidecar(item: Item) -> Path | None:
    """Return the item's archive-side metadata.json, regenerating it from recorded state
    when the on-disk copy is absent (§spec:metadata-preservation).

    Keeps the archive self-describing and lets restage/reconcile reproduce the sidecar
    without the network — the metadata dict is already in the state DB. Returns None when
    the archive directory is gone (a pruned item has nothing to link).
    """
    if item.archive_path is None or not item.archive_path.is_dir():
        return None
    src = item.archive_path / METADATA_SIDECAR
    if not src.is_file():
        write_sidecar(src, item.source_metadata)
    return src
