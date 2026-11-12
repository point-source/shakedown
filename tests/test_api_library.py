"""`GET /api/library` (§spec:library-stats): what the mirror holds — matching
`status` exactly — the disk, 30 days of growth, the most recently mirrored shows,
and roughly when the disk fills."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.dashboard import VIEW_TTL_S
from shakedown.db import connect
from shakedown.models import Item, ItemStatus, Manifest, ManifestFile, Run
from shakedown.server import build_app
from shakedown.state import ItemRepo, RunRepo
from shakedown.status import print_status
from shakedown.sync import run_sync
from tests.conftest import make_config, write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin
from tests.test_server import _Clock

COLLECTION_FIELDS = {"source", "collection", "counts", "restricted", "archive_bytes",
                     "library_bytes", "last_successful_sync_at"}


def _app(config: Config, clock: _Clock):  # type: ignore[no-untyped-def]
    return build_app(write_config(config.archive_root.parent / "cfg.yaml", config), clock=clock)


async def _get(app, path: str = "/api/library") -> dict[str, Any]:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.get(path)
        assert r.status_code == 200, r.text
        return r.json()


def _two_collections(archive: Path, library: Path) -> Config:
    return Config(
        archive_root=archive, library_root=library,
        sources=[SourceConfig(name="fake-src", type="fake", collections=[
            CollectionConfig(name="coll1", query="*", library_layout="passthrough"),
            CollectionConfig(name="coll2", query="*", library_layout="passthrough"),
        ])],
    )


def _item(collection: str, identifier: str, status: ItemStatus, *, size: int = 100,
          archived: bool = True, reason: str | None = None) -> Item:
    return Item(
        source_name="fake-src", collection_name=collection, identifier=identifier,
        status=status, archive_path=Path(f"/a/{identifier}") if archived else None,
        discovered_at=datetime(2026, 10, 1), downloaded_at=datetime(2026, 10, 1),
        restriction_reason=reason,
        recorded_manifest=Manifest(files=(
            ManifestFile("t.flac", size, "m"),
            ManifestFile("t.shn", size * 3, "n", library=False),
        )),
    )


def _status(config: Config, capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    print_status(config, as_json=True)
    return json.loads(capsys.readouterr().out)


@pytest.mark.asyncio
async def test_counts_and_bytes_match_status(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = _two_collections(*tmp_roots)
    with closing(connect(config.state_db)) as conn:
        items = ItemRepo(conn)
        for item in (
            _item("coll1", "a", ItemStatus.COMPLETE),
            _item("coll1", "b", ItemStatus.COMPLETE, reason="lossless refused"),
            _item("coll1", "c", ItemStatus.FAILED, size=40),
            _item("coll1", "d", ItemStatus.UNAVAILABLE, archived=False, reason="dark"),
            _item("coll1", "e", ItemStatus.DISAPPEARED, size=7),
            _item("coll1", "f", ItemStatus.PRUNED, archived=False),
            _item("coll2", "g", ItemStatus.COMPLETE, size=11),
            _item("coll2", "h", ItemStatus.PENDING, archived=False),
        ):
            items.upsert(item)

    body = await _get(_app(config, _Clock()))
    status = _status(config, capsys)

    assert [c["collection"] for c in body["collections"]] == ["coll1", "coll2"]
    for got, want in zip(body["collections"], status, strict=True):
        assert set(got) == COLLECTION_FIELDS
        assert got["counts"] == want["counts"]
        assert got["restricted"] == len(want["restricted"])
        assert got["archive_bytes"] == want["bytes_on_disk"]
        assert got["library_bytes"] <= got["archive_bytes"]
    coll1 = body["collections"][0]
    assert coll1["restricted"] == 2 and coll1["counts"]["disappeared"] == 1
    assert coll1["library_bytes"] == 100 + 100 + 40 + 7
    totals = body["totals"]
    assert totals["archive_bytes"] == sum(s["bytes_on_disk"] for s in status)
    assert totals["library_bytes"] == sum(c["library_bytes"] for c in body["collections"])
    assert totals["restricted"] == 2
    assert totals["counts"] == {
        k: sum(s["counts"][k] for s in status) for k in status[0]["counts"]}


@pytest.mark.asyncio
async def test_disk_is_the_archive_filesystem(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(*tmp_roots)
    seen: list[str] = []
    real = os.statvfs

    def statvfs(path):  # type: ignore[no-untyped-def]
        seen.append(str(path))
        return real(path)

    monkeypatch.setattr("shakedown.dashboard_library.os.statvfs", statvfs)
    body = await _get(_app(config, _Clock()))
    st = real(config.archive_root)
    assert seen == [str(config.archive_root)]
    assert body["disk"]["total_bytes"] == st.f_blocks * st.f_frsize
    assert abs(body["disk"]["free_bytes"] - st.f_bavail * st.f_frsize) < 64 * 2**20
    assert body["disk"]["full_in_days"] is None


def _show(identifier: str, size: int) -> None:
    FakePlugin.items[identifier] = FakeItem(
        identifier=identifier, files=[FakeFile("t01.flac", b"F" * size, format="Flac")],
        metadata={"title": f"Title {identifier}"})


@pytest.mark.asyncio
async def test_a_sync_that_adds_shows_leads_recently_mirrored_and_raises_growth(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    _show("old", 100)
    assert run_sync(config) == 0
    clock = _Clock()
    app = _app(config, clock)
    before = await _get(app)
    [today_before] = [g for g in before["growth"] if g["date"] == date.today().isoformat()]
    assert (today_before["complete"], today_before["archive_bytes"]) == (1, 100)

    _show("new1", 300)
    _show("new2", 200)
    assert run_sync(config) == 0
    clock.now += VIEW_TTL_S
    after = await _get(app)

    [today] = [g for g in after["growth"] if g["date"] == date.today().isoformat()]
    assert (today["complete"], today["archive_bytes"]) == (3, 600)
    recent = after["recently_mirrored"]
    assert {r["identifier"] for r in recent[:2]} == {"new1", "new2"}
    assert recent[2]["identifier"] == "old"
    assert recent[0]["title"] == f"Title {recent[0]['identifier']}"
    assert set(recent[0]) == {"source", "collection", "identifier", "title", "downloaded_at"}
    assert datetime.fromisoformat(recent[0]["downloaded_at"]).tzinfo is not None
    coll = after["collections"][0]
    assert coll["last_successful_sync_at"] is not None
    with closing(connect(config.state_db)) as conn:
        last = RunRepo(conn).latest("fake-src", "coll1")
    assert isinstance(last, Run) and last.finished_at is not None
    assert datetime.fromisoformat(coll["last_successful_sync_at"]) == last.finished_at.astimezone()


def _seed_growth(config: Config, days: int) -> None:
    now = datetime.now()
    with closing(connect(config.state_db)) as conn:
        for n in range(days):
            conn.execute(
                "INSERT INTO growth_snapshots (source_name, collection_name, taken_at,"
                " complete, archive_bytes) VALUES ('fake-src', 'coll1', ?, ?, ?)",
                ((now - timedelta(days=days - 1 - n)).isoformat(), n, 1_000_000 * (n + 1)),
            )


@pytest.mark.asyncio
async def test_eight_days_of_growth_show_when_the_disk_fills(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    _seed_growth(config, 8)
    body = await _get(_app(config, _Clock()))
    assert len(body["growth"]) == 8
    free = body["disk"]["free_bytes"]
    assert body["disk"]["full_in_days"] == free // 1_000_000


@pytest.mark.asyncio
async def test_six_days_of_growth_give_no_estimate(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    _seed_growth(config, 6)
    body = await _get(_app(config, _Clock()))
    assert len(body["growth"]) == 6
    assert body["disk"]["full_in_days"] is None


@pytest.mark.asyncio
async def test_a_fresh_migration_shows_one_growth_point_per_collection(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = _two_collections(*tmp_roots)
    with closing(connect(config.state_db)) as conn:
        ItemRepo(conn).upsert(_item("coll1", "a", ItemStatus.COMPLETE))
        ItemRepo(conn).upsert(_item("coll2", "b", ItemStatus.COMPLETE, size=5))
        conn.execute("DROP TABLE growth_snapshots")
        conn.execute("UPDATE schema_version SET version = 9")
    with closing(connect(config.state_db)):
        pass  # a locked run's connect migrates

    body = await _get(_app(config, _Clock()))
    assert body["growth"] == [
        {"date": date.today().isoformat(), "source": "fake-src", "collection": "coll1",
         "complete": 1, "archive_bytes": 400},
        {"date": date.today().isoformat(), "source": "fake-src", "collection": "coll2",
         "complete": 1, "archive_bytes": 20},
    ]


@pytest.mark.asyncio
async def test_a_pre_v10_database_answers_without_growth_and_never_migrates(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots)
    _show("one", 100)
    assert run_sync(config) == 0
    with closing(connect(config.state_db)) as conn:
        conn.execute("DROP TABLE growth_snapshots")
        conn.execute("DROP TABLE run_activity")
        conn.execute("UPDATE schema_version SET version = 9")

    body = await _get(_app(config, _Clock()))

    assert body["growth"] == [] and body["disk"]["full_in_days"] is None
    [coll] = body["collections"]
    [status] = _status(config, capsys)
    assert coll["counts"] == status["counts"]
    assert coll["archive_bytes"] == status["bytes_on_disk"] == 100
    assert coll["last_successful_sync_at"] is not None
    with closing(sqlite3.connect(config.state_db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9


@pytest.mark.asyncio
async def test_library_view_is_shared_until_its_interval_passes(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    clock = _Clock()
    app = _app(config, clock)
    first = await _get(app)
    with closing(connect(config.state_db)) as conn:
        ItemRepo(conn).upsert(_item("coll1", "a", ItemStatus.COMPLETE))
    second = await _get(app)
    assert second["generated_at"] == first["generated_at"]
    assert second["totals"]["counts"]["complete"] == 0
    clock.now += VIEW_TTL_S
    assert (await _get(app))["totals"]["counts"]["complete"] == 1


@pytest.mark.asyncio
async def test_library_view_never_writes(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    _show("one", 100)
    assert run_sync(config) == 0

    def dump() -> list[str]:
        with closing(sqlite3.connect(config.state_db)) as conn:
            return list(conn.iterdump())

    before = dump()
    await _get(_app(config, _Clock()))
    assert dump() == before


@pytest.mark.asyncio
async def test_library_view_is_get_only(tmp_roots: tuple[Path, Path]) -> None:
    app = _app(make_config(*tmp_roots), _Clock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.post("/api/library")).status_code == 405
