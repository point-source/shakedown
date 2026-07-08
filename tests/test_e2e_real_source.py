"""Opt-in, real-network end-to-end check for the `ia` plugin (§spec:e2e-real-source).

This is the only test that reaches the network. It is marked ``network`` and
deselected by the default ``pytest`` run (and by CI); reach it deliberately with
``pytest -m network``. See README "Real-source end-to-end check" for the run
instructions and the pinned-item replacement procedure.

The check drives the `ia` plugin against exactly one pinned, small, public,
unrestricted Internet Archive item — no credentials required — through the whole
lifecycle in order: sync → hardlink staging → no-op re-sync → restage after a
layout change → disappeared retention → prune → forget. Everything runs in a
throwaway ``tmp_path`` tree (archive, library, and state DB on one filesystem),
so it can never touch a real deployment. A network or upstream failure fails the
check loudly; it never skips itself into a false pass.

Pinned item — selection criteria (§spec:e2e-real-source):

- Public and unrestricted (no ``access-restricted``), so no credentials are
  needed and the download can never be blocked.
- Small (a few hundred KB) so runs stay fast and polite to archive.org.
- Long-lived: ``testmp3testfile`` is Internet Archive's canonical MP3 test item.
- Pinned, not queried live, so runs are comparable over time.

Replacement procedure — if the pinned item ever disappears upstream this test
fails loudly (not skips). To re-pin, pick another item meeting the criteria
above (``https://archive.org/advancedsearch.php`` for candidates; confirm the
item's ``metadata`` shows no ``access-restricted`` flag), then update
``PINNED_IDENTIFIER``, ``PINNED_FILE``, ``PINNED_FILE_MD5``, ``PINNED_FILE_SIZE``,
and the template field asserted below (``PINNED_CREATOR``) to match the new
item's metadata.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect
from shakedown.models import Item, ItemStatus, Run
from shakedown.restage import run_restage
from shakedown.state import ItemRepo, RunRepo
from shakedown.sync import forget_item, run_sync

pytestmark = pytest.mark.network

SOURCE = "ia-e2e"
COLLECTION = "pinned"

# --- The pinned item (see module docstring for selection + replacement) ---
PINNED_IDENTIFIER = "testmp3testfile"
PINNED_FILE = "mpthreetest.mp3"
PINNED_FILE_MD5 = "eebc1b05345cffe3cf7a80b15f6251f6"
PINNED_FILE_SIZE = 198658
PINNED_CREATOR = "test"  # template field surfaced by the ia plugin for this item

# `format_filters=["mp3"]` narrows the item's manifest to the single small MP3,
# keeping the download to a few hundred KB.
FORMAT_FILTERS = ["mp3"]

# A query that matches exactly the pinned item.
QUERY_MATCHES = f"identifier:{PINNED_IDENTIFIER}"
# A well-formed query that matches nothing: a *successful* enumeration returning
# zero items, which is how we make the pinned item leave enumeration on purpose.
QUERY_EMPTY = f"identifier:{PINNED_IDENTIFIER}-shakedown-e2e-absent"

PASSTHROUGH = "passthrough"
# A custom layout referencing a field the ia plugin surfaces for this item.
CUSTOM_LAYOUT = "{creator|sanitize}/{identifier}"


def _config(tmp: Path, *, query: str, layout: str, prune: bool) -> Config:
    """Build a Config rooted entirely under one throwaway tmp tree."""
    archive = tmp / "archive"
    library = tmp / "library"
    archive.mkdir(exist_ok=True)
    library.mkdir(exist_ok=True)
    return Config(
        archive_root=archive,
        library_root=library,
        max_concurrent_downloads=1,
        max_concurrent_collections=1,
        sources=[
            SourceConfig(
                name=SOURCE,
                type="ia",
                collections=[
                    CollectionConfig(
                        name=COLLECTION,
                        query=query,
                        format_filters=FORMAT_FILTERS,
                        library_layout=layout,
                        prune_disappeared=prune,
                    )
                ],
            )
        ],
    )


def _record(config: Config) -> Item | None:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    return ItemRepo(conn).get(SOURCE, COLLECTION, PINNED_IDENTIFIER)


def _latest_run(config: Config) -> Run:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    run = RunRepo(conn).latest(SOURCE, COLLECTION)
    assert run is not None
    return run


def test_ia_plugin_full_lifecycle_against_pinned_item(tmp_path: Path) -> None:
    archive_file = (
        tmp_path / "archive" / SOURCE / COLLECTION / PINNED_IDENTIFIER / PINNED_FILE
    )
    passthrough_link = (
        tmp_path / "library" / SOURCE / COLLECTION / PINNED_IDENTIFIER / PINNED_FILE
    )
    custom_link = (
        tmp_path / "library" / SOURCE / COLLECTION / PINNED_CREATOR / PINNED_IDENTIFIER / PINNED_FILE
    )

    # 1. sync downloads the item for real; the plugin verifies IA's per-file
    #    checksum as bytes arrive (§spec:sync-workflow).
    config = _config(tmp_path, query=QUERY_MATCHES, layout=PASSTHROUGH, prune=False)
    assert run_sync(config) == 0, "first real sync must succeed"

    assert archive_file.is_file(), "the pinned file must land in the archive tree"
    assert archive_file.stat().st_size == PINNED_FILE_SIZE
    assert hashlib.md5(archive_file.read_bytes()).hexdigest() == PINNED_FILE_MD5, (
        "downloaded bytes must match the pinned upstream checksum"
    )
    first_run = _latest_run(config)
    assert first_run.items_new == 1
    assert first_run.bytes_downloaded > 0
    record = _record(config)
    assert record is not None and record.status == ItemStatus.COMPLETE

    archive_inode = archive_file.stat().st_ino

    # 2. The staged library entry shares inodes with its archive counterpart
    #    (§spec:library-staging).
    assert passthrough_link.is_file()
    assert passthrough_link.stat().st_ino == archive_inode, "staging must be a hardlink"

    # 3. A second sync is a no-op: zero downloads, zero changes (§spec:sync-identity).
    assert run_sync(config) == 0
    second_run = _latest_run(config)
    assert second_run.items_new == 0
    assert second_run.items_updated == 0
    assert second_run.bytes_downloaded == 0, "unchanged item must not be re-downloaded"
    assert archive_file.stat().st_ino == archive_inode, "no-op sync must not re-fetch"

    # 4. Changing library_layout and running restage re-sorts the library tree with
    #    no network traffic — restage reads the archive/DB only, never the source.
    relayout = _config(tmp_path, query=QUERY_MATCHES, layout=CUSTOM_LAYOUT, prune=False)
    assert run_restage(relayout) == 0
    assert custom_link.is_file(), "restage must render the item under the new layout"
    assert not passthrough_link.exists(), "the old layout path must be gone after restage"
    assert custom_link.stat().st_ino == archive_inode, "restaged link is still a hardlink"

    # 5. Narrowing the query so the item leaves enumeration flags it `disappeared`
    #    with local files retained — the retention default is asserted, not assumed
    #    (§spec:item-lifecycle).
    disappear = _config(tmp_path, query=QUERY_EMPTY, layout=CUSTOM_LAYOUT, prune=False)
    assert run_sync(disappear) == 0
    record = _record(disappear)
    assert record is not None and record.status == ItemStatus.DISAPPEARED
    assert archive_file.is_file(), "retention default must keep the archived bytes"
    assert custom_link.is_file(), "retention default must keep the staged link"

    # 6. With prune_disappeared: true, the next sync removes the archive files and
    #    staging links and marks the record pruned (§spec:item-lifecycle).
    prune = _config(tmp_path, query=QUERY_EMPTY, layout=CUSTOM_LAYOUT, prune=True)
    assert run_sync(prune) == 0
    assert not archive_file.exists(), "prune must remove the archived file"
    assert not custom_link.exists(), "prune must remove the staging link"
    record = _record(prune)
    assert record is not None and record.status == ItemStatus.PRUNED
    assert record.archive_path is None

    # 7. `item forget` drops the database record entirely.
    forget_item(prune, PINNED_IDENTIFIER)
    assert _record(prune) is None, "forget must drop the DB record"
