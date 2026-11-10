"""Restage recovery after a per-show issue: the warning shows, and a retry clears it."""
from __future__ import annotations

from pathlib import Path

from shakedown.config import load
from shakedown.restage import run_restage
from shakedown.status import print_status
from shakedown.sync import run_sync
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _write_config(tmp_path: Path, archive: Path, library: Path) -> Path:
    cfg = tmp_path / "shakedown.yaml"
    cfg.write_text(f"""
archive_root: {archive}
library_root: {library}
sources:
  - name: fake-src
    type: fake
    collections:
      - name: coll1
        query: '*'
""")
    return cfg


def test_restage_item_issue_records_warning_and_retry_clears_it(
    tmp_roots: tuple[Path, Path], capsys
) -> None:
    """A show restage could not fully stage (a file shakedown did not place sits at one
    of its link targets) is reported in status; once fixed, a rerun clears it."""
    import shutil

    from shakedown.restage import RESTAGE_ISSUES_ACTION

    archive, library = tmp_roots
    config = load(_write_config(archive.parent, archive, library))
    FakePlugin.items["show-a"] = FakeItem(
        identifier="show-a",
        files=[FakeFile(name="track.flac", content=b"a")],
        metadata={"year": "1977"},
    )
    assert run_sync(config) == 0
    collection_dir = library / "fake-src" / "coll1"
    shutil.rmtree(collection_dir)
    foreign = collection_dir / "show-a" / "track.flac"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"not shakedown's")

    assert run_restage(config) == 1
    print_status(config, as_json=False)
    out = capsys.readouterr().out
    assert "RECOVERY: restage completed_with_item_issues" in out
    assert RESTAGE_ISSUES_ACTION in out
    assert foreign.read_bytes() == b"not shakedown's"

    foreign.unlink()
    assert run_restage(config) == 0
    print_status(config, as_json=False)
    out = capsys.readouterr().out
    assert "RECOVERY:" not in out
