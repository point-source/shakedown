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


# -- PUT, check-paths, override -------------------------------------------------

SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


async def _put(
    client: httpx.AsyncClient, revision: Any, document: Any, headers: Any = SAME_ORIGIN
) -> httpx.Response:
    return await client.put(
        "/api/config", json={"revision": revision, "document": document}, headers=headers
    )


async def _override(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/api/config/override", json={}, headers=SAME_ORIGIN)


async def _unlocked(paths: dict[str, Path], text: str = HAND_WRITTEN) -> dict[str, Any]:
    """A hand-written file with the override on, as loaded by the editor."""
    _write(paths, text)
    async with _client(paths) as client:
        assert (await _override(client)).status_code == 200
        return (await client.get("/api/config")).json()


def _gd(doc: dict[str, Any]) -> dict[str, Any]:
    return doc["sources"][0]["collections"][0]


@pytest.mark.asyncio
async def test_a_layout_with_no_source_field_is_refused_and_nothing_written(
    paths: dict[str, Path],
) -> None:
    loaded = await _unlocked(paths)
    before = paths["config"].read_bytes()
    doc = loaded["document"]
    _gd(doc)["library_layout"] = "Shows/All"
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
    assert response.status_code == 422
    assert [(e["loc"], e["code"]) for e in response.json()["errors"]] == [
        (["sources", 0, "collections", 0, "library_layout"], "layout_no_field")
    ]
    assert paths["config"].read_bytes() == before


@pytest.mark.asyncio
async def test_a_save_keeps_comments_and_the_next_load_uses_it(
    paths: dict[str, Path],
) -> None:
    from shakedown.config import load

    loaded = await _unlocked(paths)
    doc = loaded["document"]
    _gd(doc)["library_layout"] = "{year}/{venue}/{identifier}"
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
        after = (await client.get("/api/config")).json()
    assert response.status_code == 200, response.json()
    assert response.json() == {"revision": after["revision"], "warnings": []}
    text = paths["config"].read_text()
    assert "# Shakedown, written by hand." in text
    assert "      # The Dead, every Sunday." in text
    assert "library_layout: '{year}/{venue}/{identifier}'" in text
    config = load(paths["config"])
    assert config.sources[0].collections[0].library_layout == "{year}/{venue}/{identifier}"


@pytest.mark.asyncio
async def test_a_shared_field_layout_saves_with_a_warning(paths: dict[str, Path]) -> None:
    loaded = await _unlocked(paths)
    doc = loaded["document"]
    _gd(doc)["library_layout"] = "{year}/{venue}"
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
    assert response.status_code == 200
    assert response.json()["warnings"] == [
        {"loc": ["sources", 0, "collections", 0, "library_layout"], "code": "layout_not_unique"}
    ]


@pytest.mark.asyncio
async def test_a_hand_edit_after_loading_is_a_conflict(paths: dict[str, Path]) -> None:
    loaded = await _unlocked(paths)
    paths["config"].write_text(paths["config"].read_text() + "# edited by hand\n")
    edited = paths["config"].read_bytes()
    doc = loaded["document"]
    doc["max_concurrent_downloads"] = 6
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
    assert (response.status_code, response.json()) == (409, {"code": "changed"})
    assert paths["config"].read_bytes() == edited


@pytest.mark.asyncio
async def test_a_library_on_another_filesystem_saves_with_warnings(
    paths: dict[str, Path], tmp_path: Path
) -> None:
    import errno
    import os
    from unittest.mock import patch

    loaded = await _unlocked(paths)
    doc = loaded["document"]
    doc["library_root"] = str(tmp_path / "elsewhere")

    def exdev(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), str(src), None, str(dst))

    async with _client(paths) as client:
        with patch("shakedown.filesystem.os.link", side_effect=exdev):
            response = await _put(client, loaded["revision"], doc)
    assert response.status_code == 200
    assert response.json()["warnings"] == [
        {"loc": ["library_root"], "code": "path_moved"},
        {"loc": ["library_root"], "code": "hardlink_failed"},
    ]
    assert f"library_root: {tmp_path / 'elsewhere'}" in paths["config"].read_text()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda doc: _gd(doc).update(on_complete={"exec": "/bin/x"}), id="added"),
        pytest.param(
            lambda doc: doc["sources"][0]["collections"].append(
                {**_gd(doc), "name": "new", "on_complete": {"exec": "/bin/x"}}
            ),
            id="new-collection",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_command_handoff_cannot_be_set_from_the_api(
    paths: dict[str, Path], change: Any
) -> None:
    loaded = await _unlocked(paths)
    before = paths["config"].read_bytes()
    doc = loaded["document"]
    change(doc)
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
    assert response.status_code == 422
    assert [e["code"] for e in response.json()["errors"]] == ["exec_file_only"]
    assert response.json()["errors"][0]["loc"][-2:] == ["on_complete", "exec"]
    assert paths["config"].read_bytes() == before


WITH_EXEC = HAND_WRITTEN.replace(
    "        schedule: '0 3 * * 0'\n",
    "        schedule: '0 3 * * 0'\n        on_complete:\n          exec: /bin/notify\n",
)


@pytest.mark.parametrize(
    ("change", "status"),
    [
        pytest.param(lambda doc: None, 200, id="kept"),
        pytest.param(lambda doc: doc.update(max_concurrent_downloads=6), 200, id="other-edit"),
        pytest.param(
            lambda doc: _gd(doc).update(on_complete={"exec": "/bin/other"}), 422, id="changed"
        ),
        pytest.param(
            lambda doc: _gd(doc).update(on_complete={"webhook": "http://h"}), 422, id="swapped"
        ),
        pytest.param(lambda doc: _gd(doc).update(on_complete=None), 422, id="removed"),
        pytest.param(
            lambda doc: doc["sources"][0]["collections"].pop(0), 200, id="collection-removed"
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_command_handoff_in_the_file_stays_as_written(
    paths: dict[str, Path], change: Any, status: int
) -> None:
    loaded = await _unlocked(paths, WITH_EXEC)
    doc = loaded["document"]
    change(doc)
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
    assert response.status_code == status, response.json()
    if status == 422:
        assert [e["code"] for e in response.json()["errors"]] == ["exec_file_only"]


@pytest.mark.asyncio
async def test_a_hand_written_file_is_locked_until_the_override(
    paths: dict[str, Path],
) -> None:
    _write(paths)
    async with _client(paths) as client:
        loaded = (await client.get("/api/config")).json()
        assert loaded["edit"] == "locked"
        response = await _put(client, loaded["revision"], loaded["document"])
        assert (response.status_code, response.json()) == (409, {"code": "locked"})

        for _ in range(2):  # idempotent
            response = await _override(client)
            assert (response.status_code, response.json()) == (200, {"edit": "override"})
    # A restarted serve reads the same state.
    async with _client(paths) as client:
        loaded = (await client.get("/api/config")).json()
        assert loaded["edit"] == "override"
        doc = loaded["document"]
        doc["max_concurrent_downloads"] = 6
        assert (await _put(client, loaded["revision"], doc)).status_code == 200


@pytest.mark.asyncio
async def test_a_file_setup_wrote_is_editable(paths: dict[str, Path]) -> None:
    from shakedown.config_file import create_config

    create_config(paths["config"], {
        "archive_root": str(paths["archive"]), "library_root": str(paths["library"]),
        "sources": [{"name": "ia", "type": "ia", "collections": [
            {"name": "gd", "query": "q", "library_layout": "{identifier}"}]}],
    })
    async with _client(paths) as client:
        loaded = (await client.get("/api/config")).json()
        assert loaded["edit"] == "editable"
        doc = loaded["document"]
        doc["sources"][0]["collections"][0]["fill_tags"] = True
        assert (await _put(client, loaded["revision"], doc)).status_code == 200


@pytest.mark.asyncio
async def test_moving_the_state_database_moves_the_edit_state(
    paths: dict[str, Path], tmp_path: Path
) -> None:
    from shakedown.edit_lock import read_edit

    loaded = await _unlocked(paths)
    doc = loaded["document"]
    new_db = tmp_path / "state" / "state.db"
    doc["state_db"] = str(new_db)
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
        assert response.status_code == 200
        assert response.json()["warnings"] == [{"loc": ["state_db"], "code": "path_moved"}]
        assert read_edit(new_db) == "override"
        assert (await client.get("/api/config")).json()["edit"] == "override"


@pytest.mark.asyncio
async def test_writes_need_the_write_gate(paths: dict[str, Path]) -> None:
    loaded = await _unlocked(paths)
    async with _client(paths) as client:
        for response in (
            await _put(client, loaded["revision"], loaded["document"], headers={}),
            await client.post("/api/config/override"),
            await client.post("/api/config/check-paths", content="{}"),
        ):
            assert (response.status_code, response.json()) == (403, {"code": "cross_site"})


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root writes anywhere")
@pytest.mark.asyncio
async def test_an_unwritable_config_folder_is_write_failed(paths: dict[str, Path]) -> None:
    loaded = await _unlocked(paths)
    before = paths["config"].read_bytes()
    doc = loaded["document"]
    doc["max_concurrent_downloads"] = 6
    folder = paths["config"].parent
    folder.chmod(0o555)
    try:
        async with _client(paths) as client:
            response = await _put(client, loaded["revision"], doc)
    finally:
        folder.chmod(0o755)
    assert (response.status_code, response.json()) == (
        500, {"code": "write_failed", "path": str(paths["config"])}
    )
    assert paths["config"].read_bytes() == before


@pytest.mark.asyncio
async def test_a_missing_or_unreadable_file_cannot_be_saved(paths: dict[str, Path]) -> None:
    async with _client(paths) as client:
        response = await _put(client, None, {})
        assert (response.status_code, response.json()) == (409, {"code": "missing"})
        assert (await _override(client)).json() == {"code": "missing"}
        paths["config"].write_text("a: [b\n")
        revision = (await client.get("/api/config")).json()["revision"]
        response = await _put(client, revision, {})
        assert (response.status_code, response.json()) == (409, {"code": "unreadable"})


@pytest.mark.asyncio
async def test_an_invalid_file_can_be_unlocked_and_fixed(paths: dict[str, Path]) -> None:
    loaded = await _unlocked(
        paths, HAND_WRITTEN.replace("'{{year}}/{{identifier}}'", "'Shows/All'")
    )
    assert loaded["state"] == "invalid" and loaded["edit"] == "override"
    doc = loaded["document"]
    _gd(doc)["library_layout"] = "{identifier}"
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
        assert response.status_code == 200, response.json()
        assert (await client.get("/api/config")).json()["state"] == "valid"
    assert "      # The Dead, every Sunday." in paths["config"].read_text()


@pytest.mark.asyncio
async def test_check_paths_saves_nothing(paths: dict[str, Path], tmp_path: Path) -> None:
    _write(paths)
    before = paths["config"].read_bytes()
    blocked = tmp_path / "a-file"
    blocked.write_text("")
    async with _client(paths) as client:
        ok = await client.post("/api/config/check-paths", headers=SAME_ORIGIN, json={
            "archive_root": str(paths["archive"]), "library_root": str(paths["library"]),
        })
        bad = await client.post("/api/config/check-paths", headers=SAME_ORIGIN, json={
            "archive_root": str(paths["archive"]), "library_root": str(blocked / "lib"),
            "state_db": str(paths["state_db"]),
        })
    assert (ok.status_code, ok.json()) == (200, {"warnings": []})
    assert bad.json() == {"warnings": [
        {"loc": ["library_root"], "code": "not_writable"},
        {"loc": ["library_root"], "code": "hardlink_failed"},
    ]}
    assert paths["config"].read_bytes() == before


@pytest.mark.asyncio
async def test_a_name_that_is_not_text_is_refused_not_a_crash(paths: dict[str, Path]) -> None:
    loaded = await _unlocked(paths)
    doc = loaded["document"]
    _gd(doc)["name"] = ["x"]
    doc["sources"][1]["name"] = {"a": 1}
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
    assert response.status_code == 422
    assert {e["code"] for e in response.json()["errors"]} == {"string_type"}


@pytest.mark.asyncio
async def test_a_source_name_split_over_two_entries_saves(paths: dict[str, Path]) -> None:
    """One source may span two entries (§spec:source-budget shares one host's
    budget by name); a save matches each entry by its occurrence."""
    loaded = await _unlocked(paths)
    doc = loaded["document"]
    doc["sources"].append({"name": "ia", "type": "ia", "collections": [
        {"name": "other", "query": "q"}]})
    async with _client(paths) as client:
        first = await _put(client, loaded["revision"], doc)
        assert first.status_code == 200, first.json()
        doc["sources"][2]["collections"][0]["query"] = "q2"
        doc["sources"][0]["collections"][1]["fill_tags"] = True
        second = await _put(client, first.json()["revision"], doc)
        assert second.status_code == 200, second.json()
        saved = (await client.get("/api/config")).json()["document"]
    assert [s["name"] for s in saved["sources"]] == ["ia", "lma", "ia"]
    assert saved["sources"][2]["collections"][0]["query"] == "q2"
    assert saved["sources"][0]["collections"][1]["fill_tags"] is True
    assert saved["sources"][0]["collections"][0]["name"] == "gd"
    assert "      # The Dead, every Sunday." in paths["config"].read_text()


@pytest.mark.asyncio
async def test_a_refused_save_leaves_the_new_state_database_alone(
    paths: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The edit state moves only with a save that goes through: a hand edit landing
    while the save runs leaves no unlocked record in the database it named."""
    from shakedown import config_save

    loaded = await _unlocked(paths)
    doc = loaded["document"]
    new_db = tmp_path / "moved" / "state.db"
    doc["state_db"] = str(new_db)
    real_merge = config_save.merge

    def merge_then_hand_edit(*args):  # type: ignore[no-untyped-def]
        merged = real_merge(*args)
        paths["config"].write_text(paths["config"].read_text() + "# hand edit\n")
        return merged

    monkeypatch.setattr(config_save, "merge", merge_then_hand_edit)
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
    assert (response.status_code, response.json()) == (409, {"code": "changed"})
    assert not new_db.exists()


# -- Stored credentials follow the save (§spec:dashboard-credentials) -------------


@pytest.mark.asyncio
async def test_moving_the_state_database_moves_the_stored_credentials(
    paths: dict[str, Path], tmp_path: Path
) -> None:
    from shakedown.credentials import Login, store, stored

    loaded = await _unlocked(paths)
    store(paths["state_db"], "ia", Login("me@example.org", "pw"))
    doc = loaded["document"]
    new_db = tmp_path / "state" / "state.db"
    doc["state_db"] = str(new_db)
    async with _client(paths) as client:
        assert (await _put(client, loaded["revision"], doc)).status_code == 200
    assert stored(new_db, "ia") == Login("me@example.org", "pw")


@pytest.mark.asyncio
async def test_a_save_that_removes_a_source_forgets_its_credentials(
    paths: dict[str, Path],
) -> None:
    from shakedown.credentials import Login, store, stored

    loaded = await _unlocked(paths)
    store(paths["state_db"], "ia", Login("me@example.org", "pw"))
    store(paths["state_db"], "lma", Login("x@example.org", "pw2"))
    store(paths["state_db"], "hand-removed", Login("y@example.org", "pw3"))
    doc = loaded["document"]
    doc["sources"] = [s for s in doc["sources"] if s["name"] != "ia"]
    async with _client(paths) as client:
        response = await _put(client, loaded["revision"], doc)
    assert response.status_code == 200, response.json()
    assert stored(paths["state_db"], "ia") is None
    assert stored(paths["state_db"], "hand-removed") is None
    assert stored(paths["state_db"], "lma") == Login("x@example.org", "pw2")


@pytest.mark.asyncio
async def test_a_refused_save_forgets_nothing(paths: dict[str, Path]) -> None:
    from shakedown.credentials import Login, store, stored

    loaded = await _unlocked(paths)
    store(paths["state_db"], "ia", Login("me@example.org", "pw"))
    doc = loaded["document"]
    doc["sources"] = [s for s in doc["sources"] if s["name"] != "ia"]
    async with _client(paths) as client:
        stale = await _put(client, "sha256:stale", doc)
        doc["sources"][0]["collections"][0]["library_layout"] = "Shows/All"
        invalid = await _put(client, loaded["revision"], doc)
    assert stale.status_code == 409 and invalid.status_code == 422
    assert stored(paths["state_db"], "ia") == Login("me@example.org", "pw")
