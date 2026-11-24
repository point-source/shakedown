"""`POST /api/collections/purge` (§spec:collection-removal "Two deletes"): it starts
the `purge` run for a collection the config file no longer names, behind the write
gate and outside the edit lock."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.config import Config
from shakedown.lock import writer_lock
from shakedown.write_gate import TOKEN_ENV
from tests.test_purge import C, S, _removed, synced  # noqa: F401 (fixture)
from tests.test_server import AUTH, BUSY, CROSS_SITE, DASHBOARD, _child_app, _finish

PURGE = "/api/collections/purge"


def _client(app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _body(scope: str = "library", collection: str = C) -> dict[str, Any]:
    return {"source": S, "collection": collection, "scope": scope}


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")


@pytest.mark.asyncio
async def test_a_library_purge_starts_and_shows_as_a_purge_run(synced: Config) -> None:  # noqa: F811
    app = _child_app(_removed(synced))
    async with _client(app) as client:
        assert (await client.get("/api/config")).json()["edit"] == "locked"
        r = await client.post(PURGE, json=_body(), headers=DASHBOARD)
        assert (r.status_code, r.json()) == (202, {
            "started": "purge", "source": S, "collection": C, "scope": "library"})
        await _finish(app)
        activity = (await client.get("/api/activity")).json()["runs"]
        runs = (await client.get("/api/runs")).json()["runs"]
        unconfigured = (await client.get("/api/config")).json()["unconfigured"]
    assert not (synced.library_root / S / C).exists()
    assert (synced.archive_root / S / C / "show-a" / "t1.flac").exists()
    [live] = [e for e in activity if e["kind"] == "purge"]
    [row] = [r for r in runs if r["kind"] == "purge"]
    for answer in (live, row):
        assert (answer["source"], answer["collection"]) == (S, C)
        assert answer["result"] == "completed" and answer["bytes_freed"] == 0
    assert [(u["collection"], u["shows"], u["library_bytes"]) for u in unconfigured] == [
        (C, 2, 0)]


@pytest.mark.asyncio
async def test_an_everything_purge_takes_the_collection_off_the_list(synced: Config) -> None:  # noqa: F811
    app = _child_app(_removed(synced))
    async with _client(app) as client:
        r = await client.post(PURGE, json=_body("everything"), headers=AUTH)
        assert r.status_code == 202 and r.json()["scope"] == "everything"
        await _finish(app)
        assert (await client.get("/api/config")).json()["unconfigured"] == []
    assert not (synced.archive_root / S / C).exists()


@pytest.mark.asyncio
async def test_a_purge_while_a_run_holds_the_lock_answers_409(synced: Config) -> None:  # noqa: F811
    app = _child_app(_removed(synced))
    async with _client(app) as client:
        with writer_lock(synced):
            r = await client.post(PURGE, json=_body("everything"), headers=DASHBOARD)
    assert (r.status_code, r.json()["detail"]) == (409, BUSY)
    assert (synced.archive_root / S / C / "show-a").is_dir()


@pytest.mark.asyncio
async def test_a_configured_collection_answers_409_configured(synced: Config) -> None:  # noqa: F811
    app = _child_app(synced)
    async with _client(app) as client:
        r = await client.post(PURGE, json=_body("everything"), headers=DASHBOARD)
    assert (r.status_code, r.json()) == (409, {"code": "configured"})
    assert (synced.library_root / S / C / "show-a").is_dir()


@pytest.mark.asyncio
async def test_a_collection_with_nothing_held_answers_404(synced: Config) -> None:  # noqa: F811
    app = _child_app(_removed(synced))
    async with _client(app) as client:
        r = await client.post(PURGE, json=_body(collection="never"), headers=DASHBOARD)
    assert r.status_code == 404
    assert "nothing is held" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_purge_needs_the_write_gate_and_a_known_scope(
    synced: Config,  # noqa: F811
) -> None:
    app = _child_app(_removed(synced))
    async with _client(app) as client:
        r = await client.post(PURGE, json=_body())
        assert (r.status_code, r.json()) == (403, CROSS_SITE)
        r = await client.post(PURGE, json=_body("archive"), headers=DASHBOARD)
        assert r.status_code == 422
    assert (synced.library_root / S / C / "show-a").is_dir()

