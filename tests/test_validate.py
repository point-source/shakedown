"""Setup readiness validation scenarios."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from shakedown.cli import main


def test_validate_rejects_unsafe_layout_before_sync(
    tmp_path: Path, tmp_roots: tuple[Path, Path]
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
        library_layout: constant-folder
""")

    result = CliRunner().invoke(main, ["--config", str(cfg), "validate"])

    assert result.exit_code == 2
    assert "library_layout" in result.output
    assert "renders to the same path" in result.output


def test_validate_reports_handoff_failure(
    tmp_path: Path, tmp_roots: tuple[Path, Path]
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
        on_complete:
          exec: /definitely/missing/shakedown-import
""")

    result = CliRunner().invoke(main, ["--config", str(cfg), "validate"])

    assert result.exit_code == 1
    assert "FAIL fake-src/coll1 setup readiness" in result.output
    assert "handoff" in result.output
    assert "/definitely/missing/shakedown-import" in result.output


def test_validate_reports_broken_setup_path_without_traceback(tmp_path: Path) -> None:
    cfg = tmp_path / "shakedown.yaml"
    cfg.write_text(f"""
archive_root: /dev/null/archive
library_root: {tmp_path / "library"}
sources:
  - name: ia-src
    type: ia
    collections:
      - name: bad-path
        query: "identifier:testmp3testfile"
""")

    result = CliRunner().invoke(main, ["--config", str(cfg), "validate"])

    assert result.exit_code == 1
    assert "FAIL ia-src/bad-path setup readiness" in result.output
    assert "archive path" in result.output
    assert "Traceback" not in result.output
