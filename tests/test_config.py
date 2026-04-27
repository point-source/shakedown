from __future__ import annotations

from pathlib import Path

import pytest

from shakedown.config import Config, ConfigError, load


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "shakedown.yaml"
    p.write_text(body)
    return p


def test_loads_minimal_config(tmp_path: Path) -> None:
    p = _write(tmp_path, """
archive_root: /data/archive
library_root: /data/library
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'collection:GratefulDead'
""")
    cfg = load(p)
    assert isinstance(cfg, Config)
    assert cfg.state_db == Path("/data/archive/.shakedown/state.db"), "state_db default"
    assert cfg.max_concurrent_downloads == 4
    assert cfg.sources[0].collections[0].library_layout == "passthrough"


def test_duplicate_collection_names_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: src1
    type: ia
    collections:
      - name: dup
        query: 'q1'
      - name: dup
        query: 'q2'
""")
    with pytest.raises(ConfigError):
        load(p)


def test_extra_keys_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: src
    type: ia
    bogus_key: oops
    collections:
      - name: c
        query: q
""")
    with pytest.raises(ConfigError):
        load(p)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load(tmp_path / "nope.yaml")


def test_layout_referencing_unknown_field_rejected(tmp_path: Path) -> None:
    """First-run footgun: example-style layouts that reference {filename} (not a field
    the IA plugin surfaces) must be caught at load time, not mid-sync."""
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
        library_layout: '{year}/{date} - {venue|sanitize}/{filename}'
""")
    with pytest.raises(ConfigError) as exc_info:
        load(p)
    assert "filename" in str(exc_info.value)


def test_layout_with_known_fields_accepted(tmp_path: Path) -> None:
    """The shipped example layout must load cleanly."""
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
        library_layout: '{year}/{date} - {venue|sanitize}'
""")
    cfg = load(p)
    assert cfg.sources[0].collections[0].library_layout == "{year}/{date} - {venue|sanitize}"


def test_max_concurrent_collections_field_removed(tmp_path: Path) -> None:
    """Setting the dropped knob in YAML is now a config error (extra=forbid)."""
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
max_concurrent_collections: 4
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
""")
    with pytest.raises(ConfigError):
        load(p)
