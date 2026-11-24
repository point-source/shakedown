"""The config resource (§spec:dashboard-settings, §spec:edit-lock): `GET /api/config`
answers what the file says now and writes nothing; `PUT /api/config` saves a checked
document; check-paths and override."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

pytest.importorskip("fastapi")

from shakedown.server import build_app

HAND_WRITTEN = """\
# Shakedown, written by hand.
archive_root: {archive}
library_root: {library}

sources:
  - name: ia
    type: ia
    auth:
      email_env: TEST_IA_EMAIL
      password_env: TEST_IA_PASSWORD
    collections:
      # The Dead, every Sunday.
      - name: gd
        query: 'collection:GratefulDead'
        schedule: '0 3 * * 0'
        library_layout: '{{year}}/{{identifier}}'
      - name: phish
        query: 'collection:Phish'
  - name: lma
    type: etree
    collections:
      - name: lma
        query: 'collection:etree'
"""


@pytest.fixture()
def paths(tmp_path: Path) -> dict[str, Path]:
    archive, library = tmp_path / "archive", tmp_path / "library"
    archive.mkdir()
    library.mkdir()
    return {
        "config": tmp_path / "shakedown.yaml",
        "archive": archive,
        "library": library,
        "state_db": archive / ".shakedown" / "state.db",
    }


def _write(paths: dict[str, Path], text: str = HAND_WRITTEN) -> None:
    paths["config"].write_text(text.format(archive=paths["archive"], library=paths["library"]))


def _client(paths: dict[str, Path]) -> httpx.AsyncClient:
    app = build_app(paths["config"])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _get(paths: dict[str, Path]) -> dict[str, Any]:
    async with _client(paths) as client:
        response = await client.get("/api/config")
    assert response.status_code == 200
    return response.json()


def _snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
    return {
        str(p): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else b"")
        for p in sorted(root.rglob("*"))
    }


# -- GET -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_valid_file_with_every_default_filled_in(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_IA_EMAIL", "a@b.c")
    monkeypatch.setenv("TEST_IA_PASSWORD", "pw")
    _write(paths)
    answer = await _get(paths)

    assert answer["state"] == "valid"
    assert answer["path"] == str(paths["config"])
    assert answer["revision"].startswith("sha256:")
    assert answer["edit"] == "locked"  # a hand-written file, no record
    assert answer["errors"] == []
    doc = answer["document"]
    assert doc["archive_root"] == str(paths["archive"])
    assert doc["state_db"] == str(paths["state_db"])
    assert doc["max_concurrent_downloads"] == 4
    assert doc["keep_run_errors_days"] == 180
    assert doc["timezone"] is None and doc["notifications"] is None
    phish = doc["sources"][0]["collections"][1]
    assert phish == {
        "name": "phish", "query": "collection:Phish", "library_layout": "passthrough",
        "prune_disappeared": False, "incremental_discovery": False,
        "preserve_source_metadata": False, "fill_tags": False, "on_complete": None,
        "schedule": None,
    }
    assert answer["credentials"] == [
        {"source": "ia", "credentials": "environment"},
        {"source": "lma", "credentials": "none"},
    ]
    next_syncs = answer["next_syncs"]
    assert [(n["source"], n["collection"]) for n in next_syncs] == [
        ("ia", "gd"), ("ia", "phish"), ("lma", "lma")
    ]
    gd_next = datetime.fromisoformat(next_syncs[0]["next_at"])
    assert gd_next.tzinfo is not None and gd_next.weekday() == 6
    assert next_syncs[1]["next_at"] is None
    types = {t["type"]: t for t in answer["source_types"]}
    assert types["ia"]["takes_credentials"] is True
    assert types["etree"]["takes_credentials"] is False
    assert "identifier" in types["ia"]["layout_fields"]
    assert types["ia"]["layout_filters"] == ["lower", "sanitize", "short", "upper"]
    assert datetime.fromisoformat(answer["generated_at"]).tzinfo is not None
    assert "unconfigured" not in answer


@pytest.mark.asyncio
async def test_credentials_need_every_named_variable(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_IA_EMAIL", "a@b.c")
    monkeypatch.delenv("TEST_IA_PASSWORD", raising=False)
    _write(paths)
    assert (await _get(paths))["credentials"][0] == {"source": "ia", "credentials": "none"}


@pytest.mark.asyncio
async def test_a_missing_file(paths: dict[str, Path]) -> None:
    answer = await _get(paths)
    assert answer["state"] == "missing"
    assert answer["revision"] is None and answer["document"] is None
    assert answer["errors"] == [] and answer["edit"] == "locked"
    assert answer["credentials"] == [] and answer["next_syncs"] == []
    assert answer["source_types"]


@pytest.mark.asyncio
async def test_an_unreadable_file(paths: dict[str, Path]) -> None:
    paths["config"].write_text("archive_root: [unclosed\n")
    answer = await _get(paths)
    assert answer["state"] == "unreadable"
    assert answer["document"] is None and answer["revision"] is not None
    assert [e["code"] for e in answer["errors"]] == ["yaml_invalid"]


@pytest.mark.asyncio
async def test_an_invalid_file_shows_its_settings_and_errors(paths: dict[str, Path]) -> None:
    _write(paths, HAND_WRITTEN.replace("'{{year}}/{{identifier}}'", "'Shows/All'")
           + "max_concurrent_downloads: 0\n")
    answer = await _get(paths)
    assert answer["state"] == "invalid"
    assert sorted((tuple(e["loc"]), e["code"]) for e in answer["errors"]) == [
        (("max_concurrent_downloads",), "greater_than_equal"),
        (("sources", 0, "collections", 0, "library_layout"), "layout_no_field"),
    ]
    doc = answer["document"]
    # Kept as the file has it, so each error's loc points at its value.
    assert doc["max_concurrent_downloads"] == 0
    # Defaults are filled in around it.
    assert doc["max_concurrent_collections"] == 2
    assert doc["state_db"] == str(paths["state_db"])
    assert doc["sources"][0]["collections"][1]["fill_tags"] is False
    assert doc["sources"][0]["collections"][0]["library_layout"] == "Shows/All"
    assert answer["credentials"] == [] and answer["next_syncs"] == []


@pytest.mark.asyncio
async def test_layout_errors_point_into_the_document(paths: dict[str, Path]) -> None:
    _write(paths, HAND_WRITTEN.replace("'{{year}}/{{identifier}}'", "'Shows/All'"))
    answer = await _get(paths)
    assert [(e["loc"], e["code"]) for e in answer["errors"]] == [
        (["sources", 0, "collections", 0, "library_layout"], "layout_no_field")
    ]
    node: Any = answer["document"]
    for part in answer["errors"][0]["loc"]:
        node = node[part]
    assert node == "Shows/All"


@pytest.mark.asyncio
async def test_the_answer_follows_a_hand_edit(paths: dict[str, Path]) -> None:
    _write(paths)
    async with _client(paths) as client:
        first = (await client.get("/api/config")).json()
        paths["config"].write_text(paths["config"].read_text() + "timezone: Europe/Berlin\n")
        second = (await client.get("/api/config")).json()
    assert second["revision"] != first["revision"]
    assert second["document"]["timezone"] == "Europe/Berlin"


@pytest.mark.asyncio
async def test_reading_writes_nothing(paths: dict[str, Path]) -> None:
    _write(paths)
    root = paths["config"].parent
    before = _snapshot(root)
    await _get(paths)
    paths["config"].write_text("not: [valid\n")
    before_broken = _snapshot(root)
    await _get(paths)
    assert _snapshot(root) == before_broken
    assert not paths["state_db"].exists()
    assert set(before) == set(before_broken)


def test_invalid_document_values_are_json_safe(paths: dict[str, Path]) -> None:
    """A YAML date or timestamp left unquoted reaches the document as text."""
    from shakedown.config import read_config_file
    from shakedown.dashboard_config import document_of

    _write(paths, HAND_WRITTEN.replace("'collection:Phish'", "2024-01-01")
           .replace("query: 'collection:etree'", "query: 'q'\n        bogus: 2024-01-01 10:00:00"))
    file = read_config_file(paths["config"])
    doc = document_of(file)
    assert doc is not None
    assert doc["sources"][0]["collections"][1]["query"] == "2024-01-01"
    yaml.safe_dump(doc)  # plain data
