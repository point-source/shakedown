"""Template rendering, sanitization, hardlink staging, and restage."""
from __future__ import annotations

from pathlib import Path

import pytest

import shakedown.restage as restage_module
from shakedown.db import connect
from shakedown.models import OperationStatus, OperationType
from shakedown.restage import run_restage
from shakedown.state import OperationOutcomeRepo
from shakedown.status import print_status
from shakedown.sync import run_sync
from shakedown.utils.sanitize import sanitize
from shakedown.utils.templates import TemplateError, render
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def test_sanitize_replaces_filesystem_hostile_chars() -> None:
    assert sanitize("Cornell University: Barton Hall") == "Cornell University_ Barton Hall"
    assert sanitize("a/b\\c") == "a_b_c"
    assert sanitize("foo  ") == "foo"
    assert sanitize("") == "_"
    assert sanitize("Café Wha?") == "Café Wha_"  # non-ASCII preserved


def test_template_render_substitutes_and_filters() -> None:
    fields = {"year": "1977", "date": "1977-05-08", "venue": "Barton Hall: Cornell"}
    out = render("{year}/{date} - {venue|sanitize}", fields)
    assert out == "1977/1977-05-08 - Barton Hall_ Cornell"


def test_template_render_missing_field_renders_unknown() -> None:
    # A field the item's metadata lacks renders as a literal `unknown` segment
    # rather than raising (§spec:library-staging). Collision detection catches
    # any resulting conflicts.
    assert render("{year}/{venue|sanitize}", {"year": "1977"}) == "1977/unknown"


def test_template_render_null_field_renders_unknown() -> None:
    # An explicitly-null value is treated the same as a missing field.
    assert render("{venue}", {"venue": None}) == "unknown"


def test_template_render_unknown_filter_raises() -> None:
    # A bogus filter name is an author error and still raises.
    with pytest.raises(TemplateError):
        render("{venue|bogus}", {"venue": "Barton Hall"})


def test_custom_layout_creates_human_readable_tree(tmp_roots: tuple[Path, Path]) -> None:
    """PRD §8: template renders directories; filenames inside match the archive."""
    archive, library = tmp_roots
    config = make_config(
        archive,
        library,
        library_layout="{year}/{date} - {venue|sanitize}",
    )
    FakePlugin.items["gd1977-05-08"] = FakeItem(
        identifier="gd1977-05-08",
        files=[FakeFile(name="set1-track01.flac", content=b"audio")],
        metadata={"year": "1977", "date": "1977-05-08", "venue": "Barton Hall: Cornell"},
    )

    assert run_sync(config) == 0
    expected = (
        library / "fake-src" / "coll1" / "1977" / "1977-05-08 - Barton Hall_ Cornell" / "set1-track01.flac"
    )
    assert expected.is_file()


def test_missing_layout_field_stages_under_unknown(tmp_roots: tuple[Path, Path]) -> None:
    """§spec:library-staging: an item lacking a layout field stages under `unknown`."""
    archive, library = tmp_roots
    config = make_config(
        archive,
        library,
        library_layout="{year}/{venue|sanitize}",
    )
    FakePlugin.items["gd1977-05-08"] = FakeItem(
        identifier="gd1977-05-08",
        files=[FakeFile(name="set1-track01.flac", content=b"audio")],
        metadata={"year": "1977"},  # no venue
    )

    assert run_sync(config) == 0
    expected = (
        library / "fake-src" / "coll1" / "1977" / "unknown" / "set1-track01.flac"
    )
    assert expected.is_file()

    # restage rebuilds the same tree from the archive without any fetches.
    import shutil
    shutil.rmtree(library / "fake-src")
    assert run_restage(config) == 0
    assert expected.is_file()
    assert FakePlugin.fetch_count == {"gd1977-05-08": 1}


def test_restage_rebuilds_after_library_wipe(tmp_roots: tuple[Path, Path]) -> None:
    """PRD §16.3: wiping library tree and running restage reconstructs it without downloads."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    for n in range(5):
        FakePlugin.items[f"gd-{n}"] = FakeItem(
            identifier=f"gd-{n}",
            files=[FakeFile(name=f"gd-{n}.flac", content=f"audio-{n}".encode())],
            metadata={"year": "1977", "date": "1977-05-08"},
        )

    assert run_sync(config) == 0
    assert FakePlugin.fetch_count == {f"gd-{n}": 1 for n in range(5)}

    # User wipes library and re-points layout — we use passthrough, just wipe.
    import shutil
    shutil.rmtree(library / "fake-src")
    assert not (library / "fake-src").exists()

    assert run_restage(config) == 0

    # All five staging files re-materialized as hardlinks.
    for n in range(5):
        staged = library / "fake-src" / "coll1" / f"gd-{n}" / f"gd-{n}.flac"
        archived = archive / "fake-src" / "coll1" / f"gd-{n}" / f"gd-{n}.flac"
        assert staged.is_file()
        assert staged.stat().st_ino == archived.stat().st_ino

    # And restage didn't trigger any new fetches.
    assert FakePlugin.fetch_count == {f"gd-{n}": 1 for n in range(5)}


def test_restage_records_recovery_on_storage_failure(
    tmp_roots: tuple[Path, Path], monkeypatch, capsys
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x",
        files=[FakeFile(name="x.flac", content=b"audio")],
    )
    assert run_sync(config) == 0

    def fail_stage(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(restage_module, "stage_item", fail_stage)

    assert run_restage(config) == 1

    conn = connect(config.state_db)  # type: ignore[arg-type]
    recovery = OperationOutcomeRepo(conn).latest_actionable("fake-src", "coll1")
    assert recovery is not None
    assert recovery.operation == OperationType.RESTAGE
    assert recovery.status == OperationStatus.FAILED_BEFORE_COMPLETION
    assert recovery.phase == "stage"
    assert recovery.affected_item == "gd-x"
    assert recovery.completed_work["items_staged"] == 0
    assert recovery.safe_next_action == "fix storage/path/layout and rerun restage"

    print_status(config, as_json=True)
    status_json = capsys.readouterr().out
    assert '"recovery": {' in status_json
    assert '"operation": "restage"' in status_json
    assert '"status": "failed_before_completion"' in status_json
    assert '"affected_item": "gd-x"' in status_json
    assert '"safe_next_action": "fix storage/path/layout and rerun restage"' in status_json


def test_passthrough_layout_mirrors_archive(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, library_layout="passthrough")
    FakePlugin.items["x"] = FakeItem(
        identifier="x",
        files=[FakeFile(name="a.flac", content=b"a"), FakeFile(name="b.flac", content=b"b")],
    )
    assert run_sync(config) == 0
    for name in ("a.flac", "b.flac"):
        a = archive / "fake-src" / "coll1" / "x" / name
        b = library / "fake-src" / "coll1" / "x" / name
        assert a.is_file() and b.is_file()
        assert a.stat().st_ino == b.stat().st_ino
