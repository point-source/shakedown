"""CLI verb wiring: argument parsing, exit codes, error paths."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from shakedown.cli import main
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


def test_help_exits_zero() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "sync" in result.output and "verify" in result.output


def test_missing_config_exits_two(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["--config", str(tmp_path / "missing.yaml"), "status"])
    assert result.exit_code == 2
    assert "config file not found" in result.output


def test_verify_reconform_without_deep_exits_two(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    cfg = _write_config(tmp_path, archive, library)
    result = CliRunner().invoke(main, ["--config", str(cfg), "verify", "--reconform"])
    assert result.exit_code == 2
    assert "--reconform requires --deep" in result.output


def test_status_against_empty_db(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    cfg = _write_config(tmp_path, archive, library)
    result = CliRunner().invoke(main, ["--config", str(cfg), "status"])
    assert result.exit_code == 0
    assert "fake-src/coll1" in result.output
    assert "last run: never" in result.output


def test_sync_dry_run_succeeds(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    cfg = _write_config(tmp_path, archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"a")]
    )
    result = CliRunner().invoke(main, ["--config", str(cfg), "sync", "--dry-run"])
    assert result.exit_code == 0
    assert FakePlugin.fetch_count == {}, "dry-run must not fetch"


def test_sync_refresh_metadata_and_dry_run_mutually_exclusive(
    tmp_path: Path, tmp_roots
) -> None:
    archive, library = tmp_roots
    cfg = _write_config(tmp_path, archive, library)
    result = CliRunner().invoke(
        main, ["--config", str(cfg), "sync", "--dry-run", "--refresh-metadata"]
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_sync_refresh_metadata_rewrites_sidecar_without_refetch(
    tmp_path: Path, tmp_roots
) -> None:
    archive, library = tmp_roots
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
        preserve_source_metadata: true
""")
    FakePlugin.items["gd-r"] = FakeItem(
        identifier="gd-r", files=[FakeFile(name="r.flac", content=b"a")], metadata={"notes": "v1"}
    )
    assert CliRunner().invoke(main, ["--config", str(cfg), "sync"]).exit_code == 0
    assert FakePlugin.fetch_count == {"gd-r": 1}

    FakePlugin.items["gd-r"].metadata = {"notes": "v2"}
    result = CliRunner().invoke(main, ["--config", str(cfg), "sync", "--refresh-metadata"])
    assert result.exit_code == 0
    assert FakePlugin.fetch_count == {"gd-r": 1}  # no media re-download
    import json
    sidecar = archive / "fake-src" / "coll1" / "gd-r" / "metadata.json"
    assert json.loads(sidecar.read_text())["notes"] == "v2"


def test_sync_filters_apply(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    cfg = _write_config(tmp_path, archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"a")]
    )
    result = CliRunner().invoke(
        main, ["--config", str(cfg), "sync", "--source", "nonexistent"]
    )
    assert result.exit_code == 0
    assert FakePlugin.fetch_count == {}, "filter must skip all sources"


def test_item_show_unknown_identifier(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    cfg = _write_config(tmp_path, archive, library)
    result = CliRunner().invoke(
        main, ["--config", str(cfg), "item", "show", "no-such-item"]
    )
    assert result.exit_code == 0  # not found is reported, not a failure
    assert "not found" in result.output


def test_status_reports_pruned_takedown(tmp_path: Path, tmp_roots) -> None:
    """§spec:item-lifecycle surface: after a pruned takedown, `shakedown status`
    still reports the item — its record is retained even though the files are gone."""
    archive, library = tmp_roots
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
        prune_disappeared: true
""")
    from shakedown.config import load
    config = load(cfg)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"a")]
    )
    assert run_sync(config) == 0

    FakePlugin.items.clear()  # source takes the item down
    assert run_sync(config) == 0

    result = CliRunner().invoke(main, ["--config", str(cfg), "status"])
    assert result.exit_code == 0
    assert "pruned=1" in result.output, "status must still report the pruned takedown"


def test_status_reports_stale_collection(tmp_path: Path, tmp_roots, monkeypatch) -> None:
    """§spec:failure-behavior surface: after a source-enumeration failure, the
    `shakedown status` CLI marks the collection stale while its items remain."""
    archive, library = tmp_roots
    cfg = _write_config(tmp_path, archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"a")]
    )
    from shakedown.config import load
    config = load(cfg)
    assert run_sync(config) == 0

    def unreachable_enumerate(self, collection):
        raise ConnectionError("source unreachable")
        yield  # pragma: no cover

    monkeypatch.setattr(FakePlugin, "enumerate_items", unreachable_enumerate)
    assert run_sync(config) == 1

    result = CliRunner().invoke(main, ["--config", str(cfg), "status"])
    assert result.exit_code == 0
    assert "RECOVERY: sync stale_enumeration during discover" in result.output
    assert "action: restore source access and rerun sync" in result.output
    assert "STALE" in result.output
    assert "complete=1" in result.output, "existing items remain reported"

    json_result = CliRunner().invoke(main, ["--config", str(cfg), "status", "--json"])
    assert '"stale": true' in json_result.output
    assert '"operation": "sync"' in json_result.output
    assert '"status": "stale_enumeration"' in json_result.output
    assert '"safe_next_action": "restore source access and rerun sync"' in json_result.output


def test_item_forget_removes_row(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    cfg = _write_config(tmp_path, archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"a")]
    )
    # Seed via library function (CliRunner needs a writable config-derived state)
    from shakedown.config import load
    config = load(cfg)
    assert run_sync(config) == 0

    result = CliRunner().invoke(main, ["--config", str(cfg), "item", "forget", "gd-x"])
    assert result.exit_code == 0

    from shakedown.db import connect
    from shakedown.state import ItemRepo
    conn = connect(config.state_db)
    assert ItemRepo(conn).get("fake-src", "coll1", "gd-x") is None
