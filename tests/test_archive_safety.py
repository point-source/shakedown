"""Archive write safety and run integrity: an empty or short upstream answer, or
half-written state, must fail — never count as done."""
from __future__ import annotations

from pathlib import Path

import shakedown.staging as staging_module
import shakedown.sync as sync_module
from shakedown.db import connect
from shakedown.models import ItemStatus, OperationStatus
from shakedown.state import ItemRepo, OperationOutcomeRepo, RunRepo
from shakedown.sync import refetch_item, run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _seed(identifier: str, *files: tuple[str, bytes]) -> FakeItem:
    item = FakeItem(
        identifier=identifier,
        files=[FakeFile(name=n, content=c) for n, c in files]
        or [FakeFile(name=f"{identifier}.flac", content=b"hello world")],
        metadata={"title": "Show"},
    )
    FakePlugin.items[identifier] = item
    return item


def _row(config, identifier: str):
    return ItemRepo(connect(config.state_db)).get("fake-src", "coll1", identifier)  # type: ignore[arg-type]


# --- C2: an empty upstream file list never replaces the archive copy ------------


def test_empty_upstream_manifest_keeps_complete_archive_copy(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    item = _seed("gd-show")
    assert run_sync(config) == 0
    archived = archive / "fake-src" / "coll1" / "gd-show" / "gd-show.flac"
    assert archived.read_bytes() == b"hello world"

    # archive.org answers with zero files (darked item, glitch, filter matches nothing).
    item.files = []
    run_sync(config)

    assert archived.read_bytes() == b"hello world", "local copy must be untouched"
    row = _row(config, "gd-show")
    assert row is not None
    assert row.status == ItemStatus.UNAVAILABLE
    assert row.restriction_reason
    assert row.archive_path == archived.parent
    assert row.recorded_manifest is not None and len(row.recorded_manifest.files) == 1
    assert FakePlugin.fetch_count["gd-show"] == 1


def test_empty_upstream_manifest_for_new_item_is_unavailable(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-empty").files = []

    run_sync(config)

    assert not (archive / "fake-src" / "coll1" / "gd-empty").exists()
    row = _row(config, "gd-empty")
    assert row is not None and row.status == ItemStatus.UNAVAILABLE
    assert FakePlugin.fetch_count.get("gd-empty", 0) == 0


def test_item_back_upstream_with_same_files_returns_to_complete(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    item = _seed("gd-show")
    assert run_sync(config) == 0
    files = item.files
    item.files = []
    run_sync(config)
    assert _row(config, "gd-show").status == ItemStatus.UNAVAILABLE  # type: ignore[union-attr]

    item.files = files
    assert run_sync(config) == 0

    row = _row(config, "gd-show")
    assert row is not None and row.status == ItemStatus.COMPLETE
    assert row.restriction_reason is None
    assert FakePlugin.fetch_count["gd-show"] == 1, "same files on disk: no re-download"


def test_refetch_with_empty_upstream_manifest_keeps_archive_copy(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    item = _seed("gd-show")
    assert run_sync(config) == 0
    item.files = []

    assert refetch_item(config, "gd-show") == 1

    archived = archive / "fake-src" / "coll1" / "gd-show" / "gd-show.flac"
    assert archived.read_bytes() == b"hello world"
    row = _row(config, "gd-show")
    assert row is not None and row.archive_path == archived.parent


# --- H4: a disk error while linking fails one show, never the collection ---------


def _flaky_hardlink(monkeypatch, bad_identifier: str, exc: OSError) -> None:
    real = staging_module.hardlink

    def hardlink(src, dst):
        if bad_identifier in str(src):
            raise exc
        return real(src, dst)

    monkeypatch.setattr(staging_module, "hardlink", hardlink)


def test_link_oserror_fails_only_that_show(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    for ident in ("gd-a", "gd-b", "gd-c"):
        _seed(ident)
    _flaky_hardlink(monkeypatch, "gd-b", PermissionError(13, "Permission denied"))

    assert run_sync(config) == 1

    for ident in ("gd-a", "gd-c"):
        assert (library / "fake-src" / "coll1" / ident / f"{ident}.flac").is_file()
        assert _row(config, ident).status == ItemStatus.COMPLETE  # type: ignore[union-attr]
    # gd-b's download is safe in the archive and recorded, so nothing re-downloads.
    row_b = _row(config, "gd-b")
    assert row_b is not None and row_b.status == ItemStatus.COMPLETE
    conn = connect(config.state_db)  # type: ignore[arg-type]
    run = RunRepo(conn).latest("fake-src", "coll1")
    assert run is not None and run.finished_at is not None
    assert run.items_failed == 1
    assert any("gd-b" in e and "Permission denied" in e for e in run.errors)

    # The disk error clears: the next run links gd-b without downloading again.
    monkeypatch.undo()
    assert run_sync(config) == 0
    assert (library / "fake-src" / "coll1" / "gd-b" / "gd-b.flac").is_file()
    assert FakePlugin.fetch_count == {"gd-a": 1, "gd-b": 1, "gd-c": 1}


def test_link_oserror_on_unchanged_show_is_counted_and_run_continues(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-a")
    _seed("gd-b")
    assert run_sync(config) == 0
    # Library links wiped; relinking gd-a hits a disk-full error.
    import shutil

    shutil.rmtree(library / "fake-src")
    _flaky_hardlink(monkeypatch, "gd-a", OSError(28, "No space left on device"))

    assert run_sync(config) == 1

    assert (library / "fake-src" / "coll1" / "gd-b" / "gd-b.flac").is_file()
    run = RunRepo(connect(config.state_db)).latest("fake-src", "coll1")  # type: ignore[arg-type]
    assert run is not None and run.finished_at is not None and run.items_failed == 1


def test_unexpected_crash_still_closes_run_record(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-a")

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(sync_module, "_disappeared_plan", boom)

    assert run_sync(config) == 1

    conn = connect(config.state_db)  # type: ignore[arg-type]
    run = RunRepo(conn).latest("fake-src", "coll1")
    assert run is not None and run.finished_at is not None
    assert any("unexpected bug" in e for e in run.errors)
    outcome = OperationOutcomeRepo(conn).latest_actionable("fake-src", "coll1")
    assert outcome is not None
    assert outcome.status == OperationStatus.FAILED_BEFORE_COMPLETION


# --- H6: one writer at a time -----------------------------------------------------


def test_second_writer_exits_with_clear_error(
    tmp_roots: tuple[Path, Path], caplog
) -> None:
    from shakedown.lock import writer_lock
    from shakedown.reconcile import run_reconcile
    from shakedown.restage import run_restage
    from shakedown.verify import run_verify

    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-a")
    assert run_sync(config) == 0

    with writer_lock(config):
        caplog.clear()
        assert run_sync(config) == 1
        assert "another shakedown run" in caplog.text
        assert run_restage(config) == 1
        assert run_reconcile(config) == 1
        assert refetch_item(config, "gd-a") == 1
        assert run_verify(
            config, source_filter=None, collection_filter=None, deep=True,
            reconform=True, list_drift=False, assume_yes=True,
        ) == 1
        # A dry run writes nothing, so it does not need the lock.
        assert run_sync(config, dry_run=True) == 0

    assert FakePlugin.fetch_count["gd-a"] == 1
    assert run_sync(config) == 0, "lock released when the holder finishes"


def test_writer_lock_released_when_holder_is_killed(tmp_roots: tuple[Path, Path]) -> None:
    import os
    import signal
    import subprocess
    import sys

    from shakedown.lock import lock_path, writer_lock

    archive, library = tmp_roots
    config = make_config(archive, library)
    path = lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c", (
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n"
        ), str(path)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        assert run_sync(config) == 1
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)
        with writer_lock(config):
            pass
    finally:
        if holder.poll() is None:
            holder.kill()


# --- M4: nothing short or unsynced is recorded complete ----------------------------


def test_short_file_is_retried_and_never_recorded_complete(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    from shakedown.plugins.base import FetchResult

    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-a", ("a.flac", b"full length audio"))
    monkeypatch.setattr(sync_module.time, "sleep", lambda d: None)
    attempts = {"n": 0}

    def short_fetch(self, item, dest_dir, format_filters, exclude_filters):
        attempts["n"] += 1
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "a.flac").write_bytes(b"full")  # truncated, but claims success
        return FetchResult(success=True, bytes_downloaded=4)

    monkeypatch.setattr(FakePlugin, "fetch", short_fetch)

    assert run_sync(config) == 1

    assert attempts["n"] == sync_module._MAX_FETCH_ATTEMPTS, "a short file is retried"
    assert not (archive / "fake-src" / "coll1" / "gd-a").exists()
    row = _row(config, "gd-a")
    assert row is not None and row.status == ItemStatus.FAILED
    assert "size" in (row.restriction_reason or "")


def test_files_are_fsynced_before_the_show_is_recorded(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    import os

    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-a", ("a.flac", b"one"), ("b.flac", b"two"))
    events: list[str] = []
    real_fsync = os.fsync

    def fsync(fd):
        events.append("fsync " + os.readlink(f"/proc/self/fd/{fd}"))
        return real_fsync(fd)

    real_record = sync_module._record_complete

    def record(*args, **kwargs):
        events.append("record")
        return real_record(*args, **kwargs)

    monkeypatch.setattr(sync_module.os, "fsync", fsync)
    monkeypatch.setattr(sync_module, "_record_complete", record)

    assert run_sync(config) == 0

    before_record = events[: events.index("record")]
    synced = {e.removeprefix("fsync ").rsplit("/", 1)[-1] for e in before_record}
    assert {"a.flac", "b.flac", ".tmp-gd-a", "coll1"} <= synced


def test_crash_mid_swap_restores_the_old_copy(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    item = _seed("gd-a")
    assert run_sync(config) == 0
    coll = archive / "fake-src" / "coll1"
    # Crash between "move the old copy aside" and "move the new copy in".
    (coll / "gd-a").rename(coll / ".stale-gd-a")
    # Even if upstream now answers with nothing, the old copy must come back.
    item.files = []

    run_sync(config)

    assert (coll / "gd-a" / "gd-a.flac").read_bytes() == b"hello world"
    assert not (coll / ".stale-gd-a").exists()
    assert FakePlugin.fetch_count["gd-a"] == 1


def test_finished_swap_discards_the_leftover_old_copy(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-a")
    assert run_sync(config) == 0
    coll = archive / "fake-src" / "coll1"
    # Crash after the new copy was moved in, before the old one was removed.
    (coll / ".stale-gd-a").mkdir()
    (coll / ".stale-gd-a" / "gd-a.flac").write_bytes(b"old bytes")

    assert run_sync(config) == 0

    assert (coll / "gd-a" / "gd-a.flac").read_bytes() == b"hello world"
    assert not (coll / ".stale-gd-a").exists()


def test_abandoned_temp_dirs_are_removed_but_failed_shows_keep_theirs(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    from shakedown.plugins.base import FetchResult

    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-a")
    _seed("gd-b")
    assert run_sync(config) == 0
    _seed("gd-c")
    coll = archive / "fake-src" / "coll1"
    for leftover in (".tmp-gone-upstream", ".tmp-gd-a"):
        (coll / leftover).mkdir()
        (coll / leftover / "x.flac").write_bytes(b"partial")
    monkeypatch.setattr(sync_module.time, "sleep", lambda d: None)

    def fail_c(self, item, dest_dir, format_filters, exclude_filters):
        (dest_dir / "gd-c.flac").write_bytes(b"hello")  # partial, kept for resume
        return FetchResult(success=False, bytes_downloaded=5, error="timeout", retriable=True)

    monkeypatch.setattr(FakePlugin, "fetch", fail_c)

    assert run_sync(config) == 1

    assert not (coll / ".tmp-gone-upstream").exists()
    assert not (coll / ".tmp-gd-a").exists()
    assert (coll / ".tmp-gd-c" / "gd-c.flac").read_bytes() == b"hello"


def test_temp_dirs_kept_when_enumeration_fails(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-a")
    assert run_sync(config) == 0
    coll = archive / "fake-src" / "coll1"
    (coll / ".tmp-gd-z").mkdir()
    FakePlugin.unreachable_sources.add("fake-src")

    assert run_sync(config) == 1

    assert (coll / ".tmp-gd-z").is_dir()


# --- M9: a finished download is recorded while the listing is still running --------


def test_download_is_recorded_and_linked_before_enumeration_finishes(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    import threading
    import time

    archive, library = tmp_roots
    config = make_config(archive, library)
    _seed("gd-a")
    _seed("gd-b")
    fetched_a = threading.Event()
    real_fetch_one = sync_module._fetch_one

    def fetch_one(plugin, target, archive_root, collection):
        outcome = real_fetch_one(plugin, target, archive_root, collection)
        if target.descriptor.identifier == "gd-a":
            fetched_a.set()
        return outcome

    observed: list[object] = []

    def slow_listing(self, collection):
        yield self.describe_item("gd-a", collection)
        assert fetched_a.wait(5)
        time.sleep(0.05)  # let the fetch future settle as done
        yield self.describe_item("gd-b", collection)
        # The listing is still running (hours, on a big etree collection).
        row = _row(config, "gd-a")
        observed.append(row.status if row else None)
        observed.append((library / "fake-src" / "coll1" / "gd-a" / "gd-a.flac").is_file())
        raise RuntimeError("container stopped")

    monkeypatch.setattr(sync_module, "_fetch_one", fetch_one)
    monkeypatch.setattr(FakePlugin, "enumerate_items", lambda self, collection: None)
    monkeypatch.setattr(FakePlugin, "discover", slow_listing)

    run_sync(config)

    assert observed == [ItemStatus.COMPLETE, True]
