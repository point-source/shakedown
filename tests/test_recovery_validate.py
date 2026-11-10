"""Readiness validation and durable recovery reporting surfaces."""
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


def test_restage_collision_records_warning_and_retry_clears_it(
    tmp_roots: tuple[Path, Path], capsys
) -> None:
    archive, library = tmp_roots
    config = load(_write_config(archive.parent, archive, library))
    FakePlugin.items["show-a"] = FakeItem(
        identifier="show-a",
        files=[FakeFile(name="track.flac", content=b"a")],
        metadata={"year": "1977"},
    )
    FakePlugin.items["show-b"] = FakeItem(
        identifier="show-b",
        files=[FakeFile(name="track.flac", content=b"b")],
        metadata={"year": "1977"},
    )
    assert run_sync(config) == 0

    colliding = config.model_copy(deep=True)
    colliding.sources[0].collections[0].library_layout = "{year}"
    assert run_restage(colliding) == 1
    print_status(colliding, as_json=False)
    out = capsys.readouterr().out
    assert "RECOVERY: restage completed_with_item_issues" in out
    assert "fix library layout and rerun restage" in out

    healthy = config.model_copy(deep=True)
    healthy.sources[0].collections[0].library_layout = "{identifier}"
    assert run_restage(healthy) == 0
    print_status(healthy, as_json=False)
    out = capsys.readouterr().out
    assert "RECOVERY:" not in out
