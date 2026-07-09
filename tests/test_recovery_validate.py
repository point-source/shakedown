"""Readiness validation and durable recovery reporting surfaces."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from shakedown.cli import main
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


def test_validate_failure_records_status_warning_and_retry_clears_it(
    tmp_path: Path, tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    archive, library = tmp_roots
    cfg = _write_config(tmp_path, archive, library)

    def unreachable_enumerate(self, collection):
        raise ConnectionError("source unreachable")
        yield  # pragma: no cover

    monkeypatch.setattr(FakePlugin, "enumerate_items", unreachable_enumerate)
    failed = CliRunner().invoke(main, ["--config", str(cfg), "validate"])
    assert failed.exit_code == 1
    assert "fake-src/coll1" in failed.output
    assert "source reachable" in failed.output

    status = CliRunner().invoke(main, ["--config", str(cfg), "status"])
    assert status.exit_code == 0
    assert "RECOVERY: validate failed_before_completion" in status.output
    assert "check source connectivity" in status.output

    monkeypatch.undo()
    passed = CliRunner().invoke(main, ["--config", str(cfg), "validate"])
    assert passed.exit_code == 0
    assert "READY: this deployment is ready to attempt a real sync." in passed.output

    recovered = CliRunner().invoke(main, ["--config", str(cfg), "status"])
    assert recovered.exit_code == 0
    assert "RECOVERY:" not in recovered.output


def test_sync_failure_records_retry_warning_and_retry_clears_it(
    tmp_path: Path, tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    archive, library = tmp_roots
    cfg_path = _write_config(tmp_path, archive, library)
    config = load(cfg_path)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"a")]
    )

    def failing_fetch(self, item, dest_dir, format_filters, exclude_filters):
        raise RuntimeError("simulated mid-run failure")

    monkeypatch.setattr(FakePlugin, "fetch", failing_fetch)
    assert run_sync(config) == 1
    assert not (archive / "fake-src" / "coll1" / "gd-x").exists()

    status = CliRunner().invoke(main, ["--config", str(cfg_path), "status"])
    assert status.exit_code == 0
    assert "RECOVERY: sync completed_with_item_issues" in status.output
    assert "fix failed item issues and rerun sync" in status.output

    monkeypatch.undo()
    assert run_sync(config) == 0
    assert (archive / "fake-src" / "coll1" / "gd-x" / "x.flac").is_file()

    recovered = CliRunner().invoke(main, ["--config", str(cfg_path), "status"])
    assert recovered.exit_code == 0
    assert "RECOVERY:" not in recovered.output


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
