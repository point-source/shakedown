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
    # preserve_source_metadata is opt-in and defaults off (§spec:metadata-preservation).
    assert cfg.sources[0].collections[0].preserve_source_metadata is False
    # fill_tags is opt-in and defaults off (§spec:tag-filling).
    assert cfg.sources[0].collections[0].fill_tags is False


def test_fill_tags_opt_in(tmp_path: Path) -> None:
    p = _write(tmp_path, """
archive_root: /data/archive
library_root: /data/library
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'collection:GratefulDead'
        fill_tags: true
""")
    cfg = load(p)
    assert cfg.sources[0].collections[0].fill_tags is True


def test_example_config_documents_fill_tags_off() -> None:
    """The annotated example shows the opt-in, switched off (§spec:configuration)."""
    example = Path(__file__).resolve().parent.parent / "shakedown.example.yaml"
    cfg = load(example)
    collections = [c for s in cfg.sources for c in s.collections]
    assert "fill_tags" in example.read_text()
    assert all(c.fill_tags is False for c in collections)


def test_preserve_source_metadata_opt_in(tmp_path: Path) -> None:
    p = _write(tmp_path, """
archive_root: /data/archive
library_root: /data/library
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'collection:GratefulDead'
        preserve_source_metadata: true
""")
    cfg = load(p)
    assert cfg.sources[0].collections[0].preserve_source_metadata is True


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


def test_layout_with_unknown_filter_rejected(tmp_path: Path) -> None:
    """A misspelled filter must fail at load, before restage clears the library."""
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
        library_layout: '{year}/{venue|sanitise}/{identifier}'
""")
    with pytest.raises(ConfigError) as exc_info:
        load(p)
    message = str(exc_info.value)
    assert "sanitise" in message
    assert "lower, sanitize, short, upper" in message


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


def test_layout_without_unique_field_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-passthrough layout keying only on shared fields (no per-item-unique
    component) loads but emits a named startup warning (§spec:layout-collision-safety)."""
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
        library_layout: '{year}/{venue}'
""")
    with caplog.at_level("WARNING"):
        cfg = load(p)
    assert cfg.sources[0].collections[0].library_layout == "{year}/{venue}"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("ia/gd" in m and "per-item-unique" in m for m in warnings), warnings
    assert any(
        "two items sharing these fields get folders labelled with their identifier — add a "
        "unique component such as {identifier} to the leaf" in m
        for m in warnings
    ), warnings


def test_layout_with_unique_field_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A layout that includes the per-item-unique {identifier} is silent."""
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
        library_layout: '{year}/{venue}/{identifier}'
""")
    with caplog.at_level("WARNING"):
        load(p)
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_max_concurrent_collections_defaults(tmp_path: Path) -> None:
    """The global collection-concurrency cap is restored and defaults to bounded parallel."""
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
""")
    cfg = load(p)
    assert cfg.max_concurrent_collections == 2


def test_max_concurrent_collections_honored_from_yaml(tmp_path: Path) -> None:
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
max_concurrent_collections: 5
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
""")
    cfg = load(p)
    assert cfg.max_concurrent_collections == 5


def test_max_concurrent_requests_defaults_to_none(tmp_path: Path) -> None:
    """Unset, the per-source budget falls back to max_concurrent_downloads
    (SPEC §spec:source-budget), so the field defaults to None."""
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
""")
    cfg = load(p)
    assert cfg.sources[0].max_concurrent_requests is None


def test_max_concurrent_requests_honored_from_yaml(tmp_path: Path) -> None:
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    max_concurrent_requests: 3
    collections:
      - name: gd
        query: 'q'
""")
    cfg = load(p)
    assert cfg.sources[0].max_concurrent_requests == 3


def test_max_concurrent_requests_must_be_positive(tmp_path: Path) -> None:
    """A zero/negative per-source budget is a config error (ge=1)."""
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    max_concurrent_requests: 0
    collections:
      - name: gd
        query: 'q'
""")
    with pytest.raises(ConfigError):
        load(p)


def test_max_concurrent_collections_must_be_positive(tmp_path: Path) -> None:
    """A zero/negative cap is a config error (ge=1), named at startup."""
    p = _write(tmp_path, """
archive_root: /a
library_root: /l
max_concurrent_collections: 0
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'q'
""")
    with pytest.raises(ConfigError):
        load(p)


@pytest.mark.parametrize(
    "key, value",
    [
        ("format_filters", "[flac, shn]"),
        ("exclude_filters", "['*_64kb.mp3']"),
        ("fallback_format_filters", "[mp3]"),
    ],
)
def test_removed_format_settings_fail_to_load_naming_the_key(
    tmp_path: Path, key: str, value: str
) -> None:
    """File choice follows the quality rule (§spec:quality-selection); a config that
    still sets a removed format setting fails to load, naming it, so the operator
    deletes it once on upgrade (§spec:configuration)."""
    p = _write(tmp_path, f"""
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    collections:
      - name: gd
        query: 'collection:GratefulDead'
        {key}: {value}
""")
    with pytest.raises(ConfigError, match=key):
        load(p)
