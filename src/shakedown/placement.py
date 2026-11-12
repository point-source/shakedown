"""Library placement: who owns each library path, and keeping the library a faithful,
deterministic view of the archive (§spec:library-ownership, §spec:library-cleanup).

One engine per (source, collection) and staging operation. The library it produces is
a function of the shows with an archive copy, their recorded metadata, and the layout:

- Each show renders a *bare* folder from `library_layout`. When two or more shows
  render the same bare folder, every one of them gets ` [<identifier>]` appended to
  the folder's last segment; a show that clashes with no other keeps the bare name.
- Every file shakedown places is recorded (`PlacementRepo`). The record alone decides
  what shakedown may replace or remove: a library file it did not place is never
  replaced or removed, only reported.
- A pass first removes every recorded file that is no longer part of its show's
  placement (and folders that removal leaves empty), then links what is missing, so
  folder moves and swaps never depend on the order shows are handled in.
- The first pass in a collection with no record adopts the library links already
  there: a file that is the same inode as a file in a show's archive copy becomes
  that show's placement (upgrade from a version that kept no record, or a database
  rebuilt from scratch).

Callers: `begin()` before a sync's fetches, `update()` after each fetched show lands
(so a long first sync fills the library as it goes, §spec:discovery-pipeline), and
`finish()` once at the end of every staging operation for the full pass.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from shakedown.config import CollectionConfig, Config
from shakedown.db import transaction
from shakedown.filesystem import FilesystemError, hardlink, is_within
from shakedown.models import Item
from shakedown.staging import (
    METADATA_SIDECAR,
    UnsafeLibraryPathError,
    ensure_archive_sidecar,
    manifest_reserves_sidecar_name,
    staging_dir_for,
)
from shakedown.state import ItemRepo, PlacementRepo
from shakedown.utils.sanitize import path_safe
from shakedown.utils.templates import TemplateError

if TYPE_CHECKING:
    from shakedown.activity import Progress

log = logging.getLogger(__name__)


def foreign_file_issue(identifier: str, path: Path) -> str:
    """Per-show issue: a link target already holds a file shakedown did not place."""
    return (
        f"{identifier}: {path} already holds a file shakedown did not place; "
        "left it and skipped that link"
    )


def removal_issue(identifier: str, path: Path, error: OSError) -> str:
    """Per-show issue: a file shakedown placed could not be removed (retried next time)."""
    return f"{identifier}: could not remove {path} ({error}); will retry on the next run"


def log_separations(source_name: str, collection_name: str, count: int) -> None:
    """The run-summary line for separations in effect; not a failure."""
    if count:
        log.info(
            "%s/%s: %d shows render the same library folder; each was staged into its own "
            "folder labelled with its identifier",
            source_name, collection_name, count,
        )


def has_archive_copy(item: Item) -> bool:
    """A show stages when its archive copy is on disk, whatever its status: a show
    marked disappeared or unavailable keeps its files, so it keeps its folder."""
    return (
        item.recorded_manifest is not None
        and item.archive_path is not None
        and item.archive_path.is_dir()
    )


@dataclass
class PlacementReport:
    """What one full pass left in effect."""

    # Shows staged into a folder labelled with their identifier (separations in effect).
    separations: int = 0
    # Per-show issues: foreign-file conflicts, failed removals, refused paths, disk errors.
    issues: dict[str, list[str]] = field(default_factory=dict)
    # Absolute library folder of every show staged by the pass.
    folder_of: dict[str, Path] = field(default_factory=dict)
    links_created: int = 0
    files_removed: int = 0
    # (identifier, message) when the layout could not be rendered; nothing was removed.
    render_error: tuple[str, str] | None = None


class LibraryPlacement:
    def __init__(
        self,
        config: Config,
        collection: CollectionConfig,
        source_name: str,
        items: ItemRepo,
        *,
        progress: Progress | None = None,
    ) -> None:
        self.config = config
        # A restage reports each show its full pass stages (§spec:run-activity).
        self._progress = progress
        self.collection = collection
        self.source = source_name
        self.items = items
        self.repo = PlacementRepo(items.conn)
        self.base = config.library_root / source_name / collection.name
        # identifier -> bare folder (relative, posix); bare folder -> identifiers.
        self._bare: dict[str, str] = {}
        self._groups: dict[str, set[str]] = {}
        self._refused: dict[str, str] = {}
        self._render_error: tuple[str, str] | None = None
        self._report = PlacementReport()

    # --- public API ----------------------------------------------------------------

    def begin(self) -> None:
        """Adopt pre-existing links if needed and index every show's folder, so shows
        fetched during the run can be staged at once with `update()`."""
        self._ensure_adopted()
        self._build_index()

    def update(self, identifier: str) -> None:
        """Stage one show whose archive copy just landed, moving any show whose
        separation it starts or ends. Issues found here are reported by `finish()`,
        whose full pass meets them again."""
        if self._render_error is not None:
            return
        old = self._bare.get(identifier)
        self._unindex(identifier)
        item = self.items.get(self.source, self.collection.name, identifier)
        if item is None or not has_archive_copy(item):
            return
        try:
            self._index(item)
        except TemplateError as e:
            self._refused[identifier] = f"{identifier}: {e}"
            return
        if identifier in self._refused:
            return  # an unsafe path: refused, and reported by the full pass
        affected = {identifier}
        for bare in (old, self._bare.get(identifier)):
            if bare is not None:
                affected |= self._groups.get(bare, set())
        self._report = PlacementReport()
        self._apply(sorted(affected), full=False)

    def finish(self) -> PlacementReport:
        """Full pass over the collection: every show at its current folder, every file
        no longer placed removed, the list of files shakedown did not place refreshed."""
        self._ensure_adopted()
        self._build_index()
        self._report = PlacementReport()
        if self._render_error is not None:
            self._report.render_error = self._render_error
            return self._report
        if self._progress is not None:
            self._progress.work_total(len(self._bare))
        self._apply(sorted(self._bare), full=True)
        self._refresh_unplaced()
        report = self._report
        for identifier, message in self._refused.items():
            report.issues.setdefault(identifier, []).append(message)
        for identifier in self._bare:
            report.folder_of[identifier] = self.base / self._folder(identifier)
        report.separations = sum(1 for identifier in self._bare if self._separated(identifier))
        return report

    # --- folder index --------------------------------------------------------------

    def _build_index(self) -> None:
        self._bare.clear()
        self._groups.clear()
        self._refused.clear()
        self._render_error = None
        for item in self.items.list_for_collection(self.source, self.collection.name):
            if not has_archive_copy(item):
                continue
            try:
                self._index(item)
            except TemplateError as e:
                # Render every folder before removing anything: a layout that cannot
                # render leaves the existing tree in place (§spec:library-staging).
                self._render_error = (item.identifier, str(e))
                return

    def _index(self, item: Item) -> None:
        assert item.archive_path is not None
        try:
            folder = staging_dir_for(
                self.config, self.collection, self.source, item.source_metadata,
                item.archive_path,
            )
            self._desired_names(item)  # refuse a show whose file names leave its folder
        except UnsafeLibraryPathError as e:
            self._refused[item.identifier] = f"{item.identifier}: {e}"
            return
        bare = PurePosixPath(folder.relative_to(self.base)).as_posix()
        self._bare[item.identifier] = bare
        self._groups.setdefault(bare, set()).add(item.identifier)

    def _unindex(self, identifier: str) -> None:
        self._refused.pop(identifier, None)
        bare = self._bare.pop(identifier, None)
        if bare is not None:
            group = self._groups[bare]
            group.discard(identifier)
            if not group:
                del self._groups[bare]

    def _separated(self, identifier: str) -> bool:
        return len(self._groups[self._bare[identifier]]) > 1

    def _folder(self, identifier: str) -> str:
        bare = self._bare[identifier]
        if not self._separated(identifier):
            return bare
        path = PurePosixPath(bare)
        return (path.parent / f"{path.name} [{path_safe(identifier)}]").as_posix()

    # --- desired placement ---------------------------------------------------------

    def _desired_names(self, item: Item) -> list[str]:
        """The file names the show places in its folder: every recorded file the
        library stages (whether or not its archive copy is on disk — a missing archive
        file must not take the library copy with it), plus the metadata sidecar when
        preserved. An archive-only copy, such as SHN beside a playable copy, is not
        placed (§spec:quality-selection)."""
        assert item.recorded_manifest is not None
        names = [mf.name for mf in item.recorded_manifest.files if mf.library]
        probe = self.base / "probe"
        for name in names:
            if not is_within(probe, probe / name):
                raise UnsafeLibraryPathError(
                    f"refusing file name {name!r}: it would land outside its library folder"
                )
        if self.collection.preserve_source_metadata and not manifest_reserves_sidecar_name(
            item.recorded_manifest
        ):
            names.append(METADATA_SIDECAR)
        return names

    def _desired(self, item: Item) -> dict[str, str]:
        """Relative library path -> file name in the archive copy."""
        folder = PurePosixPath(self._folder(item.identifier))
        return {(folder / name).as_posix(): name for name in self._desired_names(item)}

    # --- the pass ------------------------------------------------------------------

    def _apply(self, identifiers: list[str], *, full: bool) -> None:
        """Remove what no longer belongs, then link what is missing, for `identifiers`
        (and, in a full pass, for every show that no longer stages). Writes only what
        changed: a pass over an up-to-date library reads and stats, nothing more."""
        touched: set[Path] = set()
        folders = self.repo.folders(self.source, self.collection.name)
        for identifier in identifiers:
            item = self.items.get(self.source, self.collection.name, identifier)
            if item is None:
                continue
            desired = self._desired(item)
            placed = self.repo.placed_inodes(self.source, self.collection.name, identifier)
            stale = sorted(rel for rel in placed if rel not in desired)
            if stale:
                moves = self._last_copy_moves(item, desired, stale, folders.get(identifier))
                with transaction(self.items.conn):
                    for rel in stale:
                        if rel in moves:
                            self._move(identifier, rel, moves[rel], placed[rel], touched)
                        else:
                            self._remove(identifier, rel, placed[rel], touched)
        if full:
            for identifier in self._orphans(set(identifiers)):
                placed = self.repo.placed_inodes(self.source, self.collection.name, identifier)
                with transaction(self.items.conn):
                    for rel in sorted(placed):
                        self._remove(identifier, rel, placed[rel], touched)
                    self.repo.delete_folder(self.source, self.collection.name, identifier)
        self._prune_dirs(touched)

        folders = self.repo.folders(self.source, self.collection.name)
        progress = self._progress if full else None
        for identifier in identifiers:
            item = self.items.get(self.source, self.collection.name, identifier)
            if item is None:
                continue
            if progress is None:
                self._link_show(item, folders.get(identifier))
                continue
            progress.working(identifier)
            try:
                self._link_show(item, folders.get(identifier))
            finally:
                progress.idle(identifier)
            progress.done()

    def _last_copy_moves(
        self, item: Item, desired: dict[str, str], stale: list[str], old_folder: str | None
    ) -> dict[str, str]:
        """Old library path -> new path for files whose archive copy is gone: when the
        show's folder changes, the library link is the last copy of those bytes, so it
        moves with the show instead of being removed."""
        assert item.archive_path is not None
        missing = {
            name: rel
            for rel, name in desired.items()
            if name != METADATA_SIDECAR and not (item.archive_path / name).is_file()
        }
        if not missing or old_folder is None:
            return {}
        moves: dict[str, str] = {}
        for rel in stale:
            try:
                name = PurePosixPath(rel).relative_to(old_folder).as_posix()
            except ValueError:
                continue  # not in the show's recorded folder
            if name in missing:
                moves[rel] = missing.pop(name)
        return moves

    def _move(self, identifier: str, rel: str, new_rel: str, inode: int,
              touched: set[Path]) -> None:
        """Link a placed file at its new path, then remove the old path. If the new path
        is taken, the old file stays where it is (and stays recorded)."""
        path = self.base / rel
        try:
            if path.lstat().st_ino != inode:
                self._let_go(rel)
                return
            hardlink(path, self.base / new_rel)
        except FileNotFoundError:
            self.repo.remove_placement(self.source, self.collection.name, rel)
            return
        except FilesystemError:
            return  # the new path holds another file; the link phase reports it
        except OSError as e:
            self._issue(identifier, removal_issue(identifier, path, e))
            return
        self.repo.add_placement(self.source, self.collection.name, identifier, new_rel, inode)
        self._remove(identifier, rel, inode, touched)

    def _orphans(self, staged: set[str]) -> list[str]:
        """Shows with a placement or folder record that no longer stage (pruned,
        forgotten, archive copy gone). A refused show keeps its placement."""
        recorded = self.repo.placed_identifiers(self.source, self.collection.name)
        recorded |= set(self.repo.folders(self.source, self.collection.name))
        return sorted(recorded - staged - set(self._refused))

    def _remove(self, identifier: str, rel: str, inode: int, touched: set[Path]) -> None:
        """Remove one placed file. A different file now at that path is not
        shakedown's: it stays, is reported, and the record lets go of the path."""
        path = self.base / rel
        try:
            if path.lstat().st_ino != inode:
                self._let_go(rel)
                return
            path.unlink()
        except FileNotFoundError:
            pass  # already gone: only the record is left to drop
        except OSError as e:
            self._issue(identifier, removal_issue(identifier, path, e))
            return  # stays recorded; retried by the next staging operation
        else:
            self._report.files_removed += 1
            touched.add(path.parent)
        self.repo.remove_placement(self.source, self.collection.name, rel)

    def _let_go(self, rel: str) -> None:
        """Stop owning a recorded path that now holds someone else's file; report it."""
        self.repo.remove_placement(self.source, self.collection.name, rel)
        self.repo.add_unplaced(self.source, self.collection.name, rel)

    def _prune_dirs(self, touched: set[Path]) -> None:
        """Remove folders left empty, up to the collection's library folder. A folder
        that held removed files and still has content is checked for files shakedown
        did not place, which are reported, never removed."""
        for folder in sorted(touched, key=lambda p: len(p.parts), reverse=True):
            current = folder
            while current != self.base and current.is_relative_to(self.base):
                try:
                    current.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    if current == folder:
                        self._note_unplaced_in(folder)
                    break
                current = current.parent

    def _note_unplaced_in(self, folder: Path) -> None:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                continue
            rel = PurePosixPath(Path(entry.path).relative_to(self.base)).as_posix()
            if self.repo.owner_of(self.source, self.collection.name, rel) is None:
                self.repo.add_unplaced(self.source, self.collection.name, rel)

    def _link_show(self, item: Item, recorded_folder: str | None) -> None:
        assert item.archive_path is not None
        identifier = item.identifier
        desired = self._desired(item)
        placed = self.repo.placed_inodes(self.source, self.collection.name, identifier)
        sidecar = None
        if METADATA_SIDECAR in desired.values():
            sidecar = ensure_archive_sidecar(item)
        with transaction(self.items.conn):
            for rel, name in desired.items():
                src = sidecar if name == METADATA_SIDECAR and sidecar else item.archive_path / name
                if not src.is_file():
                    log.warning(
                        "skipping staging for %s/%s: source missing on disk", identifier, name
                    )
                    continue
                try:
                    self._link_file(identifier, src, rel, placed.get(rel))
                except (OSError, FilesystemError) as e:
                    self._issue(identifier, f"{identifier}: linking into the library failed: {e}")
                    break
            folder = self._folder(identifier)
            if folder != recorded_folder:
                self.repo.set_folder(
                    self.source, self.collection.name, identifier, folder,
                    separated=self._separated(identifier),
                )

    def _link_file(self, identifier: str, src: Path, rel: str, placed: int | None) -> None:
        """Link one archive file at `rel`. `placed` is the inode this show recorded
        there, if any."""
        dst = self.base / rel
        src_ino = src.stat().st_ino
        try:
            dst_ino: int | None = dst.lstat().st_ino
        except FileNotFoundError:
            dst_ino = None
        if dst_ino == src_ino:
            if placed != src_ino:  # adopt an unrecorded link to the archive copy
                self.repo.add_placement(
                    self.source, self.collection.name, identifier, rel, src_ino
                )
            return
        if dst_ino is not None and dst_ino != placed:
            # Never replace a file shakedown did not place (§spec:library-ownership).
            self._issue(identifier, foreign_file_issue(identifier, dst))
            owner = self.repo.owner_of(self.source, self.collection.name, rel)
            if owner is None or owner == identifier:
                self._let_go(rel)
            return
        if dst_ino is not None:
            dst.unlink()  # our stale link: the archive copy was replaced by a re-fetch
        hardlink(src, dst)
        self.repo.add_placement(self.source, self.collection.name, identifier, rel, src_ino)
        self._report.links_created += 1

    def _refresh_unplaced(self) -> None:
        """Drop reported files that are gone or that shakedown now owns."""
        with transaction(self.items.conn):
            for rel in self.repo.list_unplaced(self.source, self.collection.name):
                path = self.base / rel
                gone = not (path.exists() or path.is_symlink())
                if gone or self.repo.owner_of(self.source, self.collection.name, rel):
                    self.repo.remove_unplaced(self.source, self.collection.name, rel)

    def _issue(self, identifier: str, message: str) -> None:
        log.warning("[%s/%s] %s", self.source, self.collection.name, message)
        self._report.issues.setdefault(identifier, []).append(message)

    # --- adoption ------------------------------------------------------------------

    def _ensure_adopted(self) -> None:
        """Once per collection: record existing library links as placements of the show
        whose archive file they are the same inode as (§spec:library-cleanup). Files
        that match no archive file are reported as not placed by shakedown."""
        if self.repo.is_adopted(self.source, self.collection.name):
            return
        linked: dict[tuple[int, int], list[str]] = {}  # (st_dev, st_ino) -> library paths
        unplaced: list[str] = []
        for path in self._library_files():
            rel = PurePosixPath(path.relative_to(self.base)).as_posix()
            try:
                st = path.lstat()
            except OSError:
                continue
            if st.st_nlink > 1 and path.is_file() and not path.is_symlink():
                linked.setdefault((st.st_dev, st.st_ino), []).append(rel)
            else:
                unplaced.append(rel)
        adopted: list[tuple[str, str, int]] = []
        if linked:
            for identifier, src in self._archive_files():
                try:
                    st = src.stat()
                except OSError:
                    continue
                for rel in linked.pop((st.st_dev, st.st_ino), []):
                    adopted.append((identifier, rel, st.st_ino))
        unplaced.extend(rel for rels in linked.values() for rel in rels)
        with transaction(self.items.conn):
            for identifier, rel, inode in adopted:
                if self.repo.owner_of(self.source, self.collection.name, rel) is None:
                    self.repo.add_placement(
                        self.source, self.collection.name, identifier, rel, inode
                    )
            for rel in unplaced:
                if self.repo.owner_of(self.source, self.collection.name, rel) is None:
                    self.repo.add_unplaced(self.source, self.collection.name, rel)
            self.repo.mark_adopted(self.source, self.collection.name, datetime.now())
        if adopted or unplaced:
            log.info(
                "[%s/%s] adopted %d existing library file(s); %d not placed by shakedown",
                self.source, self.collection.name, len(adopted), len(unplaced),
            )

    def _library_files(self) -> Iterator[Path]:
        if not self.base.is_dir():
            return
        for root, _dirs, files in os.walk(self.base):
            for name in files:
                yield Path(root) / name

    def _archive_files(self) -> Iterable[tuple[str, Path]]:
        for item in self.items.list_for_planning(self.source, self.collection.name):
            if not has_archive_copy(item):
                continue
            assert item.archive_path is not None and item.recorded_manifest is not None
            for mf in item.recorded_manifest.files:
                yield item.identifier, item.archive_path / mf.name
            yield item.identifier, item.archive_path / METADATA_SIDECAR

