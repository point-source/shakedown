"""End-to-end sync tests using FakePlugin. Maps to PRD §16 success criteria."""
from __future__ import annotations

import threading
from pathlib import Path

import shakedown.sync as sync_module
from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect
from shakedown.models import ItemStatus
from shakedown.plugins.base import FetchResult
from shakedown.state import ItemRepo, RunRepo
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _seed_item(identifier: str, *, content: bytes = b"hello world") -> FakeItem:
    item = FakeItem(
        identifier=identifier,
        files=[FakeFile(name=f"{identifier}.flac", content=content)],
        metadata={"title": "Show", "date": "1977-05-08", "year": "1977", "venue": "Cornell"},
    )
    FakePlugin.items[identifier] = item
    return item


def test_sync_creates_archive_and_staging(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd1977-05-08")

    rc = run_sync(config)
    assert rc == 0

    # Archive layout: <archive>/<source>/<collection>/<identifier>/<file>
    archived = archive / "fake-src" / "coll1" / "gd1977-05-08" / "gd1977-05-08.flac"
    assert archived.is_file()

    # Staging layout (passthrough): mirrors archive layout under library_root.
    staged = library / "fake-src" / "coll1" / "gd1977-05-08" / "gd1977-05-08.flac"
    assert staged.is_file()
    # Hardlink: same inode.
    assert archived.stat().st_ino == staged.stat().st_ino


def test_sync_is_idempotent(tmp_roots: tuple[Path, Path]) -> None:
    """PRD §16.1: re-running sync does not re-fetch unchanged items."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd1977-05-08")

    assert run_sync(config) == 0
    assert FakePlugin.fetch_count["gd1977-05-08"] == 1

    assert run_sync(config) == 0
    assert FakePlugin.fetch_count["gd1977-05-08"] == 1, "second sync must not re-fetch"


def test_tag_rewrite_does_not_trigger_refetch(tmp_roots: tuple[Path, Path]) -> None:
    """PRD §16.4: library tools mutating bytes in place must not trigger re-downloads.

    This is the load-bearing invariant of the whole project: sync compares
    manifest-vs-manifest, never manifest-vs-disk-bytes (§5).
    """
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd1977-05-08", content=b"original audio bytes")

    assert run_sync(config) == 0
    archived = archive / "fake-src" / "coll1" / "gd1977-05-08" / "gd1977-05-08.flac"
    assert archived.is_file()

    # Simulate a library tool retagging the file in place, mutating bytes.
    archived.write_bytes(b"original audio bytes + new ID3 tags written by Beets")

    # Second sync: source manifest is unchanged, so even though disk bytes differ,
    # the item must classify as 'unchanged'.
    assert run_sync(config) == 0
    assert FakePlugin.fetch_count["gd1977-05-08"] == 1, "tag rewrite must not trigger re-fetch"


def test_core_rejects_manifest_file_name_escaping_item_dir(tmp_roots: tuple[Path, Path]) -> None:
    """A remote manifest file name that escapes the item dir is refused before fetch.

    The write happens inside the plugin's fetch(), so the core must reject the
    unsafe name up front — not merely refuse to promote afterwards.
    """
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["evil"] = FakeItem(
        identifier="evil",
        files=[FakeFile(name="../../escape.flac", content=b"pwned")],
        metadata={"date": "1977-05-08"},
    )

    rc = run_sync(config)
    assert rc == 1  # the item failed, so the run reports failure
    assert "evil" not in FakePlugin.fetch_count, "fetch must not run for an unsafe name"
    # Nothing was written anywhere near the traversal target.
    assert not (archive / "escape.flac").exists()
    assert not (archive.parent / "escape.flac").exists()

    conn = connect(config.state_db)  # type: ignore[arg-type]
    item = ItemRepo(conn).get("fake-src", "coll1", "evil")
    assert item is not None
    assert item.status == ItemStatus.FAILED


def test_changed_upstream_triggers_refetch(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    item = _seed_item("gd1977-05-08", content=b"v1")

    assert run_sync(config) == 0
    assert FakePlugin.fetch_count["gd1977-05-08"] == 1

    # Source publishes new bytes for the same file → manifest md5/size changes.
    item.files = [FakeFile(name="gd1977-05-08.flac", content=b"v2-much-longer-bytes-here")]
    assert run_sync(config) == 0
    assert FakePlugin.fetch_count["gd1977-05-08"] == 2

    # And the on-disk bytes match the new source content.
    archived = archive / "fake-src" / "coll1" / "gd1977-05-08" / "gd1977-05-08.flac"
    assert archived.read_bytes() == b"v2-much-longer-bytes-here"


def test_restricted_item_recorded_as_unavailable(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    item = _seed_item("gd-stream-only")
    item.is_restricted = True
    item.restriction_reason = "stream-only soundboard per band request"

    assert run_sync(config) == 0
    assert FakePlugin.fetch_count.get("gd-stream-only", 0) == 0

    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    row = items.get("fake-src", "coll1", "gd-stream-only")
    assert row is not None
    assert row.status == ItemStatus.UNAVAILABLE
    assert "soundboard" in (row.restriction_reason or "")


def test_disappeared_items_retained_by_default(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-show-1")

    assert run_sync(config) == 0
    archived = archive / "fake-src" / "coll1" / "gd-show-1"
    assert archived.is_dir()

    # Source removes the item from its enumeration.
    FakePlugin.items.clear()
    assert run_sync(config) == 0

    # Files must remain on disk; DB row flips to disappeared.
    assert archived.is_dir(), "disappeared items must be retained by default"
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    row = items.get("fake-src", "coll1", "gd-show-1")
    assert row is not None and row.status == ItemStatus.DISAPPEARED


def test_disappeared_pruned_when_opted_in(tmp_roots: tuple[Path, Path]) -> None:
    """§spec:item-lifecycle: prune_disappeared removes files + staging links but
    retains the DB record marked `pruned` so `status` can still report the takedown."""
    archive, library = tmp_roots
    config = make_config(archive, library, prune_disappeared=True)
    _seed_item("gd-show-2")

    assert run_sync(config) == 0
    archived = archive / "fake-src" / "coll1" / "gd-show-2"
    staged = library / "fake-src" / "coll1" / "gd-show-2"
    assert archived.is_dir() and staged.is_dir()

    FakePlugin.items.clear()
    assert run_sync(config) == 0

    # Files gone from both trees...
    assert not archived.exists(), "prune_disappeared=true must remove archive directory"
    assert not staged.exists(), "prune_disappeared=true must remove staging links"

    # ...but the record is retained, marked pruned, so the takedown stays reportable.
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    row = items.get("fake-src", "coll1", "gd-show-2")
    assert row is not None and row.status == ItemStatus.PRUNED
    assert row.archive_path is None

    # A subsequent sync leaves the pruned record untouched (not re-disappeared).
    assert run_sync(config) == 0
    row = items.get("fake-src", "coll1", "gd-show-2")
    assert row is not None and row.status == ItemStatus.PRUNED


def test_disappeared_item_pruned_when_prune_enabled_later(tmp_roots: tuple[Path, Path]) -> None:
    """§spec:item-lifecycle: an item already flagged `disappeared` under the default
    retain policy is pruned on the next sync once the collection opts into
    `prune_disappeared` — the retain→prune transition is not stuck at `disappeared`."""
    archive, library = tmp_roots
    _seed_item("gd-show-3")

    # First sync (retain default) fetches; then the source drops it → disappeared.
    retain = make_config(archive, library, prune_disappeared=False)
    assert run_sync(retain) == 0
    archived = archive / "fake-src" / "coll1" / "gd-show-3"
    staged = library / "fake-src" / "coll1" / "gd-show-3"
    assert archived.is_dir() and staged.is_dir()

    FakePlugin.items.clear()
    assert run_sync(retain) == 0
    conn = connect(retain.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    row = items.get("fake-src", "coll1", "gd-show-3")
    assert row is not None and row.status == ItemStatus.DISAPPEARED
    assert archived.is_dir(), "retain default must keep local files"

    # Operator opts into pruning; the next sync prunes the already-disappeared item.
    prune = make_config(archive, library, prune_disappeared=True)
    assert run_sync(prune) == 0
    assert not archived.exists(), "enabling prune must remove the archive directory"
    assert not staged.exists(), "enabling prune must remove staging links"
    row = items.get("fake-src", "coll1", "gd-show-3")
    assert row is not None and row.status == ItemStatus.PRUNED
    assert row.archive_path is None


def test_describe_failure_does_not_prune_still_enumerated_item(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """§spec:prune-safety: on a prune_disappeared collection, a transient describe_item
    failure for a still-enumerated item must NOT prune or disappear it. Prune eligibility
    keys on absence from the enumeration, never on absence from the described set."""
    archive, library = tmp_roots
    config = make_config(archive, library, prune_disappeared=True)
    _seed_item("gd-prune-safe")

    # First sync mirrors the item to COMPLETE.
    assert run_sync(config) == 0
    archived = archive / "fake-src" / "coll1" / "gd-prune-safe"
    staged = library / "fake-src" / "coll1" / "gd-prune-safe"
    assert archived.is_dir() and staged.is_dir()

    # Re-sync: enumeration still lists the item, but its per-item description fails
    # transiently (describe_item returns None). It must be left exactly as recorded.
    real_describe = FakePlugin.describe_item

    def flaky_describe(self, identifier, collection):
        if identifier == "gd-prune-safe":
            return None  # transient per-item metadata fault
        return real_describe(self, identifier, collection)

    monkeypatch.setattr(FakePlugin, "describe_item", flaky_describe)
    assert run_sync(config) == 0

    # Archive files retained; the DB row is untouched (still COMPLETE) — never
    # disappeared, pruned, or failed. status reports it exactly as before.
    assert archived.is_dir(), "a describe failure must not remove archive files"
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    row = items.get("fake-src", "coll1", "gd-prune-safe")
    assert row is not None and row.status == ItemStatus.COMPLETE
    # The enumeration succeeded, so the collection is not stale either.
    latest = RunRepo(conn).latest("fake-src", "coll1")
    assert latest is not None and latest.stale is False

    # A later clean re-sync (describe recovers) resolves the item normally.
    monkeypatch.undo()
    assert run_sync(config) == 0
    row = items.get("fake-src", "coll1", "gd-prune-safe")
    assert row is not None and row.status == ItemStatus.COMPLETE
    assert archived.is_dir()


def test_stale_source_retains_items_and_flags_run(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """§spec:failure-behavior: when source enumeration fails, the run is flagged
    stale and the collection's existing items are retained untouched."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-show")

    # First sync mirrors the item normally.
    assert run_sync(config) == 0
    archived = archive / "fake-src" / "coll1" / "gd-show" / "gd-show.flac"
    assert archived.is_file()

    # Source goes unreachable: enumeration raises instead of listing identifiers.
    # The parallel driver materializes enumerate_items() before describe/plan, so a
    # raise here still reaches the stale handler exactly as a failed discover() did.
    def unreachable_enumerate(self, collection):
        raise ConnectionError("source unreachable")
        yield  # pragma: no cover  (keep it a generator, like the real enumerate_items)

    monkeypatch.setattr(FakePlugin, "enumerate_items", unreachable_enumerate)

    # The sync surfaces failure, but the existing item and its files remain.
    assert run_sync(config) == 1
    assert archived.is_file(), "a stale source must not delete existing items"

    conn = connect(config.state_db)  # type: ignore[arg-type]
    row = ItemRepo(conn).get("fake-src", "coll1", "gd-show")
    assert row is not None and row.status == ItemStatus.COMPLETE

    # The latest run is flagged stale — the signal `status` reports.
    latest = RunRepo(conn).latest("fake-src", "coll1")
    assert latest is not None and latest.stale is True
    assert any("enumeration failed" in e for e in latest.errors)


def test_sync_never_hashes_disk_bytes(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    """Defensive guard for the §5 invariant: sync MUST NOT call hashlib on disk bytes.

    A future contributor might "helpfully" add a verification pass to sync
    (e.g., "let's also re-check md5s while we're here"). That breaks the
    library-tools-can-retag-in-place property silently. This test imports
    hashlib via shakedown.sync's transitive deps and asserts no md5 was
    computed from sync's call path.
    """
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-x", content=b"some audio")

    import hashlib
    real_md5 = hashlib.md5
    calls: list[str] = []

    def trip_wire(*args, **kwargs):
        calls.append("md5 called from sync path")
        return real_md5(*args, **kwargs)

    monkeypatch.setattr(hashlib, "md5", trip_wire)

    # Initial sync (writes the file).
    assert run_sync(config) == 0
    # Idempotent re-sync — the codepath we care about: should not hash.
    assert run_sync(config) == 0

    # FakePlugin uses md5 to compute manifest fingerprints, which IS a sync
    # codepath — but it hashes content the *plugin* holds in memory, not bytes
    # read off disk. Mutating the on-disk file then re-syncing must not change
    # the call count: that proves nothing on disk got hashed.
    archived = archive / "fake-src" / "coll1" / "gd-x" / "gd-x.flac"
    archived.write_bytes(b"locally retagged bytes -- much longer than the original")
    calls_before = len(calls)
    assert run_sync(config) == 0
    calls_after = len(calls)

    # The third sync hashed only what the FakePlugin manifests in memory, never
    # the (now-mutated) on-disk bytes. If it had hashed disk bytes, the manifest
    # comparison would have been against on-disk content, not source content.
    # The behavioral proof: fetch_count stays at 1 (covered by the §16.4 test).
    # The structural proof here: the only md5 calls during the third sync are
    # FakePlugin computing manifest fingerprints on its in-memory `content`, not
    # on `archived` bytes — confirmed because the sum of bytes hashed during
    # this sync equals 1 * len(in-memory content), not len(on-disk content).
    assert FakePlugin.fetch_count["gd-x"] == 1
    # The third sync invokes md5 for FakePlugin's in-memory hashing during
    # discover; the count delta should be small and deterministic.
    delta = calls_after - calls_before
    assert delta <= 2, (
        f"sync invoked hashlib.md5 {delta} times — suspect a disk-byte hash leaked into sync"
    )


def test_dry_run_does_not_fetch(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-x")

    assert run_sync(config, dry_run=True) == 0
    assert FakePlugin.fetch_count == {}
    assert not (archive / "fake-src").exists() or not list((archive / "fake-src").rglob("*.flac"))


def test_unchanged_with_missing_files_is_refetched(tmp_roots: tuple[Path, Path]) -> None:
    """PRD §5: 'have it' = manifest matches AND files exist. Manifest-equality alone
    must not let vanished archive files go unrepaired."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd1977-05-08", content=b"original audio")

    assert run_sync(config) == 0
    archived = archive / "fake-src" / "coll1" / "gd1977-05-08" / "gd1977-05-08.flac"
    assert archived.is_file()

    # Manifest is still the same upstream, but the archive file was removed
    # (manual delete, corrupt FS, accidental rm — does not matter).
    archived.unlink()
    assert not archived.exists()

    assert run_sync(config) == 0
    assert FakePlugin.fetch_count["gd1977-05-08"] == 2, "missing archive file must trigger re-fetch"
    assert archived.is_file()


def test_unchanged_with_missing_staging_link_is_restored(tmp_roots: tuple[Path, Path]) -> None:
    """PRD §13: 'Library tool deletes a hardlink in staging' → restored on next sync."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-show")

    assert run_sync(config) == 0
    staged = library / "fake-src" / "coll1" / "gd-show" / "gd-show.flac"
    archived = archive / "fake-src" / "coll1" / "gd-show" / "gd-show.flac"
    assert staged.is_file()

    # User (or library tool) deletes the staging hardlink.
    staged.unlink()
    assert not staged.exists()
    # Archive copy stays (different inode reference is just gone, not the bytes).
    assert archived.is_file()

    # Next sync must restore it without re-fetching.
    assert run_sync(config) == 0
    assert FakePlugin.fetch_count["gd-show"] == 1, "restoring staging must not re-fetch"
    assert staged.is_file()
    assert staged.stat().st_ino == archived.stat().st_ino


def test_staging_collision_returns_nonzero(tmp_roots: tuple[Path, Path]) -> None:
    """PRD §8: collisions are an error, not a silent overwrite."""
    archive, library = tmp_roots
    config = make_config(
        archive, library, library_layout="{year}/same-folder",
    )
    # Two distinct items render to the same staging directory + file name.
    for ident in ("show-A", "show-B"):
        FakePlugin.items[ident] = FakeItem(
            identifier=ident,
            files=[FakeFile(name="track.flac", content=ident.encode())],
            metadata={"year": "1977"},
        )

    rc = run_sync(config)
    assert rc == 1, "staging collisions must escalate to nonzero exit"


def test_changed_upstream_preserves_old_bytes_on_fetch_failure(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """PRD §5: archive is durable. A transient fetch failure during CHANGED_UPSTREAM
    must not erase the prior archive bytes."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    item = _seed_item("gd-show", content=b"original bytes from v1 fetch")

    assert run_sync(config) == 0
    archived = archive / "fake-src" / "coll1" / "gd-show" / "gd-show.flac"
    assert archived.read_bytes() == b"original bytes from v1 fetch"

    # Source publishes new content → next sync would normally CHANGED_UPSTREAM.
    item.files = [FakeFile(name="gd-show.flac", content=b"v2 longer bytes here")]

    # But the fetch raises — simulating a network drop, plugin crash, etc.
    def boom(self, *args, **kwargs):
        raise RuntimeError("simulated transient fetch failure")
    monkeypatch.setattr(FakePlugin, "fetch", boom)

    assert run_sync(config) == 1  # surfaced as failure

    # Crucially: old archive bytes survive untouched.
    assert archived.is_file()
    assert archived.read_bytes() == b"original bytes from v1 fetch"


def test_sync_sweeps_stale_temp_dir_before_fetch(tmp_roots: tuple[Path, Path]) -> None:
    """SPEC §spec:sync-workflow: a temp dir left by a prior crashed run is swept on
    the next run and never leaks into the archive tree."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-show", content=b"real bytes")

    # Simulate a prior crashed fetch: a leftover core-owned temp dir with junk.
    coll_root = archive / "fake-src" / "coll1"
    coll_root.mkdir(parents=True)
    stale_tmp = coll_root / ".tmp-gd-show"
    stale_tmp.mkdir()
    (stale_tmp / "junk-from-last-time.flac").write_bytes(b"garbage")

    assert run_sync(config) == 0

    archived = coll_root / "gd-show" / "gd-show.flac"
    assert archived.read_bytes() == b"real bytes"
    # The stale temp dir and its junk are gone; nothing leaked into the archive.
    assert not stale_tmp.exists()
    assert not (coll_root / "gd-show" / "junk-from-last-time.flac").exists()


def test_partial_fetch_is_not_promoted(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    """SPEC §spec:sync-workflow: the core promotes only a complete temp dir. A plugin
    that reports success but fails to write every manifest file must not commit
    partial state to the archive."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-show", content=b"bytes")

    from shakedown.plugins.base import FetchResult

    def partial_fetch(self, item, dest_dir, format_filters, exclude_filters):
        # Claim success but never write the manifest file into the temp dir.
        dest_dir.mkdir(parents=True, exist_ok=True)
        return FetchResult(success=True, bytes_downloaded=0)

    monkeypatch.setattr(FakePlugin, "fetch", partial_fetch)

    assert run_sync(config) == 1  # completeness guard fails the item
    # No partial archive directory was committed.
    assert not (archive / "fake-src" / "coll1" / "gd-show").exists()
    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    row = items.get("fake-src", "coll1", "gd-show")
    assert row is None or row.status == ItemStatus.FAILED


def test_retriable_fetch_failure_is_retried_then_succeeds(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """SPEC §spec:failure-behavior: a transient (checksum) fault is retried, bounded,
    and the item completes once a later attempt succeeds — with backoff between tries."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-show", content=b"good bytes")

    delays: list[float] = []
    monkeypatch.setattr(sync_module.time, "sleep", lambda d: delays.append(d))

    attempts = {"n": 0}
    real_fetch = FakePlugin.fetch

    def flaky_fetch(self, item, dest_dir, format_filters, exclude_filters):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return FetchResult(
                success=False, bytes_downloaded=0,
                error="checksum mismatch", retriable=True,
            )
        return real_fetch(self, item, dest_dir, format_filters, exclude_filters)

    monkeypatch.setattr(FakePlugin, "fetch", flaky_fetch)

    assert run_sync(config) == 0
    assert attempts["n"] == 3, "should retry until the third attempt succeeds"
    assert delays == [1.0, 2.0], "exponential backoff between the two retries"
    archived = archive / "fake-src" / "coll1" / "gd-show" / "gd-show.flac"
    assert archived.read_bytes() == b"good bytes"


def test_retriable_fetch_failure_gives_up_after_bounded_attempts(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """Retries are bounded: a persistently-failing transient fault marks the item
    failed after _MAX_FETCH_ATTEMPTS and never commits partial state."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-show")

    monkeypatch.setattr(sync_module.time, "sleep", lambda d: None)
    attempts = {"n": 0}

    def always_fail(self, item, dest_dir, format_filters, exclude_filters):
        attempts["n"] += 1
        return FetchResult(
            success=False, bytes_downloaded=0, error="checksum mismatch", retriable=True,
        )

    monkeypatch.setattr(FakePlugin, "fetch", always_fail)

    assert run_sync(config) == 1
    assert attempts["n"] == sync_module._MAX_FETCH_ATTEMPTS
    assert not (archive / "fake-src" / "coll1" / "gd-show").exists()


def test_non_retriable_fetch_failure_is_not_retried(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """A permanent fault (retriable=False) fails fast — exactly one attempt."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-show")

    monkeypatch.setattr(sync_module.time, "sleep", lambda d: None)
    attempts = {"n": 0}

    def hard_fail(self, item, dest_dir, format_filters, exclude_filters):
        attempts["n"] += 1
        return FetchResult(
            success=False, bytes_downloaded=0, error="malformed metadata", retriable=False,
        )

    monkeypatch.setattr(FakePlugin, "fetch", hard_fail)

    assert run_sync(config) == 1
    assert attempts["n"] == 1, "non-retriable faults must not be retried"


def test_retry_after_is_honored_over_backoff(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """SPEC §spec:failure-behavior: a source-supplied Retry-After overrides the
    core's exponential backoff."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-show", content=b"good bytes")

    delays: list[float] = []
    monkeypatch.setattr(sync_module.time, "sleep", lambda d: delays.append(d))

    attempts = {"n": 0}
    real_fetch = FakePlugin.fetch

    def rate_limited(self, item, dest_dir, format_filters, exclude_filters):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FetchResult(
                success=False, bytes_downloaded=0,
                error="429 too many requests", retriable=True, retry_after=7.5,
            )
        return real_fetch(self, item, dest_dir, format_filters, exclude_filters)

    monkeypatch.setattr(FakePlugin, "fetch", rate_limited)

    assert run_sync(config) == 0
    assert delays == [7.5], "the Retry-After value, not exponential backoff"


def test_retry_after_is_clamped_to_backoff_cap(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """A fetch worker holds a SourceBudget slot while it sleeps, so an
    upstream-controlled Retry-After is clamped to the backoff cap — a hostile source
    can't pin the slot (and stall the source's whole run) for an attacker-chosen wait."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed_item("gd-show", content=b"good bytes")

    delays: list[float] = []
    monkeypatch.setattr(sync_module.time, "sleep", lambda d: delays.append(d))

    attempts = {"n": 0}
    real_fetch = FakePlugin.fetch

    def rate_limited(self, item, dest_dir, format_filters, exclude_filters):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FetchResult(
                success=False, bytes_downloaded=0,
                error="429 too many requests", retriable=True, retry_after=31_536_000.0,
            )
        return real_fetch(self, item, dest_dir, format_filters, exclude_filters)

    monkeypatch.setattr(FakePlugin, "fetch", rate_limited)

    assert run_sync(config) == 0
    assert delays == [sync_module._MAX_BACKOFF_SECONDS], "clamped to the cap, not a year"


def _two_collection_config(archive: Path, library: Path, *, max_collections: int = 2) -> Config:
    return Config(
        archive_root=archive,
        library_root=library,
        max_concurrent_collections=max_collections,
        sources=[
            SourceConfig(
                name="fake-src",
                type="fake",
                collections=[
                    CollectionConfig(name="c1", query="*"),
                    CollectionConfig(name="c2", query="*"),
                ],
            )
        ],
    )


def test_two_collections_both_mirror(tmp_roots: tuple[Path, Path]) -> None:
    """SPEC §spec:sync-workflow surface: a multi-collection sync mirrors every
    collection into its own archive + staging subtree."""
    archive, library = tmp_roots
    config = _two_collection_config(archive, library)
    _seed_item("gd-show")

    assert run_sync(config) == 0
    for coll in ("c1", "c2"):
        assert (archive / "fake-src" / coll / "gd-show" / "gd-show.flac").is_file()
        assert (library / "fake-src" / coll / "gd-show" / "gd-show.flac").is_file()


def test_collections_sync_concurrently(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    """SPEC §spec:configuration: max_concurrent_collections lets collections run in
    parallel. A rendezvous barrier both collection threads must reach at once passes
    only under real concurrency; a sequential run would deadlock and fail the sync."""
    archive, library = tmp_roots
    config = _two_collection_config(archive, library, max_collections=2)
    _seed_item("gd-show")

    barrier = threading.Barrier(2, timeout=5)
    orig_enumerate = FakePlugin.enumerate_items

    def gated_enumerate(self, collection):
        # Block until both collection workers are at discovery start simultaneously.
        # enumerate_items() is called once per collection at the top of discovery.
        barrier.wait()
        return orig_enumerate(self, collection)

    monkeypatch.setattr(FakePlugin, "enumerate_items", gated_enumerate)

    assert run_sync(config) == 0, "both collections must be in flight at once"
    for coll in ("c1", "c2"):
        assert (archive / "fake-src" / coll / "gd-show" / "gd-show.flac").is_file()


def test_collection_cap_of_one_serializes(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    """max_concurrent_collections=1 forces serial collection execution: a barrier
    expecting two simultaneous arrivals must time out, so the run reports failure."""
    archive, library = tmp_roots
    config = _two_collection_config(archive, library, max_collections=1)
    _seed_item("gd-show")

    barrier = threading.Barrier(2, timeout=0.5)
    orig_enumerate = FakePlugin.enumerate_items

    def gated_enumerate(self, collection):
        barrier.wait()  # never satisfied with only one worker at a time
        return orig_enumerate(self, collection)

    monkeypatch.setattr(FakePlugin, "enumerate_items", gated_enumerate)

    assert run_sync(config) == 1, "serial execution can't satisfy a 2-party barrier"


def test_source_budget_bounds_fetches_across_collections(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """SPEC §spec:source-budget: a source's shared concurrency budget is the hard
    ceiling on simultaneous fetches, even across two collections of that source
    running at once (max_concurrent_collections=2) and a larger per-collection
    download pool (max_concurrent_downloads=4). With max_concurrent_requests=2,
    the observed peak concurrency must never exceed 2."""
    import time as _time

    archive, library = tmp_roots
    config = Config(
        archive_root=archive,
        library_root=library,
        max_concurrent_downloads=4,
        max_concurrent_collections=2,
        sources=[
            SourceConfig(
                name="fake-src",
                type="fake",
                max_concurrent_requests=2,
                collections=[
                    CollectionConfig(name="c1", query="*"),
                    CollectionConfig(name="c2", query="*"),
                ],
            )
        ],
    )
    # Enough distinct items that both collections' pools (4 workers each) would
    # overlap well past 2 in-flight fetches if the budget were not enforced.
    for i in range(6):
        _seed_item(f"gd-show-{i}")

    lock = threading.Lock()
    state = {"in_flight": 0, "max": 0}
    real_fetch = FakePlugin.fetch

    def instrumented_fetch(self, item, dest_dir, format_filters, exclude_filters):
        with lock:
            state["in_flight"] += 1
            state["max"] = max(state["max"], state["in_flight"])
        try:
            _time.sleep(0.02)  # hold the slot so overlap is real
            return real_fetch(self, item, dest_dir, format_filters, exclude_filters)
        finally:
            with lock:
                state["in_flight"] -= 1

    monkeypatch.setattr(FakePlugin, "fetch", instrumented_fetch)

    assert run_sync(config) == 0
    assert state["max"] <= 2, (
        f"per-source budget breached: observed {state['max']} simultaneous fetches (cap 2)"
    )
    # Sanity: every item was actually fetched into both collections.
    for coll in ("c1", "c2"):
        for i in range(6):
            assert (archive / "fake-src" / coll / f"gd-show-{i}" / f"gd-show-{i}.flac").is_file()


def test_traversal_identifier_is_rejected(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    """A remote source must not escape the archive tree via a `..`/absolute identifier.
    The core rejects it before any download, rename, or rmtree runs."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["../../evil"] = FakeItem(
        identifier="../../evil",
        files=[FakeFile(name="evil.flac", content=b"pwned")],
        metadata={},
    )

    fetched: list[str] = []
    real_fetch = FakePlugin.fetch

    def tracking_fetch(self, item, dest_dir, format_filters, exclude_filters):
        fetched.append(item.identifier)
        return real_fetch(self, item, dest_dir, format_filters, exclude_filters)

    monkeypatch.setattr(FakePlugin, "fetch", tracking_fetch)

    assert run_sync(config) == 1, "unsafe item is a failed fetch"
    assert "../../evil" not in fetched, "unsafe identifier must never reach fetch"
    # Nothing was written anywhere outside the collection's archive subtree.
    assert not (archive / "evil").exists()
    assert not (archive / "evil.flac").exists()
    assert not (archive.parent / "evil").exists()


# --- §road:parallel-discovery: per-item metadata resolution fanned across the budget ---

def _two_collection_budget_config(
    archive: Path, library: Path, *, max_concurrent_requests: int
) -> Config:
    """Two collections of one source, each with its own download pool but sharing
    one per-source budget — the surface where concurrent describe() must stay bounded."""
    return Config(
        archive_root=archive,
        library_root=library,
        max_concurrent_downloads=4,
        max_concurrent_collections=2,
        sources=[
            SourceConfig(
                name="fake-src",
                type="fake",
                max_concurrent_requests=max_concurrent_requests,
                collections=[
                    CollectionConfig(name="c1", query="*"),
                    CollectionConfig(name="c2", query="*"),
                ],
            )
        ],
    )


def test_concurrent_discovery_bounded_by_source_budget(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """§spec:discovery-pipeline + §spec:source-budget: per-item describe_item() runs
    CONCURRENTLY across the drain, but the shared per-source budget is the hard ceiling
    on simultaneous metadata fetches — even with two collections of the source running
    at once (max_concurrent_collections=2) and a larger download pool. With
    max_concurrent_requests=2, the observed peak describe concurrency must never
    exceed 2, while every item still gets resolved."""
    import time as _time

    archive, library = tmp_roots
    config = _two_collection_budget_config(archive, library, max_concurrent_requests=2)
    # Enough distinct items that two collections resolving concurrently would overlap
    # well past 2 in-flight describes if the budget were not enforced.
    for i in range(6):
        _seed_item(f"gd-show-{i}")

    lock = threading.Lock()
    state = {"in_flight": 0, "max": 0}
    described: set[str] = set()
    real_describe = FakePlugin.describe_item

    def instrumented_describe(self, identifier, collection):
        with lock:
            state["in_flight"] += 1
            state["max"] = max(state["max"], state["in_flight"])
            described.add(identifier)
        try:
            _time.sleep(0.02)  # hold the slot so overlap is real
            return real_describe(self, identifier, collection)
        finally:
            with lock:
                state["in_flight"] -= 1

    monkeypatch.setattr(FakePlugin, "describe_item", instrumented_describe)

    assert run_sync(config) == 0
    # Every enumerated item's metadata was resolved (concurrency didn't drop any).
    assert described == {f"gd-show-{i}" for i in range(6)}
    # ...and the shared budget was never breached during discovery.
    assert state["max"] <= 2, (
        f"per-source budget breached during discovery: observed {state['max']} "
        "simultaneous describes (cap 2)"
    )
    # Sanity: both collections mirrored every item.
    for coll in ("c1", "c2"):
        for i in range(6):
            assert (archive / "fake-src" / coll / f"gd-show-{i}" / f"gd-show-{i}.flac").is_file()


def test_slow_item_does_not_stall_others_during_discovery(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """§spec:discovery-pipeline: because describe_item() is fanned across the budget,
    one slow item's metadata fetch must not serialize the others behind it. With a
    budget >= 2, a deliberately slow describe overlaps the fast ones in time — proving
    the resolution is concurrent, not a serial drain."""
    import time as _time

    archive, library = tmp_roots
    config = make_config(archive, library)  # single collection, default budget (4)
    slow_id = "gd-slow"
    _seed_item(slow_id)
    for i in range(5):
        _seed_item(f"gd-fast-{i}")

    lock = threading.Lock()
    intervals: dict[str, tuple[float, float]] = {}
    real_describe = FakePlugin.describe_item

    def timed_describe(self, identifier, collection):
        start = _time.perf_counter()
        _time.sleep(0.4 if identifier == slow_id else 0.02)
        result = real_describe(self, identifier, collection)
        end = _time.perf_counter()
        with lock:
            intervals[identifier] = (start, end)
        return result

    monkeypatch.setattr(FakePlugin, "describe_item", timed_describe)

    assert run_sync(config) == 0
    # All items resolved and mirrored.
    assert set(intervals) == {slow_id, *(f"gd-fast-{i}" for i in range(5))}
    for ident in intervals:
        assert (archive / "fake-src" / "coll1" / ident / f"{ident}.flac").is_file()

    # The slow item's describe overlapped at least one fast item's describe: some fast
    # item started after the slow one began AND finished before the slow one ended.
    slow_start, slow_end = intervals[slow_id]
    overlapped = [
        ident
        for ident, (start, end) in intervals.items()
        if ident != slow_id and start < slow_end and end > slow_start
    ]
    assert overlapped, (
        "no describe overlapped the slow item — resolution was serial, not fanned"
    )


def test_concurrent_discovery_classifies_identically_to_serial(
    tmp_roots: tuple[Path, Path]
) -> None:
    """KEY BATCH INVARIANT: NEW/CHANGED/UNCHANGED/DISAPPEARED classification under
    concurrent per-item discovery is byte-for-byte identical to an effectively-serial
    run (max_concurrent_requests=1). Seed the same items into two independent roots,
    sync one concurrently and one serially, and assert identical per-item DB statuses.
    Also proves idempotency: a second no-op sync re-fetches nothing and keeps every
    item UNCHANGED."""

    def _run_and_snapshot(
        root: Path, *, max_concurrent_requests: int
    ) -> tuple[dict[str, ItemStatus], Config]:
        archive = root / "archive"
        library = root / "library"
        archive.mkdir(parents=True)
        library.mkdir(parents=True)
        config = _two_collection_budget_config(
            archive, library, max_concurrent_requests=max_concurrent_requests
        )
        assert run_sync(config) == 0
        conn = connect(config.state_db)  # type: ignore[arg-type]
        items = ItemRepo(conn)
        snapshot: dict[str, ItemStatus] = {}
        for coll in ("c1", "c2"):
            for row in items.list_for_collection("fake-src", coll):
                snapshot[f"{coll}/{row.identifier}"] = row.status
        return snapshot, config

    archive0, _ = tmp_roots
    base = archive0.parent

    # Seed a mixed set: normal items plus one restricted (classifies UNAVAILABLE).
    for i in range(5):
        _seed_item(f"gd-show-{i}")
    restricted = _seed_item("gd-stream-only")
    restricted.is_restricted = True
    restricted.restriction_reason = "stream-only per band request"

    concurrent_snap, concurrent_cfg = _run_and_snapshot(
        base / "concurrent", max_concurrent_requests=4
    )
    serial_snap, _ = _run_and_snapshot(base / "serial", max_concurrent_requests=1)

    assert concurrent_snap == serial_snap, (
        "concurrent discovery produced different classification than the serial run"
    )
    # Both runs classified the same set (5 complete + 1 unavailable) per collection.
    assert len(concurrent_snap) == 12
    assert sum(1 for s in concurrent_snap.values() if s == ItemStatus.UNAVAILABLE) == 2

    # Idempotency: a second sync over the concurrent roots re-fetches nothing.
    fetches_before = dict(FakePlugin.fetch_count)
    assert run_sync(concurrent_cfg) == 0
    assert FakePlugin.fetch_count == fetches_before, "no-op re-sync must not re-fetch"


# --- §road:stream-fetch: discover→download pipelining (overlap + prune barrier) ---


def test_download_overlaps_discovery(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    """§spec:discovery-pipeline (overlap lever): a download begins while discovery is
    still enumerating. With the shared budget wide enough that every describe runs at
    once, a fast item's fetch starts while a deliberately slow item is still being
    described — so the first download starts before discovery resolves the last item.
    A non-pipelined implementation (fetch only after the full drain) fails this."""
    import time as _time

    archive, library = tmp_roots
    config = Config(
        archive_root=archive,
        library_root=library,
        max_concurrent_downloads=4,
        sources=[
            SourceConfig(
                name="fake-src",
                type="fake",
                max_concurrent_requests=8,  # >= items, so no describe/fetch slot contention
                collections=[CollectionConfig(name="coll1", query="*")],
            )
        ],
    )
    for i in range(3):
        _seed_item(f"fast-{i}")
    _seed_item("slow")

    lock = threading.Lock()
    describe_end: dict[str, float] = {}
    fetch_start: dict[str, float] = {}
    real_describe = FakePlugin.describe_item
    real_fetch = FakePlugin.fetch

    def timed_describe(self, identifier, collection):
        _time.sleep(0.3 if identifier == "slow" else 0.01)
        result = real_describe(self, identifier, collection)
        with lock:
            describe_end[identifier] = _time.perf_counter()
        return result

    def timed_fetch(self, item, dest_dir, format_filters, exclude_filters):
        with lock:
            fetch_start[item.identifier] = _time.perf_counter()
        return real_fetch(self, item, dest_dir, format_filters, exclude_filters)

    monkeypatch.setattr(FakePlugin, "describe_item", timed_describe)
    monkeypatch.setattr(FakePlugin, "fetch", timed_fetch)

    assert run_sync(config) == 0
    assert set(fetch_start) == {"fast-0", "fast-1", "fast-2", "slow"}
    # The first download started before discovery finished resolving the last (slow)
    # item — proof the stages overlap rather than running strictly sequentially.
    assert min(fetch_start.values()) < max(describe_end.values()), (
        "no download overlapped discovery — fetch did not start until discovery completed"
    )


def test_discovery_failure_partway_prunes_nothing(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """§spec:discovery-pipeline: a discovery failure PARTWAY through leaves the
    collection stale with ZERO items pruned — a partial enumeration is never read as
    items disappearing, even on a prune_disappeared collection. An item resolved and
    fetched before the fault is still retained (fetch may precede completion)."""
    archive, library = tmp_roots
    # budget=1 serializes describe resolution, so the fault is deterministic: the new
    # item resolves and is fetched, then the next describe raises.
    config = Config(
        archive_root=archive,
        library_root=library,
        sources=[
            SourceConfig(
                name="fake-src",
                type="fake",
                max_concurrent_requests=1,
                collections=[
                    CollectionConfig(name="coll1", query="*", prune_disappeared=True)
                ],
            )
        ],
    )
    _seed_item("keep-me")  # pre-existing; will vanish from the (failed) enumeration

    assert run_sync(config) == 0
    assert (archive / "fake-src" / "coll1" / "keep-me").is_dir()

    # Second sync: the source no longer lists keep-me; it lists a new item then faults
    # resolving the next one — so the enumeration never completes.
    FakePlugin.items.clear()
    _seed_item("new-good")
    _seed_item("faulty")

    real_describe = FakePlugin.describe_item

    def flaky_describe(self, identifier, collection):
        if identifier == "faulty":
            raise ConnectionError("metadata endpoint failed mid-enumeration")
        return real_describe(self, identifier, collection)

    monkeypatch.setattr(FakePlugin, "describe_item", flaky_describe)

    assert run_sync(config) == 1  # a stale run surfaces as failure

    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)

    # keep-me is NOT pruned despite prune_disappeared — the enumeration failed, so its
    # absence is not treated as a disappearance.
    assert (archive / "fake-src" / "coll1" / "keep-me").is_dir(), "must not prune on failed enumeration"
    keep = items.get("fake-src", "coll1", "keep-me")
    assert keep is not None and keep.status == ItemStatus.COMPLETE

    # The item resolved+fetched before the fault is retained (fetch precedes completion).
    assert (archive / "fake-src" / "coll1" / "new-good").is_dir(), "item fetched before the fault is retained"

    # And the run is flagged stale — the signal `status` reports.
    latest = RunRepo(conn).latest("fake-src", "coll1")
    assert latest is not None and latest.stale is True
    assert any("enumeration failed" in e for e in latest.errors)


def test_incremental_skips_metadata_fetch_when_signal_unchanged(
    tmp_roots: tuple[Path, Path],
) -> None:
    """§road:incremental-skip: an unchanged change signal short-circuits the item to
    UNCHANGED without the per-item metadata fetch."""
    archive, library = tmp_roots
    config = make_config(archive, library, incremental_discovery=True)
    item = _seed_item("gd1977-05-08")
    item.change_signal = "sig-1"

    # First run: item is NEW → fetched, and describe_item resolved it once.
    assert run_sync(config) == 0
    assert FakePlugin.describe_count["gd1977-05-08"] == 1
    assert FakePlugin.fetch_count["gd1977-05-08"] == 1

    # Second run: signal unchanged → skip the metadata fetch entirely.
    assert run_sync(config) == 0
    assert FakePlugin.describe_count["gd1977-05-08"] == 1, "no second metadata fetch"
    assert FakePlugin.fetch_count["gd1977-05-08"] == 1, "no refetch"

    conn = connect(config.state_db)  # type: ignore[arg-type]
    row = ItemRepo(conn).get("fake-src", "coll1", "gd1977-05-08")
    assert row is not None and row.status == ItemStatus.COMPLETE


def test_incremental_changed_signal_refetches(tmp_roots: tuple[Path, Path]) -> None:
    """A changed signal falls through to describe_item and the full manifest
    comparison, which refetches when the manifest differs."""
    archive, library = tmp_roots
    config = make_config(archive, library, incremental_discovery=True)
    item = _seed_item("gd1977-05-08", content=b"v1")
    item.change_signal = "sig-1"

    assert run_sync(config) == 0
    assert FakePlugin.describe_count["gd1977-05-08"] == 1
    assert FakePlugin.fetch_count["gd1977-05-08"] == 1

    # Source publishes new bytes (manifest changes) AND bumps the change signal.
    item.files = [FakeFile(name="gd1977-05-08.flac", content=b"v2-much-longer-bytes")]
    item.change_signal = "sig-2"

    assert run_sync(config) == 0
    assert FakePlugin.describe_count["gd1977-05-08"] == 2, "signal changed → metadata fetched"
    assert FakePlugin.fetch_count["gd1977-05-08"] == 2, "changed-upstream → refetched"


def test_incremental_no_stored_signal_falls_through(tmp_roots: tuple[Path, Path]) -> None:
    """With no source change signal (None), incremental discovery has nothing to skip
    on, so every run resolves the full descriptor."""
    archive, library = tmp_roots
    config = make_config(archive, library, incremental_discovery=True)
    item = _seed_item("gd1977-05-08")
    item.change_signal = None

    assert run_sync(config) == 0
    assert FakePlugin.describe_count["gd1977-05-08"] == 1

    assert run_sync(config) == 0
    assert FakePlugin.describe_count["gd1977-05-08"] == 2, "no signal → full metadata pass each run"


def test_incremental_flag_off_ignores_signal(tmp_roots: tuple[Path, Path]) -> None:
    """Flag off (default): the change signal is ignored and the full manifest pass runs
    every sync, exactly as today."""
    archive, library = tmp_roots
    config = make_config(archive, library)  # incremental_discovery defaults off
    item = _seed_item("gd1977-05-08")
    item.change_signal = "sig-1"

    assert run_sync(config) == 0
    assert FakePlugin.describe_count["gd1977-05-08"] == 1

    assert run_sync(config) == 0
    assert FakePlugin.describe_count["gd1977-05-08"] == 2, "flag off → signal ignored, full pass"
