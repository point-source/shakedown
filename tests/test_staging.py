"""Template rendering, sanitization, hardlink staging, and restage."""
from __future__ import annotations

from pathlib import Path

import pytest

from shakedown.restage import run_restage
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


def test_template_render_unknown_field_raises() -> None:
    with pytest.raises(TemplateError):
        render("{nope}", {})


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
