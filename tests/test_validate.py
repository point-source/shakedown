"""Setup readiness validation: `shakedown validate` (§spec:setup-readiness-validation)."""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from shakedown import notify
from shakedown.cli import main
from shakedown.config import (
    CollectionConfig,
    Config,
    ExecHandoff,
    IAAuth,
    SourceConfig,
    WebhookHandoff,
)
from shakedown.db import connect
from shakedown.models import OperationStatus, OperationType
from shakedown.state import OperationOutcomeRepo
from shakedown.validate import run_validate, validate_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


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


def _valid_config(tmp_path: Path, tmp_roots: tuple[Path, Path]) -> Config:
    archive, library = tmp_roots
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"audio")]
    )
    return _config(
        archive,
        library,
        tmp_path / "state.db",
        [SourceConfig(name="fake-src", type="fake", collections=[_collection()])],
    )


# -- happy path -----------------------------------------------------------------


def test_valid_config_passes(tmp_path: Path, tmp_roots) -> None:
    report = validate_config(_valid_config(tmp_path, tmp_roots))
    assert report.ok
    # Pass means "ready to attempt a sync", not "mirrored": no item was downloaded.
    assert "gd-x" not in FakePlugin.fetch_count


def test_pass_leaves_no_archive_or_library_items(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    validate_config(_valid_config(tmp_path, tmp_roots))
    # The write-probe is always removed; no collection item is staged or downloaded.
    assert list(archive.iterdir()) == []
    assert list(library.iterdir()) == []


# -- individual failing probes --------------------------------------------------


def test_missing_archive_path_fails(tmp_path: Path) -> None:
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


def test_missing_credential_env_var_fails(tmp_path: Path, tmp_roots, monkeypatch) -> None:
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


def test_unreachable_source_fails(tmp_path: Path, tmp_roots) -> None:
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


def test_unknown_source_plugin_reports_failure_without_crashing(
    tmp_path: Path, tmp_roots
) -> None:
    archive, library = tmp_roots
    src = SourceConfig(
        name="missing-src",
        type="missing-plugin",
        collections=[_collection(library_layout="{title}")],
    )

    report = validate_config(_config(archive, library, tmp_path / "state.db", [src]))

    assert not report.ok
    src_group = next(
        g for g in report.groups if g.source == "missing-src" and g.collection is None
    )
    plugin_check = next(c for c in src_group.checks if c.name == "source plugin loads")
    assert not plugin_check.ok
    assert plugin_check.consequence and "missing-plugin" in plugin_check.consequence
    coll_group = next(g for g in report.groups if g.collection == "coll1")
    assert not any(c.name == "library layout is collision-safe" for c in coll_group.checks)


def test_unsafe_layout_template_fails(tmp_path: Path, tmp_roots) -> None:
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


# -- combined acceptance scenario (matches the batch verify criteria) ------------


def test_combined_failures_named_and_no_items(tmp_path: Path, monkeypatch) -> None:
    library = tmp_path / "library"
    library.mkdir()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    archive = blocker / "archive"  # missing/unusable archive path
    monkeypatch.delenv("NOPE_EMAIL", raising=False)
    FakePlugin.unreachable_sources.add("s-bad")
    FakePlugin.items["ok-1"] = FakeItem(
        identifier="ok-1", files=[FakeFile(name="a.flac", content=b"a")]
    )
    sources = [
        SourceConfig(
            name="s-bad",
            type="fake",
            auth=IAAuth(email_env="NOPE_EMAIL"),
            collections=[_collection("bad-coll", library_layout="{title}")],
        ),
        SourceConfig(name="s-good", type="fake", collections=[_collection("good-coll")]),
    ]
    exit_code = run_validate(_write_yaml(tmp_path, archive, library, sources))

    assert exit_code != 0  # non-zero on any failure

    # Nothing was downloaded/staged into the (writable) library.
    assert list(library.iterdir()) == []
    assert FakePlugin.fetch_count == {}


def test_fix_then_rerun_passes(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    FakePlugin.unreachable_sources.add("fake-src")
    cfg_path = _write_yaml(
        tmp_path,
        archive,
        library,
        [SourceConfig(name="fake-src", type="fake", collections=[_collection()])],
    )
    assert run_validate(cfg_path) != 0
    # "Fix" the source and rerun.
    FakePlugin.unreachable_sources.clear()
    assert run_validate(cfg_path) == 0


# -- handoff readiness ----------------------------------------------------------


def test_default_handoff_does_not_mutate(tmp_path: Path, tmp_roots, monkeypatch) -> None:
    archive, library = tmp_roots
    posted: list = []
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: posted.append((a, k)))
    src = SourceConfig(
        name="fake-src",
        type="fake",
        collections=[_collection(on_complete=WebhookHandoff(webhook="https://hooks.test/x"))],
    )
    report = validate_config(_config(archive, library, tmp_path / "state.db", [src]))
    # Webhook parses cleanly → the handoff check passes, but nothing was sent.
    coll_group = next(g for g in report.groups if g.collection == "coll1")
    handoff = next(c for c in coll_group.checks if c.name == "handoff target ready")
    assert handoff.ok
    assert posted == []  # no production notification triggered by a default check


def test_live_handoff_sends_marked_payload(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    out = tmp_path / "handoff.json"
    # `tee <out>` reads the marked payload from stdin and writes it to disk.
    src = SourceConfig(
        name="fake-src",
        type="fake",
        collections=[_collection(on_complete=ExecHandoff(exec=f"tee {out}"))],
    )
    report = validate_config(
        _config(archive, library, tmp_path / "state.db", [src]), live_handoff=True
    )
    coll_group = next(g for g in report.groups if g.collection == "coll1")
    live = next(c for c in coll_group.checks if c.name == "live handoff test")
    assert live.ok
    body = json.loads(out.read_text())
    assert body["event"] == notify.VALIDATION_EVENT
    assert body["test"] is True


# -- JSON output ----------------------------------------------------------------


def test_json_mirrors_structure(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    cfg_path = _write_yaml(
        tmp_path,
        archive,
        library,
        [SourceConfig(name="fake-src", type="fake", collections=[_collection()])],
    )
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"a")]
    )
    result = CliRunner().invoke(main, ["--config", str(cfg_path), "validate", "--json"])
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["ready"] is True
    assert any(g["source"] == "fake-src" and g["collection"] == "coll1" for g in doc["groups"])
    for g in doc["groups"]:
        for c in g["checks"]:
            assert set(c) == {"name", "ok", "target", "consequence", "action"}


# -- recovery integration (§spec:recoverable-operation-reporting) ---------------


def test_failed_validation_recorded_and_cleared(tmp_path: Path, tmp_roots) -> None:
    archive, library = tmp_roots
    state_db = tmp_path / "state.db"
    FakePlugin.unreachable_sources.add("fake-src")
    cfg = _config(
        archive,
        library,
        state_db,
        [SourceConfig(name="fake-src", type="fake", collections=[_collection()])],
    )
    validate_config(cfg)

    outcomes = OperationOutcomeRepo(connect(state_db))
    rec = outcomes.latest_actionable("fake-src", "coll1")
    assert rec is not None
    assert rec.operation is OperationType.VALIDATE
    assert rec.status is OperationStatus.FAILED_BEFORE_COMPLETION
    assert rec.safe_next_action

    # Fix and rerun: the actionable validation warning clears.
    FakePlugin.unreachable_sources.clear()
    validate_config(cfg)
    assert OperationOutcomeRepo(connect(state_db)).latest_actionable("fake-src", "coll1") is None


# -- helpers --------------------------------------------------------------------


def _write_yaml(
    tmp_path: Path, archive: Path, library: Path, sources: list[SourceConfig]
) -> Path:
    lines = [f"archive_root: {archive}", f"library_root: {library}",
             f"state_db: {tmp_path / 'state.db'}", "sources:"]
    for s in sources:
        lines.append(f"  - name: {s.name}")
        lines.append(f"    type: {s.type}")
        if s.auth:
            lines.append("    auth:")
            if s.auth.email_env:
                lines.append(f"      email_env: {s.auth.email_env}")
            if s.auth.password_env:
                lines.append(f"      password_env: {s.auth.password_env}")
        lines.append("    collections:")
        for c in s.collections:
            lines.append(f"      - name: {c.name}")
            lines.append("        query: '*'")
            lines.append(f"        library_layout: '{c.library_layout}'")
            if isinstance(c.on_complete, WebhookHandoff):
                lines.append(f"        on_complete: {{webhook: '{c.on_complete.webhook}'}}")
            elif isinstance(c.on_complete, ExecHandoff):
                lines.append(f"        on_complete: {{exec: '{c.on_complete.exec}'}}")
    path = tmp_path / "shakedown.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path
