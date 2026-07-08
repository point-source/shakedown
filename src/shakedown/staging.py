"""Materialize the library staging tree as hardlinks. PRD §8."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shakedown.config import CollectionConfig, Config
from shakedown.filesystem import FilesystemError, hardlink
from shakedown.models import Item, Manifest
from shakedown.utils.templates import render

log = logging.getLogger(__name__)

PASSTHROUGH = "passthrough"

# Name of the per-item source-metadata sidecar (§spec:metadata-preservation). Written by
# the core into the archive item directory at fetch time and hardlinked into the library
# staging directory beside the media. Deliberately excluded from the recorded manifest, so
# an upstream metadata edit never flips an item to `changed-upstream` (§spec:sync-identity).
METADATA_SIDECAR = "metadata.json"


def write_sidecar(path: Path, metadata: dict[str, Any]) -> None:
    """Serialize an item's raw source metadata to a `metadata.json` sidecar at `path`.

    Written in place (same inode across rewrites) so a hardlinked library copy tracks the
    archive copy without a relink. `ensure_ascii=False` preserves multilingual archival
    metadata verbatim, matching the conservative-sanitization stance of §spec:library-staging.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True))


@dataclass
class StageResult:
    linked: int = 0
    already_present: int = 0
    collisions: list[str] = None  # type: ignore[assignment]
    # The item's resolved staging directory. Callers reuse it (e.g. the collision
    # summary, the handoff payload) instead of re-rendering the layout template.
    staging_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.collisions is None:
            self.collisions = []


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
        return base / rel

    rendered = render(layout, item_metadata)
    return base / rendered.strip("/")


def stage_item(
    config: Config,
    collection: CollectionConfig,
    source_name: str,
    item: Item,
    manifest: Manifest,
    *,
    staged_paths: set[Path] | None = None,
) -> StageResult:
    """Create hardlinks for one item's manifest files into the staging tree.

    `manifest` is the source-of-truth file list (the recorded manifest at fetch time).

    `staged_paths`, if provided, tracks every staging path written this sync. It's how
    we tell the two "dst already exists" cases apart:

    - **Cross-item collision** (PRD §8): another item this run rendered to the same
      staging path. Error — surfaced via `result.collisions`.
    - **Stale link from a prior fetch**: the archive file's inode changed (CHANGED_UPSTREAM
      via the IA plugin's atomic swap) and the existing library hardlink points to the
      *old* inode. Silently replace — this is a normal consequence of re-fetching.
    """
    if item.archive_path is None:
        raise ValueError(f"cannot stage item {item.identifier!r}: no archive_path")

    staging_dir = staging_dir_for(
        config, collection, source_name, item.source_metadata, item.archive_path
    )

    result = StageResult(staging_dir=staging_dir)
    for mf in manifest.files:
        src = item.archive_path / mf.name
        dst = staging_dir / mf.name
        if not src.is_file():
            log.warning(
                "skipping staging for %s/%s: source missing on disk",
                item.identifier, mf.name,
            )
            continue
        _link_file(
            src, dst, result, staged_paths,
            collision_msg=(
                f"{dst}: another item already staged here this run "
                f"(template renders to the same path for multiple items)"
            ),
        )

    # The metadata.json sidecar is staged as an explicit additional link, not a manifest
    # entry (§spec:metadata-preservation). It is regenerated from the DB-recorded metadata
    # when the archive copy is absent, so restage/reconcile stay network-free.
    if collection.preserve_source_metadata:
        sidecar_src = _ensure_archive_sidecar(item)
        if sidecar_src is not None:
            _link_file(
                sidecar_src, staging_dir / METADATA_SIDECAR, result, staged_paths,
                collision_msg=(
                    f"{staging_dir / METADATA_SIDECAR}: metadata sidecar already staged "
                    f"by another item this run"
                ),
            )
    return result


def _link_file(
    src: Path,
    dst: Path,
    result: StageResult,
    staged_paths: set[Path] | None,
    *,
    collision_msg: str,
) -> None:
    """Hardlink one file into the staging tree, resolving the two "dst exists" cases.

    Already-present (same inode) is a no-op; a stale link from a prior fetch (archive
    inode replaced) is unlinked and relinked; a path already claimed by another item this
    run is a cross-item collision reported via ``collision_msg`` (§spec:library-staging,
    §spec:layout-collision-safety).
    """
    already_claimed_this_run = staged_paths is not None and dst in staged_paths
    try:
        if dst.exists():
            if src.stat().st_ino == dst.stat().st_ino:
                result.already_present += 1
                if staged_paths is not None:
                    staged_paths.add(dst)
                return
            if already_claimed_this_run:
                result.collisions.append(collision_msg)
                return
            # Stale link from a prior run; archive inode has been replaced.
            dst.unlink()
        hardlink(src, dst)
        result.linked += 1
        if staged_paths is not None:
            staged_paths.add(dst)
    except FilesystemError as e:
        result.collisions.append(f"{dst}: {e}")


def _ensure_archive_sidecar(item: Item) -> Path | None:
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


def unstage_item(
    config: Config,
    collection: CollectionConfig,
    source_name: str,
    item: Item,
) -> None:
    """Remove an item's staging hardlinks and any directories left empty.

    Used when pruning a disappeared item (§spec:item-lifecycle): the archive
    files and their library links are removed while the DB record is retained.
    Only the item's own manifest links are unlinked; shared parent directories
    are removed solely when they become empty, so a sibling item's staging tree
    is never touched. Missing links are ignored — unstaging is idempotent.
    """
    if item.archive_path is None or item.recorded_manifest is None:
        return

    staging_dir = staging_dir_for(
        config, collection, source_name, item.source_metadata, item.archive_path
    )
    for mf in item.recorded_manifest.files:
        (staging_dir / mf.name).unlink(missing_ok=True)
    # Drop the metadata.json sidecar link too, so the staging dir can go empty. Done
    # unconditionally (missing_ok) — idempotent, and it clears a stale link even if the
    # collection later opted out of preservation (§spec:metadata-preservation).
    (staging_dir / METADATA_SIDECAR).unlink(missing_ok=True)

    # Prune now-empty directories from the item's staging dir up to the library
    # root; rmdir removes a directory only when empty, so siblings are safe.
    library_root = config.library_root
    current = staging_dir
    while current != library_root and current.is_relative_to(library_root):
        try:
            current.rmdir()
        except OSError:
            break  # not empty (or gone) — stop climbing
        current = current.parent
