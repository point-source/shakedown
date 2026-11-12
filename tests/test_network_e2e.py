"""Network activity end to end (§spec:network-activity "User-level test path"): a sync
records its network activity with `serve` stopped, and `serve` shows it afterwards —
for any source, through the same endpoints."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

import shakedown.activity as activity
import shakedown.sync as sync_module
from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.dashboard import LIVE_TTL_S
from shakedown.models import ManifestFile
from shakedown.plugins.base import FetchResult
from shakedown.server import build_app
from shakedown.status import print_status
from shakedown.sync import run_sync
from tests.conftest import make_config, write_config
from tests.fake_archive import Reply, fake_archive, json_reply
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin
from tests.test_server import _Clock

SEARCH = "/services/search/v1/scrape"
SHOW_A = b"a" * 3000
SHOW_B = b"b" * 2000


def _file(name: str, content: bytes) -> dict[str, Any]:
    return {"name": name, "size": str(len(content)), "md5": hashlib.md5(content).hexdigest(),
            "format": "Flac"}


def _ia_config(tmp_roots: tuple[Path, Path]) -> Config:
    archive, library = tmp_roots
    return Config(
        archive_root=archive,
        library_root=library,
        sources=[SourceConfig(name="ia", type="ia", collections=[
            CollectionConfig(name="gd", query="collection:GratefulDead"),
        ])],
    )


def _app(config: Config, clock: _Clock | None = None):  # type: ignore[no-untyped-def]
    return build_app(write_config(config.archive_root.parent / "cfg.yaml", config),
                     clock=clock or _Clock())


async def _get(app, path: str) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.get(path)
        assert r.status_code == 200, r.text
        return r.json()


def _last_run(config: Config, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    print_status(config, as_json=True)
    [summary] = json.loads(capsys.readouterr().out)
    return summary["last_run"]


def _minute_of(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=None, second=0, microsecond=0)


@pytest.mark.asyncio
async def test_a_throttled_sync_shows_its_series_hits_and_back_off(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """serve stopped; a sync meets 429s with Retry-After on its listing and on a
    download. serve then shows the series, the hits, and the back-off at the minutes
    they happened, and the series sums to the run's bytes_downloaded."""
    monkeypatch.setattr(sync_module.time, "sleep", lambda _s: None)
    config = _ia_config(tmp_roots)
    with fake_archive() as archive:
        archive.route(
            SEARCH,
            Reply(429, b"slow down", {"Retry-After": "2"}),
            json_reply({"items": [{"identifier": "gd1"}, {"identifier": "gd2"}],
                        "count": 2, "total": 2}),
        )
        archive.item("gd1", [_file("a.flac", SHOW_A)])
        archive.file("gd1", "a.flac", Reply(429, b"slow down", {"Retry-After": "5"}),
                     Reply(200, SHOW_A))
        archive.item("gd2", [_file("b.flac", SHOW_B)])
        archive.file("gd2", "b.flac", Reply(200, SHOW_B))
        assert run_sync(config) == 0

    body = await _get(_app(config), "/api/network?days=1")

    [ia] = body["sources"]
    series = ia["series"]
    last = _last_run(config, capsys)
    assert last["bytes_downloaded"] == len(SHOW_A) + len(SHOW_B)
    assert sum(p["bytes"] for p in series) == last["bytes_downloaded"]
    assert sum(p["rate_limited"] for p in series) == 2
    assert sum(p["retries"] for p in series) == 2
    assert sum(p["backoff_seconds"] for p in series) == 2 + 5
    assert max(p["peak_requests_in_flight"] for p in series) >= 1

    events = ia["events"]
    hits = [e for e in events if e["kind"] == "rate_limited"]
    assert sorted((e["identifier"] or "", e["http_status"]) for e in hits) == [
        ("", 429), ("gd1", 429),
    ]
    assert all(e["collection"] == "gd" for e in events)
    assert sorted(e["wait_seconds"] for e in events if e["kind"] == "backoff") == [2.0, 5.0]
    # Each hit is at a minute whose bucket counts it.
    by_minute = {_minute_of(p["t"]): p for p in series}
    for hit in hits:
        assert by_minute[_minute_of(hit["at"])]["rate_limited"] >= 1
    assert ia["events_total"] == len(events)


def test_a_resumed_show_does_not_count_its_kept_files(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted show resumed on the next sync: the file it kept is not
    downloaded again and not counted in bytes_downloaded."""
    monkeypatch.setattr(sync_module.time, "sleep", lambda _s: None)
    config = _ia_config(tmp_roots)
    tmp = config.archive_root / "ia" / "gd" / ".tmp-gd1"
    tmp.mkdir(parents=True)
    (tmp / "a.flac").write_bytes(SHOW_A)  # finished before the interruption
    with fake_archive() as archive:
        archive.search_results({"identifier": "gd1"})
        archive.item("gd1", [_file("a.flac", SHOW_A), _file("b.flac", SHOW_B)])
        archive.file("gd1", "a.flac", Reply(200, SHOW_A))
        archive.file("gd1", "b.flac", Reply(200, SHOW_B))
        assert run_sync(config) == 0
        assert archive.calls_to("/download/gd1/a.flac") == []

    assert _last_run(config, capsys)["bytes_downloaded"] == len(SHOW_B)


@pytest.mark.asyncio
async def test_during_a_sync_the_live_view_shows_speed_and_requests_in_flight(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(activity, "TICK_S", 0.05)
    config = make_config(*tmp_roots)
    FakePlugin.items["gd-a"] = FakeItem(
        "gd-a", files=[FakeFile("gd-a.flac", b"x" * 4000, format="Flac")]
    )
    entered, release = threading.Event(), threading.Event()
    real_fetch = FakePlugin.fetch

    def held_fetch(self, identifier: str, files: Sequence[ManifestFile],
                   dest_dir: Path) -> FetchResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "gd-a.flac").write_bytes(b"x" * 1000)  # a quarter is in
        entered.set()
        assert release.wait(20)
        return real_fetch(self, identifier, files, dest_dir)

    monkeypatch.setattr(FakePlugin, "fetch", held_fetch)
    clock = _Clock()
    app = _app(config, clock)
    runner = threading.Thread(target=lambda: run_sync(config))
    runner.start()
    try:
        assert entered.wait(10)
        network: list[dict[str, Any]] = []

        async def poll() -> bool:
            clock.now += LIVE_TTL_S
            network[:] = (await _get(app, "/api/activity"))["network"]
            return bool(network) and network[0]["speed_bytes_per_second"] > 0 \
                and network[0]["requests_in_flight"] >= 1

        deadline = time.monotonic() + 10
        while not await poll():
            assert time.monotonic() < deadline, f"no live network activity: {network}"
            time.sleep(0.05)
        assert network[0]["source"] == "fake-src"
    finally:
        release.set()
        runner.join(20)


@pytest.mark.asyncio
async def test_a_source_block_of_any_plugin_appears_with_no_dashboard_code(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A third source block, using the test plugin, shows its activity like the
    shipped plugins' blocks (§spec:network-activity "Measured by the core")."""
    config = make_config(*tmp_roots)
    config.sources[:0] = [
        SourceConfig(name="ia", type="ia",
                     collections=[CollectionConfig(name="gd", query="collection:gd")]),
        SourceConfig(name="etree-lma", type="etree",
                     collections=[CollectionConfig(name="lma", query="collection:etree")]),
    ]
    FakePlugin.items["show"] = FakeItem("show", files=[FakeFile("s.flac", b"s" * 1234,
                                                                format="Flac")])
    assert run_sync(config, source_filter="fake-src") == 0

    body = await _get(_app(config), "/api/network")

    sources = {s["source"]: s for s in body["sources"]}
    assert list(sources) == ["ia", "etree-lma", "fake-src"]
    assert sources["ia"]["series"] == [] and sources["etree-lma"]["series"] == []
    assert sum(p["bytes"] for p in sources["fake-src"]["series"]) == 1234
