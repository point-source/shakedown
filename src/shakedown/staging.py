"""Materialize the library staging tree as hardlinks. PRD §8."""
from __future__ import annotations

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


@dataclass
class StageResult:
    linked: int = 0
    already_present: int = 0
    collisions: list[str] = None  # type: ignore[assignment]

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

    result = StageResult()
    for mf in manifest.files:
        src = item.archive_path / mf.name
        dst = staging_dir / mf.name
        if not src.is_file():
            log.warning(
                "skipping staging for %s/%s: source missing on disk",
                item.identifier, mf.name,
            )
            continue

        already_claimed_this_run = staged_paths is not None and dst in staged_paths
        try:
            if dst.exists():
                if src.stat().st_ino == dst.stat().st_ino:
                    result.already_present += 1
                    if staged_paths is not None:
                        staged_paths.add(dst)
                    continue
                if already_claimed_this_run:
                    result.collisions.append(
                        f"{dst}: another item already staged here this run "
                        f"(template renders to the same path for multiple items)"
                    )
                    continue
                # Stale link from a prior run; archive inode has been replaced.
                dst.unlink()
            hardlink(src, dst)
            result.linked += 1
            if staged_paths is not None:
                staged_paths.add(dst)
        except FilesystemError as e:
            result.collisions.append(f"{dst}: {e}")
    return result
