"""The one config validation (§spec:dashboard-settings, §spec:configuration):
`check_document` returns every error as `{loc, code, message}`, never quoting a value
from the file, and `load` refuses exactly what it refuses."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from shakedown.config import ConfigError, check_document, load, read_config_file

SECRET = "SECRET-TOKEN-7f3a"


def _doc(**collection: Any) -> dict[str, Any]:
    return {
        "archive_root": "/a",
        "library_root": "/l",
        "sources": [
            {
                "name": "ia",
                "type": "ia",
                "collections": [{"name": "gd", "query": "q", **collection}],
            }
        ],
    }


def _codes(raw: Any) -> list[tuple[list[Any], str]]:
    return [(list(p.loc), p.code) for p in check_document(raw).errors]


def test_a_valid_document_gives_the_config() -> None:
    checked = check_document(_doc())
    assert checked.errors == [] and checked.config is not None
    assert checked.config.state_db == Path("/a/.shakedown/state.db")


def test_every_error_is_returned_with_its_place_and_code() -> None:
    raw = _doc(library_layout="Shows/All")
    raw["sources"][0]["collections"].append(
        {"name": "phish", "query": "q", "library_layout": "{filename}"}
    )
    raw["sources"][0]["collections"].append({"name": "gd", "query": "q"})
    raw["sources"].append({"name": "x", "type": "nope", "collections": []})
    assert _codes(raw) == [
        (["sources", 0, "collections", 2, "name"], "duplicate_name"),
        (["sources", 0, "collections", 0, "library_layout"], "layout_no_field"),
        (["sources", 0, "collections", 1, "library_layout"], "layout_unknown_field"),
        (["sources", 1, "type"], "source_type_unknown"),
    ]


def test_a_collection_repeated_under_a_second_source_entry_of_the_same_name() -> None:
    """Archived shows live under source and collection names, so the pair is unique
    across the whole file, not only within one source entry."""
    raw = _doc()
    raw["sources"].append(
        {"name": "ia", "type": "ia", "collections": [{"name": "gd", "query": "q"}]}
    )
    assert _codes(raw) == [(["sources", 1, "collections", 0, "name"], "duplicate_name")]


def test_model_errors_carry_pydantic_codes() -> None:
    raw = _doc(bogus=1, fill_tags="maybe")
    raw["max_concurrent_downloads"] = 0
    raw["timezone"] = "Mars/Olympus"
    raw["sources"][0]["collections"][0]["schedule"] = "* * *"
    assert sorted(_codes(raw)) == sorted([
        (["max_concurrent_downloads"], "greater_than_equal"),
        (["sources", 0, "collections", 0, "fill_tags"], "bool_parsing"),
        (["sources", 0, "collections", 0, "schedule"], "schedule_invalid"),
        (["sources", 0, "collections", 0, "bogus"], "extra_forbidden"),
        (["timezone"], "timezone_unknown"),
    ])


def test_model_errors_do_not_hide_layout_and_name_errors() -> None:
    raw = _doc(library_layout="Shows/All")
    raw["max_concurrent_downloads"] = 0
    raw["sources"][0]["collections"].append({"name": "gd", "query": 7})
    assert sorted(_codes(raw)) == sorted([
        (["max_concurrent_downloads"], "greater_than_equal"),
        (["sources", 0, "collections", 1, "query"], "string_type"),
        (["sources", 0, "collections", 1, "name"], "duplicate_name"),
        (["sources", 0, "collections", 0, "library_layout"], "layout_no_field"),
    ])


def test_a_shared_field_layout_is_a_warning_not_an_error() -> None:
    checked = check_document(_doc(library_layout="{year}/{venue}"))
    assert checked.errors == [] and checked.config is not None
    assert [(list(w.loc), w.code) for w in checked.warnings] == [
        (["sources", 0, "collections", 0, "library_layout"], "layout_not_unique")
    ]


@pytest.mark.parametrize(
    "collection",
    [
        {"library_layout": f"shows-{SECRET}"},
        {"library_layout": f"{{{SECRET}}}"},
        {"library_layout": f"{{identifier|{SECRET}}}"},
        {"schedule": f"{SECRET} * * * *"},
        {"on_complete": {"webhok": f"https://hooks.example/{SECRET}"}},
        {"fill_tags": SECRET},
        {"query": ["https://x/" + SECRET]},
    ],
)
def test_no_message_quotes_a_value_from_the_file(collection: dict[str, Any]) -> None:
    errors = check_document(_doc(**collection)).errors
    assert errors
    assert all(SECRET not in p.message for p in errors)


def test_names_and_types_are_never_quoted() -> None:
    raw = _doc()
    raw["sources"][0]["collections"].append({"name": "gd", "query": "q"})
    raw["sources"].append({"name": "x", "type": SECRET, "collections": []})
    errors = check_document(raw).errors
    assert len(errors) == 2
    assert all(SECRET not in p.message and "'gd'" not in p.message for p in errors)


DOCUMENTS = [
    _doc(),
    _doc(library_layout="{year}/{venue}"),
    _doc(library_layout="Shows/All"),
    _doc(library_layout="{nope}"),
    _doc(schedule="0 3 * * 0"),
    _doc(schedule="61 * * * *"),
    {**_doc(), "keep_run_errors_days": 7},
]


@pytest.mark.parametrize("raw", DOCUMENTS)
def test_load_refuses_exactly_what_check_document_refuses(raw: Any, tmp_path: Path) -> None:
    path = tmp_path / "shakedown.yaml"
    path.write_text(yaml.safe_dump(raw))
    checked = check_document(raw)
    if checked.errors:
        with pytest.raises(ConfigError) as exc_info:
            load(path)
        assert exc_info.value.problems == checked.errors
    else:
        assert load(path) == checked.config


def test_the_file_states(tmp_path: Path) -> None:
    path = tmp_path / "shakedown.yaml"
    assert read_config_file(path).state == "missing"
    assert read_config_file(path).revision is None

    path.write_text("archive_root: [unclosed\n")
    unreadable = read_config_file(path)
    assert unreadable.state == "unreadable"
    assert [(p.loc, p.code) for p in unreadable.errors] == [((), "yaml_invalid")]
    assert unreadable.revision is not None

    path.write_text("- a list\n")
    assert [p.code for p in read_config_file(path).errors] == ["root_not_mapping"]

    path.write_text(yaml.safe_dump(_doc(library_layout="Shows/All")))
    invalid = read_config_file(path)
    assert invalid.state == "invalid" and invalid.config is None
    assert [p.code for p in invalid.errors] == ["layout_no_field"]

    path.write_text(yaml.safe_dump(_doc()))
    valid = read_config_file(path)
    assert valid.state == "valid" and valid.errors == [] and valid.config is not None


def test_the_revision_follows_the_exact_bytes(tmp_path: Path) -> None:
    path = tmp_path / "shakedown.yaml"
    path.write_text(yaml.safe_dump(_doc()))
    first = read_config_file(path).revision
    assert read_config_file(path).revision == first
    path.write_text(yaml.safe_dump(_doc()) + "# a comment\n")
    assert read_config_file(path).revision != first

