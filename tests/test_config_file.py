"""Writing the config file (§spec:dashboard-settings "Hand edits survive"): a save
changes only the settings that changed, keeps comments, key order, quoting, and
blank lines, replaces the file atomically, and keeps its permissions."""
from __future__ import annotations

import copy
import difflib
import os
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from shakedown.config import ConfigRejected, check_document, read_config_file
from shakedown.config_file import (
    ConfigExists,
    WriteFailed,
    atomic_replace,
    create_config,
    merge,
)
from shakedown.edit_lock import read_edit

EXAMPLE = Path(__file__).resolve().parents[1] / "shakedown.example.yaml"


def _effective(text: str) -> dict[str, Any]:
    checked = check_document(yaml.safe_load(text))
    assert checked.config is not None, checked.errors
    return checked.config.model_dump(mode="json")


def _changed_lines(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(before.splitlines(True), after.splitlines(True))
        if line[:1] in "+-" and line[:3] not in ("+++", "---")
    ]


def _save(text: str, edit) -> str:  # type: ignore[no-untyped-def]
    old = _effective(text)
    new = copy.deepcopy(old)
    edit(new)
    merged = merge(text, old, new)
    assert _effective(merged) == new
    return merged


def test_an_unchanged_document_comes_back_byte_identical() -> None:
    text = EXAMPLE.read_text()
    assert merge(text, _effective(text), _effective(text)) == text


def test_one_changed_setting_changes_one_line() -> None:
    text = EXAMPLE.read_text()

    def edit(doc: dict[str, Any]) -> None:
        doc["sources"][0]["collections"][0]["library_layout"] = "{year}/{identifier}"

    merged = _save(text, edit)
    assert _changed_lines(text, merged) == [
        "-        library_layout: '{year}/{date} - {venue}/{identifier}'\n",
        "+        library_layout: '{year}/{identifier}'\n",
    ]


def test_an_added_setting_lands_in_its_collection_before_the_comments_after_it() -> None:
    text = EXAMPLE.read_text()

    def edit(doc: dict[str, Any]) -> None:
        doc["sources"][0]["collections"][1]["fill_tags"] = True

    merged = _save(text, edit)
    assert _changed_lines(text, merged) == ["+        fill_tags: true\n"]


def test_a_quoted_value_keeps_its_quotes() -> None:
    text = EXAMPLE.read_text()

    def edit(doc: dict[str, Any]) -> None:
        doc["sources"][0]["collections"][0]["query"] = "collection:GratefulDead AND year:1977"
        doc["sources"][0]["collections"][0]["schedule"] = "30 2 * * *"

    merged = _save(text, edit)
    assert _changed_lines(text, merged) == [
        "-        query: 'collection:GratefulDead'\n",
        "+        query: 'collection:GratefulDead AND year:1977'\n",
        "-        schedule: '0 3 * * 0'\n",
        "+        schedule: '30 2 * * *'\n",
    ]


def test_a_double_quoted_value_keeps_double_quotes() -> None:
    text = 'archive_root: "/a"\nlibrary_root: /l\nsources: []\n'
    merged = _save(text, lambda doc: doc.update(archive_root="/b"))
    assert merged.startswith('archive_root: "/b"\n')


def test_a_default_left_out_stays_out_and_a_change_is_added() -> None:
    text = "archive_root: /a\nlibrary_root: /l\nstate_db: /db/s.db\nsources: []\n"
    merged = _save(text, lambda doc: doc.update(max_concurrent_downloads=8))
    assert merged == (
        "archive_root: /a\nlibrary_root: /l\nstate_db: /db/s.db\nsources: []\n"
        "max_concurrent_downloads: 8\n"
    )


def test_an_unset_option_removes_its_key() -> None:
    text = (
        "archive_root: /a\nlibrary_root: /l\nstate_db: /db/s.db\n"
        "timezone: Europe/Berlin  # the clock\nsources: []\n"
    )
    merged = _save(text, lambda doc: doc.update(timezone=None))
    assert "timezone" not in merged


def test_moving_the_archive_keeps_the_state_database_where_it_was() -> None:
    """With no `state_db` in the file, the database follows `archive_root`; a save
    that moves the archive but not the database writes the database down."""
    text = "archive_root: /a\nlibrary_root: /l\nsources: []\n"
    merged = _save(text, lambda doc: doc.update(archive_root="/b"))
    assert _effective(merged)["state_db"] == "/a/.shakedown/state.db"
    assert "state_db: /a/.shakedown/state.db" in merged


def test_collections_are_added_removed_and_reordered_by_name() -> None:
    text = EXAMPLE.read_text()

    def edit(doc: dict[str, Any]) -> None:
        colls = doc["sources"][0]["collections"]
        added = copy.deepcopy(colls[1])
        added.update(name="dead-and-co", query="collection:DeadAndCompany", schedule=None)
        added["library_layout"] = "passthrough"
        doc["sources"][0]["collections"] = [colls[1], added]  # grateful-dead removed

    merged = _save(text, edit)
    names = [c["name"] for c in yaml.safe_load(merged)["sources"][0]["collections"]]
    assert names == ["phish", "dead-and-co"]
    added = yaml.safe_load(merged)["sources"][0]["collections"][1]
    # A new collection is written with what differs from the defaults only.
    assert added == {"name": "dead-and-co", "query": "collection:DeadAndCompany"}
    # The other source and its comments are untouched.
    assert "# The etree Live Music Archive." in merged
    assert "        # Opt-in fast-path for recurring no-op syncs" in merged


def test_a_new_source_is_written() -> None:
    text = "archive_root: /a\nlibrary_root: /l\nstate_db: /s.db\nsources: []\n"

    def edit(doc: dict[str, Any]) -> None:
        doc["sources"].append({
            "name": "lma", "type": "etree", "auth": None, "max_concurrent_requests": None,
            "collections": [{
                "name": "lma", "query": "collection:etree", "library_layout": "passthrough",
                "prune_disappeared": False, "incremental_discovery": False,
                "preserve_source_metadata": False, "fill_tags": False,
                "on_complete": {"webhook": "http://hook"}, "schedule": "0 3 * * 0",
            }],
        })

    merged = _save(text, edit)
    assert yaml.safe_load(merged)["sources"] == [{
        "name": "lma", "type": "etree",
        "collections": [{
            "name": "lma", "query": "collection:etree",
            "on_complete": {"webhook": "http://hook"}, "schedule": "0 3 * * 0",
        }],
    }]


def test_atomic_replace_keeps_the_mode(tmp_path: Path) -> None:
    path = tmp_path / "shakedown.yaml"
    path.write_text("old\n")
    path.chmod(0o640)
    atomic_replace(path, b"new\n")
    assert path.read_bytes() == b"new\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == ["shakedown.yaml"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through read-only modes")
def test_a_read_only_folder_fails_and_leaves_the_file(tmp_path: Path) -> None:
    folder = tmp_path / "config"
    folder.mkdir()
    path = folder / "shakedown.yaml"
    path.write_text("old\n")
    folder.chmod(0o555)
    try:
        with pytest.raises(WriteFailed) as exc_info:
            atomic_replace(path, b"new\n")
        assert exc_info.value.path == path
        assert path.read_text() == "old\n"
    finally:
        folder.chmod(0o755)


def _new_document(tmp_path: Path) -> dict[str, Any]:
    return {
        "archive_root": str(tmp_path / "archive"),
        "library_root": str(tmp_path / "library"),
        "sources": [{"name": "ia", "type": "ia", "collections": [
            {"name": "gd", "query": "collection:GratefulDead", "schedule": "0 3 * * 0"},
        ]}],
    }


def test_create_writes_a_new_file_recorded_editable(tmp_path: Path) -> None:
    path = tmp_path / "shakedown-config" / "shakedown.yaml"
    revision = create_config(path, _new_document(tmp_path))
    file = read_config_file(path)
    assert file.state == "valid" and file.revision == revision
    assert yaml.safe_load(path.read_text()) == _new_document(tmp_path)
    assert read_edit(tmp_path / "archive" / ".shakedown" / "state.db") == "editable"


def test_create_heads_the_file_with_where_to_edit_it(tmp_path: Path) -> None:
    """§spec:first-run-setup "The written file": a header saying it is editable by
    hand or in the dashboard, pointing at the example file."""
    path = tmp_path / "shakedown.yaml"
    create_config(path, _new_document(tmp_path))
    header = path.read_text().split("archive_root:")[0]
    assert header.startswith("# ") and "dashboard" in header
    assert "shakedown.example.yaml" in header


def test_create_never_replaces_a_file(tmp_path: Path) -> None:
    path = tmp_path / "shakedown.yaml"
    path.write_text("hand written\n")
    with pytest.raises(ConfigExists):
        create_config(path, _new_document(tmp_path))
    assert path.read_text() == "hand written\n"
    assert read_edit(tmp_path / "archive" / ".shakedown" / "state.db") == "locked"


def test_create_refuses_an_invalid_document(tmp_path: Path) -> None:
    path = tmp_path / "shakedown.yaml"
    doc = _new_document(tmp_path)
    doc["sources"][0]["collections"][0]["library_layout"] = "Shows/All"
    with pytest.raises(ConfigRejected) as exc_info:
        create_config(path, doc)
    assert [e.code for e in exc_info.value.errors] == ["layout_no_field"]
    assert not path.exists()


ALIASED = """\
archive_root: /a
library_root: /l
sources:
  - name: ia
    type: ia
    auth: &login
      email_env: IA_EMAIL
      password_env: IA_PASSWORD
    collections:
      - name: gd
        query: 'collection:GratefulDead'
  - name: ia2
    type: ia
    auth: *login
    collections:
      - name: phish
        query: 'collection:Phish'
"""


@pytest.mark.parametrize("edited", [0, 1])
def test_editing_one_use_of_a_shared_anchor_leaves_the_other_as_it_was(edited: int) -> None:
    def edit(doc: dict[str, Any]) -> None:
        doc["sources"][edited]["auth"]["email_env"] = "OTHER_EMAIL"

    merged = _save(ALIASED, edit)
    sources = _effective(merged)["sources"]
    assert sources[edited]["auth"]["email_env"] == "OTHER_EMAIL"
    assert sources[1 - edited]["auth"]["email_env"] == "IA_EMAIL"
