"""First-run setup (§spec:first-run-setup): `serve` with no config file, the
defaults setup offers, the path check, and finishing setup."""
from __future__ import annotations

import asyncio
import errno
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

pytest.importorskip("fastapi")

from shakedown import credentials, server, validate
from shakedown.config import read_config_file
from shakedown.server import build_app
from shakedown.trigger import Busy, ExitedEarly, Failed
from tests.serve_child import SPEC_ENV

SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}
INDEX = "<!doctype html><title>shakedown</title>"


@pytest.fixture()
def config_path(tmp_path: Path) -> Path:
    """Where the config would be; nothing is there."""
    return tmp_path / "shakedown-config" / "shakedown.yaml"


def _page(tmp_path: Path) -> Path:
    page = tmp_path / "page"
    (page / "assets").mkdir(parents=True)
    (page / "index.html").write_text(INDEX)
    return page


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# -- serve with no config file -------------------------------------------------

@pytest.mark.asyncio
async def test_serve_with_no_file_opens_the_page_and_answers_missing(
    config_path: Path, tmp_path: Path
) -> None:
    app = build_app(config_path, page_dir=_page(tmp_path))
    async with _client(app) as client:
        page = await client.get("/")
        config = (await client.get("/api/config")).json()
        reads = {
            path: await client.get(path)
            for path in ("/api/health", "/api/activity", "/api/library", "/status")
        }
    assert (page.status_code, page.text) == (200, INDEX)
    assert config["state"] == "missing"
    assert (config["revision"], config["document"], config["edit"]) == (None, None, "locked")
    assert config["source_types"]
    # The read endpoints answer as they always have with no config: 503 naming it.
    for path, response in reads.items():
        assert response.status_code == 503, path
        assert str(config_path) in response.json()["detail"], path
    assert not config_path.parent.exists()  # nothing was written


# -- the defaults setup offers -------------------------------------------------

@pytest.mark.asyncio
async def test_setup_defaults_work_on_the_example_stack(config_path: Path) -> None:
    async with _client(build_app(config_path)) as client:
        response = await client.get("/api/setup")
        config = (await client.get("/api/config")).json()
    assert response.status_code == 200
    answer = response.json()
    assert {k: answer[k] for k in ("archive_root", "library_root", "state_db", "schedule")} == {
        "archive_root": "/data/archive",
        "library_root": "/data/library",
        "state_db": "/data/archive/.shakedown/state.db",
        "schedule": "0 3 * * 0",
    }
    assert answer["source_types"] == config["source_types"]
    assert answer["generated_at"]
    assert not config_path.parent.exists()  # nothing was written


# -- the path check before finishing -------------------------------------------

@pytest.mark.asyncio
async def test_check_paths_with_no_file_creates_the_folders(
    config_path: Path, tmp_path: Path
) -> None:
    archive, library = tmp_path / "data" / "archive", tmp_path / "data" / "library"
    async with _client(build_app(config_path)) as client:
        response = await client.post(
            "/api/config/check-paths",
            headers=SAME_ORIGIN,
            json={"archive_root": str(archive), "library_root": str(library)},
        )
    assert (response.status_code, response.json()) == (200, {"warnings": []})
    assert archive.is_dir() and library.is_dir()
    assert (archive / ".shakedown").is_dir()  # the default state_db's folder
    assert not config_path.parent.exists()


# -- finishing setup -----------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CHILD_PREFIX = (sys.executable, "-m", "tests.serve_child")
PASSWORD = "s3cret-pw"


def _document(tmp_path: Path, *, type_: str = "fake") -> dict[str, Any]:
    """What the setup page sends: the paths, one source, one collection."""
    return {
        "archive_root": str(tmp_path / "data" / "archive"),
        "library_root": str(tmp_path / "data" / "library"),
        "timezone": "Europe/Berlin",
        "sources": [{"name": "ia", "type": type_, "collections": [
            {"name": "phish", "query": "collection:Phish", "schedule": "0 3 * * 0"},
        ]}],
    }


def _setup_app(config_path: Path, tmp_path: Path) -> Any:
    """serve whose first sync is a fake-source child process (tests.serve_child)."""
    spec = tmp_path / "fake-spec.json"
    spec.write_text(json.dumps({"items": {}, "gate": None}))
    return build_app(
        config_path,
        argv_prefix=CHILD_PREFIX,
        child_env={SPEC_ENV: str(spec), "PYTHONPATH": str(REPO_ROOT)},
    )


async def _finish(client: httpx.AsyncClient, body: Any, headers: Any = SAME_ORIGIN) -> Any:
    return await client.post("/api/setup", json=body, headers=headers)


async def _runs_done(app: Any) -> None:
    for triggered in app.state.triggered:
        assert await asyncio.to_thread(triggered.done.wait, 30)


@pytest.mark.asyncio
async def test_finishing_writes_the_file_and_starts_the_first_sync(
    config_path: Path, tmp_path: Path
) -> None:
    app = _setup_app(config_path, tmp_path)
    async with _client(app) as client:
        response = await _finish(client, {"document": _document(tmp_path)})
        config = (await client.get("/api/config")).json()
    await _runs_done(app)
    assert response.status_code == 201, response.text
    answer = response.json()
    assert answer["started"] == {"source": "ia", "collection": "phish"}
    assert "reason" not in answer
    assert answer["warnings"] == []
    # The file holds only what setup set, under the header, and is editable.
    text = config_path.read_text()
    assert text.startswith("# ")
    assert "shakedown.example.yaml" in text
    assert yaml.safe_load(text) == _document(tmp_path)
    assert config["state"] == "valid" and config["edit"] == "editable"
    assert config["revision"] == answer["revision"]
    # The first sync is that collection's, started as POST /sync starts one.
    (run,) = app.state.triggered
    assert run.process.args[-5:] == ["sync", "--source", "ia", "--collection", "phish"]
    assert run.process.returncode == 0


@pytest.mark.asyncio
async def test_credentials_are_stored_and_never_answered(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IA_EMAIL", raising=False)
    monkeypatch.delenv("IA_PASSWORD", raising=False)
    monkeypatch.setattr(server, "launch", lambda *a, **k: Busy())  # no real archive.org run
    body = {
        "document": _document(tmp_path, type_="ia"),
        "credentials": {"email": " me@example.org ", "password": PASSWORD},
    }
    async with _client(build_app(config_path)) as client:
        response = await _finish(client, body)
        config = await client.get("/api/config")
    assert response.status_code == 201, response.text
    assert config.json()["credentials"] == [{"source": "ia", "credentials": "dashboard"}]
    state_db = tmp_path / "data" / "archive" / ".shakedown" / "state.db"
    assert credentials.stored(state_db, "ia") == credentials.Login("me@example.org", PASSWORD)
    for text in (response.text, config.text, config_path.read_text()):
        assert PASSWORD not in text and "me@example.org" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal, reason", [
    (Busy(), "busy"), (Failed("cannot start"), "failed"), (ExitedEarly(2), "exited_early"),
])
async def test_a_first_sync_that_cannot_start_leaves_the_file_written(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    refusal: Any, reason: str,
) -> None:
    monkeypatch.setattr(server, "launch", lambda *a, **k: refusal)
    async with _client(build_app(config_path)) as client:
        response = await _finish(client, {"document": _document(tmp_path)})
    assert response.status_code == 201
    assert (response.json()["started"], response.json()["reason"]) == (None, reason)
    assert read_config_file(config_path).state == "valid"


@pytest.mark.asyncio
async def test_a_second_setup_is_refused_and_overwrites_nothing(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "launch", lambda *a, **k: Busy())
    async with _client(build_app(config_path)) as client:
        first = await _finish(client, {"document": _document(tmp_path)})
        before = config_path.read_bytes()
        changed = _document(tmp_path)
        changed["sources"][0]["collections"][0]["name"] = "dead"
        second = await _finish(client, {"document": changed})
    assert first.status_code == 201
    assert (second.status_code, second.json()) == (409, {"code": "exists"})
    assert config_path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["# written by hand\nsources: []\n", "sources: [\n"])
async def test_setup_never_overwrites_a_file_that_appeared(
    config_path: Path, tmp_path: Path, text: str
) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text(text)
    async with _client(build_app(config_path)) as client:
        response = await _finish(client, {"document": _document(tmp_path)})
    assert (response.status_code, response.json()) == (409, {"code": "exists"})
    assert config_path.read_text() == text


def _two_collections(doc: dict[str, Any]) -> None:
    doc["sources"][0]["collections"].append({"name": "dead", "query": "collection:GD"})


def _two_sources(doc: dict[str, Any]) -> None:
    doc["sources"].append({"name": "lma", "type": "fake", "collections": [
        {"name": "lma", "query": "collection:etree"},
    ]})


def _no_collection(doc: dict[str, Any]) -> None:
    doc["sources"][0]["collections"] = []


def _bad_layout(doc: dict[str, Any]) -> None:
    doc["sources"][0]["collections"][0]["library_layout"] = "Shows/All"


def _exec_handoff(doc: dict[str, Any]) -> None:
    doc["sources"][0]["collections"][0]["on_complete"] = {"exec": "/bin/true"}


@pytest.mark.asyncio
@pytest.mark.parametrize("change, expected", [
    (_two_collections, [(["sources", 0, "collections"], "setup_one_collection")]),
    (_no_collection, [(["sources", 0, "collections"], "setup_one_collection")]),
    (_two_sources, [(["sources"], "setup_one_collection")]),
    (_bad_layout, [(["sources", 0, "collections", 0, "library_layout"], "layout_no_field")]),
    (_exec_handoff, [(["sources", 0, "collections", 0, "on_complete", "exec"],
                      "exec_file_only")]),
])
async def test_a_document_setup_refuses_writes_nothing(
    config_path: Path, tmp_path: Path, change: Any, expected: list[Any]
) -> None:
    doc = _document(tmp_path)
    change(doc)
    async with _client(build_app(config_path)) as client:
        response = await _finish(client, {"document": doc})
    assert response.status_code == 422, response.text
    assert [(e["loc"], e["code"]) for e in response.json()["errors"]] == expected
    assert not config_path.parent.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    [],
    {},
    {"document": {}, "extra": 1},
    {"document": {}, "credentials": {"email": "me@example.org"}},
    {"document": {}, "credentials": {"email": "me@example.org", "password": " "}},
    {"document": {}, "credentials": {"email": "me@example.org", "password": 5}},
])
async def test_a_body_setup_cannot_read_is_one_code_never_quoted(
    config_path: Path, body: Any
) -> None:
    async with _client(build_app(config_path)) as client:
        response = await _finish(client, body)
    assert (response.status_code, response.json()) == (422, {"code": "invalid_body"})
    assert not config_path.parent.exists()


@pytest.mark.asyncio
async def test_credentials_for_a_source_that_takes_none_write_nothing(
    config_path: Path, tmp_path: Path
) -> None:
    body = {"document": _document(tmp_path),
            "credentials": {"email": "me@example.org", "password": PASSWORD}}
    async with _client(build_app(config_path)) as client:
        response = await _finish(client, body)
    assert (response.status_code, response.json()) == (404, {"code": "takes_no_credentials"})
    assert not config_path.parent.exists()


@pytest.mark.asyncio
async def test_setup_passes_the_write_gate(config_path: Path, tmp_path: Path) -> None:
    async with _client(build_app(config_path)) as client:
        response = await _finish(
            client, {"document": _document(tmp_path)}, headers={"Sec-Fetch-Site": "cross-site"}
        )
    assert (response.status_code, response.json()) == (403, {"code": "cross_site"})
    assert not config_path.parent.exists()


@pytest.mark.asyncio
async def test_a_config_folder_that_cannot_be_written(tmp_path: Path) -> None:
    locked = tmp_path / "read-only"
    locked.mkdir()
    locked.chmod(0o555)
    config_path = locked / "shakedown-config" / "shakedown.yaml"
    try:
        async with _client(build_app(config_path)) as client:
            response = await _finish(client, {"document": _document(tmp_path)})
    finally:
        locked.chmod(0o755)
    assert response.status_code == 500
    assert response.json() == {"code": "write_failed", "path": str(config_path)}
    assert not config_path.exists()


@pytest.mark.asyncio
async def test_a_library_on_another_filesystem_warns_and_setup_finishes(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cross_device(archive: Path, library: Path) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(validate, "ensure_hardlinkable", cross_device)
    monkeypatch.setattr(server, "launch", lambda *a, **k: Busy())
    async with _client(build_app(config_path)) as client:
        response = await _finish(client, {"document": _document(tmp_path)})
    assert response.status_code == 201
    assert response.json()["warnings"] == [{"loc": ["library_root"], "code": "hardlink_failed"}]
    assert read_config_file(config_path).state == "valid"


@pytest.mark.asyncio
async def test_settings_saves_the_setup_file_and_keeps_its_header(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup's file is editable from the start, and a save keeps the header."""
    monkeypatch.setattr(server, "launch", lambda *a, **k: Busy())
    async with _client(build_app(config_path)) as client:
        assert (await _finish(client, {"document": _document(tmp_path)})).status_code == 201
        header = config_path.read_text().split("archive_root:")[0]
        loaded = (await client.get("/api/config")).json()
        loaded["document"]["sources"][0]["collections"][0]["query"] = "collection:PhishLive"
        saved = await client.put(
            "/api/config", headers=SAME_ORIGIN,
            json={"revision": loaded["revision"], "document": loaded["document"]},
        )
    assert saved.status_code == 200, saved.text
    assert config_path.read_text().startswith(header)
    assert "collection:PhishLive" in config_path.read_text()
