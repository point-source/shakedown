"""Release workflow tests: the gate CI runs before any image is published.

Each test is named for the user workflow it proves, so a failure reads as the
workflow that broke — e.g. ``test_partial_failure_recovery_restage`` — not as an
internal fixture (§spec:release-validation-gate, §req:success-criteria #18).

Synthetic sources only (``FakePlugin``) and throwaway archive, library, config,
and state paths under ``tmp_path``. No network, and no skip path: a scenario that
cannot run fails.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import shakedown.restage as restage_module
from shakedown.cli import main
from shakedown.config import (
    CollectionConfig,
    Config,
    ConfigError,
    ExecHandoff,
    IAAuth,
    SourceConfig,
    load,
)
from shakedown.db import connect
from shakedown.models import ItemStatus, OperationStatus, OperationType
from shakedown.reconcile import run_reconcile
from shakedown.restage import run_restage
from shakedown.state import ItemRepo, OperationOutcomeRepo
from shakedown.status import print_status
from shakedown.sync import run_sync
from shakedown.validate import validate_config
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

# -- helpers --------------------------------------------------------------------


def _collection(
    name: str = "coll1",
    *,
    library_layout: str = "passthrough",
    on_complete=None,
) -> CollectionConfig:
    return CollectionConfig(
        name=name, query="*", library_layout=library_layout, on_complete=on_complete
    )


def _config(
    archive: Path,
    library: Path,
    state_db: Path,
    sources: list[SourceConfig],
) -> Config:
    return Config(
        archive_root=archive, library_root=library, state_db=state_db, sources=sources
    )


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


# -- first-setup readiness: `validate` catches what would break a first sync ----


def test_first_setup_readiness_missing_path(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    archive = blocker / "archive"  # parent is a file → mkdir cannot succeed
    cfg = _config(
        archive,
        library,
        tmp_path / "state.db",
        [SourceConfig(name="fake-src", type="fake", collections=[_collection()])],
    )
    report = validate_config(cfg)
    assert not report.ok
    cfg_group = next(g for g in report.groups if g.source is None)
    archive_check = next(c for c in cfg_group.checks if c.name == "archive path writable")
    assert not archive_check.ok
    assert archive_check.consequence and str(archive) in archive_check.consequence
    assert archive_check.action


def test_first_setup_readiness_bad_credentials(tmp_path: Path, tmp_roots, monkeypatch) -> None:
    archive, library = tmp_roots
    monkeypatch.delenv("MISSING_EMAIL_X", raising=False)
    monkeypatch.delenv("MISSING_PW_X", raising=False)
    src = SourceConfig(
        name="fake-src",
        type="fake",
        auth=IAAuth(email_env="MISSING_EMAIL_X", password_env="MISSING_PW_X"),
        collections=[_collection()],
    )
    report = validate_config(_config(archive, library, tmp_path / "state.db", [src]))
    assert not report.ok
    src_group = next(g for g in report.groups if g.source == "fake-src" and g.collection is None)
    cred = next(c for c in src_group.checks if c.name == "credentials present")
    assert not cred.ok
    assert cred.consequence and "MISSING_EMAIL_X" in cred.consequence


def test_first_setup_readiness_source_access(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    FakePlugin.unreachable_sources.add("fake-src")
    report = validate_config(
        _config(
            archive,
            library,
            tmp_path / "state.db",
            [SourceConfig(name="fake-src", type="fake", collections=[_collection()])],
        )
    )
    assert not report.ok
    coll_group = next(g for g in report.groups if g.collection == "coll1")
    reach = next(c for c in coll_group.checks if c.name == "source reachable")
    assert not reach.ok
    assert reach.consequence and "unreachable" in reach.consequence


def test_first_setup_readiness_unsafe_layout(tmp_path: Path, tmp_roots) -> None:
    # `{title}` is a known fake field but not per-item-unique: two same-title items
    # would collide. The config layer only warns; validate fails.
    archive, library = tmp_roots
    src = SourceConfig(
        name="fake-src",
        type="fake",
        collections=[_collection(library_layout="{title}")],
    )
    report = validate_config(_config(archive, library, tmp_path / "state.db", [src]))
    assert not report.ok
    coll_group = next(g for g in report.groups if g.collection == "coll1")
    layout = next(c for c in coll_group.checks if c.name == "library layout is collision-safe")
    assert not layout.ok
    assert layout.consequence and "collide" in layout.consequence


def test_first_setup_readiness_broken_handoff_target(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    program = str(tmp_path / "missing" / "shakedown-import")
    src = SourceConfig(
        name="fake-src",
        type="fake",
        collections=[_collection(on_complete=ExecHandoff(exec=program))],
    )
    report = validate_config(_config(archive, library, tmp_path / "state.db", [src]))
    assert not report.ok
    coll_group = next(g for g in report.groups if g.collection == "coll1")
    handoff = next(c for c in coll_group.checks if c.name == "handoff target ready")
    assert not handoff.ok
    assert handoff.consequence and program in handoff.consequence
    assert handoff.action


# -- unsafe config rejection ----------------------------------------------------


def test_unsafe_config_rejection_constant_layout(tmp_path: Path) -> None:
    """A template with no source fields renders every item to one path — a guaranteed
    collapse and a hard config error (§spec:layout-collision-safety)."""
    p = tmp_path / "shakedown.yaml"
    p.write_text("""
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
        library_layout: 'all-shows'
""")
    with pytest.raises(ConfigError) as exc_info:
        load(p)
    assert "ia/gd" in str(exc_info.value)
    assert "same path" in str(exc_info.value)


# -- partial-failure recovery: recordings survive; a retry clears the warning ----


def test_partial_failure_recovery_sync(
    tmp_path: Path, tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    archive, library = tmp_roots
    cfg_path = _write_config(tmp_path, archive, library)
    config = load(cfg_path)
    FakePlugin.items["gd-a"] = FakeItem(
        identifier="gd-a", files=[FakeFile(name="a.flac", content=b"mirrored")]
    )
    assert run_sync(config) == 0
    archived_a = archive / "fake-src" / "coll1" / "gd-a" / "a.flac"
    staged_a = library / "fake-src" / "coll1" / "gd-a" / "a.flac"
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"a")]
    )

    def failing_fetch(self, item, dest_dir, format_filters, exclude_filters):
        raise RuntimeError("simulated mid-run failure")

    monkeypatch.setattr(FakePlugin, "fetch", failing_fetch)
    assert run_sync(config) == 1
    assert not (archive / "fake-src" / "coll1" / "gd-x").exists()
    # The recording mirrored before the failure is untouched, in archive and library.
    assert archived_a.read_bytes() == b"mirrored"
    assert staged_a.stat().st_ino == archived_a.stat().st_ino

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


def test_partial_failure_recovery_restage(
    tmp_roots: tuple[Path, Path], monkeypatch, capsys
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x",
        files=[FakeFile(name="x.flac", content=b"audio")],
    )
    assert run_sync(config) == 0
    archived = archive / "fake-src" / "coll1" / "gd-x" / "x.flac"
    staged = library / "fake-src" / "coll1" / "gd-x" / "x.flac"

    def fail_stage(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(restage_module, "stage_item", fail_stage)

    assert run_restage(config) == 1
    assert archived.read_bytes() == b"audio"  # the recording itself is never at risk

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

    monkeypatch.undo()
    assert run_restage(config) == 0
    assert staged.stat().st_ino == archived.stat().st_ino
    assert OperationOutcomeRepo(conn).latest_actionable("fake-src", "coll1") is None
    print_status(config, as_json=False)
    assert "RECOVERY:" not in capsys.readouterr().out


def test_partial_failure_recovery_reconcile(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """If `discover` raises (source down, network out), reconcile still records on-disk
    items rather than refusing to run."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["a"] = FakeItem(
        identifier="a", files=[FakeFile(name="a.flac", content=b"a")]
    )
    assert run_sync(config) == 0
    config.state_db.unlink()  # type: ignore[union-attr]

    def discover_explodes(self, collection):
        raise ConnectionError("upstream unreachable")
        yield  # pragma: no cover  (keeps it a generator)

    monkeypatch.setattr(FakePlugin, "discover", discover_explodes)
    assert run_reconcile(config) == 1
    assert (archive / "fake-src" / "coll1" / "a" / "a.flac").read_bytes() == b"a"

    conn = connect(config.state_db)  # type: ignore[arg-type]
    items = ItemRepo(conn)
    row = items.get("fake-src", "coll1", "a")
    assert row is not None, "on-disk items must be reconciled even when source is unreachable"
    # No prior DB row + no source descriptor → COMPLETE with no manifest;
    # next sync (when source returns) will refresh the manifest.
    assert row.status == ItemStatus.COMPLETE
    assert row.recorded_manifest is None

    recovery = OperationOutcomeRepo(conn).latest_actionable("fake-src", "coll1")
    assert recovery is not None
    assert recovery.operation == OperationType.RECONCILE
    assert recovery.status == OperationStatus.STALE_ENUMERATION
    assert recovery.phase == "fetch-manifests"
    assert recovery.completed_work["items_on_disk"] == 1
    assert recovery.preservation_context == "local recordings retained"
    assert recovery.safe_next_action == "restore source access and rerun reconcile"

    monkeypatch.undo()
    assert run_reconcile(config) == 0
    assert OperationOutcomeRepo(conn).latest_actionable("fake-src", "coll1") is None
    row = items.get("fake-src", "coll1", "a")
    assert row is not None and row.recorded_manifest is not None


def test_partial_failure_recovery_validation(
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


# -- ordinary sync-to-library path ----------------------------------------------


def test_sync_to_library_hardlinked_staging(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd1977-05-08"] = FakeItem(
        identifier="gd1977-05-08",
        files=[FakeFile(name="gd1977-05-08.flac", content=b"hello world")],
        metadata={"title": "Show", "date": "1977-05-08", "year": "1977", "venue": "Cornell"},
    )

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
