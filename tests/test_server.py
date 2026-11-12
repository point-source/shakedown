"""Smoke test for shakedown serve. PRD §10."""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.db import connect
from shakedown.server import TOKEN_ENV, build_app
from shakedown.state import RescheduledRepo
from shakedown.sync import run_sync
from tests.conftest import make_config, write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin
from tests.serve_child import SPEC_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent
CHILD_PREFIX = (sys.executable, "-m", "tests.serve_child")
BUSY = "a run is in progress; try again when it finishes"
AUTH = {"Authorization": "Bearer test-token"}


def _app(config):  # type: ignore[no-untyped-def]
    """serve over a config file holding ``config`` (written beside the archive root)."""
    return build_app(write_config(config.archive_root.parent / "cfg.yaml", config))


def _child_app(config, *, items=None, gate=None):  # type: ignore[no-untyped-def]
    """serve whose triggered runs are fake-source child processes (tests.serve_child)."""
    root = config.archive_root.parent
    spec = root / "fake-spec.json"
    spec.write_text(json.dumps({"items": items or {}, "gate": str(gate) if gate else None}))
    return build_app(
        write_config(root / "cfg.yaml", config),
        argv_prefix=CHILD_PREFIX,
        child_env={SPEC_ENV: str(spec), "PYTHONPATH": str(REPO_ROOT)},
    )


async def _finish(app) -> None:  # type: ignore[no-untyped-def]
    """Wait until every run this serve started has exited and been counted."""
    for triggered in app.state.triggered:
        assert await asyncio.to_thread(triggered.done.wait, 30)


@pytest.mark.asyncio
async def test_serve_endpoints_smoke(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"audio")]
    )
    assert run_sync(config) == 0
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        RescheduledRepo(conn).record("fake-src", "coll1", datetime(2026, 11, 10, 3, 0, 0))

    app = _child_app(config, items={"gd-x": {"x.flac": "audio"}})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:

        # healthz: open
        r = await client.get("/healthz")
        assert r.status_code == 200 and r.json() == {"status": "ok"}

        # status: open, JSON
        r = await client.get("/status")
        assert r.status_code == 200
        body = r.json()
        assert body[0]["source"] == "fake-src"
        assert body[0]["counts"]["complete"] == 1
        # Same summary as `status --json` (§spec:library-ownership, §spec:library-cleanup).
        assert body[0]["separations"] == []
        assert body[0]["unplaced_files"] == []
        # A rescheduled sync (§spec:rescheduled-syncs).
        assert body[0]["rescheduled"] == {"since": "2026-11-10T03:00:00"}

        # metrics: open, prometheus text
        r = await client.get("/metrics")
        assert r.status_code == 200
        assert "shakedown_items_total" in r.text
        assert 'status="complete"' in r.text

        # sync: requires a bearer token
        assert (await client.post("/sync")).status_code == 401
        # a bare token (no Bearer scheme) is rejected
        assert (
            await client.post("/sync", headers={"Authorization": "test-token"})
        ).status_code == 401
        # a malformed (non-ASCII) token yields a clean 401, not a 500. Starlette
        # decodes header bytes as latin-1, so send raw bytes to reach that path.
        assert (
            await client.post(
                "/sync", headers={"Authorization": b"Bearer \xff"}  # type: ignore[dict-item]
            )
        ).status_code == 401
        r = await client.post("/sync", headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 202
        body = r.json()
        assert body["started"] == "sync" and len(body["run_ids"]) == 1
        await _finish(app)
        last_run = (await client.get("/status")).json()[0]["last_run"]
        assert last_run["run_id"] == body["run_ids"][0]
        assert last_run["items_new"] == 0  # idempotent: the separate run saw it mirrored

        # verify: requires a bearer token
        r = await client.post(
            "/verify?deep=true",
            headers={"Authorization": "Bearer test-token"},
        )
        assert r.status_code == 202
        assert r.json() == {
            "started": "verify", "source": None, "collection": None, "run_ids": [],
        }
        await _finish(app)


@pytest.mark.asyncio
async def test_serve_validate_endpoint(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"audio")]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(config)),
        base_url="http://testserver",
    ) as client:
        # default validation is read-only: no token, same posture as /status
        r = await client.post("/validate")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert any(g["collection"] == "coll1" for g in body["groups"])

        # a live handoff test is mutating: same bearer posture as sync/verify
        assert (
            await client.post("/validate", params={"live_handoff": True})
        ).status_code == 401
        r = await client.post(
            "/validate",
            params={"live_handoff": True},
            headers={"Authorization": "Bearer test-token"},
        )
        assert r.status_code == 200
        assert r.json()["ready"] is True


@pytest.mark.asyncio
async def test_serve_mutating_disabled_without_token(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """No token configured => mutating endpoints are disabled (503), not open,
    while read endpoints stay reachable."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    archive, library = tmp_roots
    config = make_config(archive, library)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(config)),
        base_url="http://testserver",
    ) as client:

        # read endpoints remain reachable
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/status")).status_code == 200
        assert (await client.get("/metrics")).status_code == 200
        # default (read-only) validation stays reachable
        assert (await client.post("/validate")).status_code == 200

        # mutating endpoints are disabled, not open -- even with a bearer header
        assert (
            await client.post("/sync", headers={"Authorization": "Bearer anything"})
        ).status_code == 503
        assert (await client.post("/verify")).status_code == 503
        # a live handoff test is mutating: disabled without a configured token
        assert (
            await client.post("/validate", params={"live_handoff": True})
        ).status_code == 503


@pytest.mark.asyncio
async def test_serve_unknown_collection_is_404(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    """A misspelled collection must not sync nothing and report success."""
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    auth = {"Authorization": "Bearer test-token"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(config)),
        base_url="http://testserver",
    ) as client:
        for route in ("/sync", "/verify"):
            r = await client.post(route, params={"collection": "nope"}, headers=auth)
            assert r.status_code == 404, route
            assert "coll1" in r.json()["detail"]
        r = await client.post("/validate", params={"collection": "nope"})
        assert r.status_code == 404
        r = await client.post("/sync", params={"source": "nope"}, headers=auth)
        assert r.status_code == 404
        assert "fake-src" in r.json()["detail"]
        # still authenticated first: no token, no config names
        assert (await client.post("/sync", params={"collection": "nope"})).status_code == 401


def test_api_reports_installed_package_version(tmp_roots: tuple[Path, Path]) -> None:
    from importlib.metadata import version

    archive, library = tmp_roots
    app = _app(make_config(archive, library))
    assert app.version == version("shakedown")


@pytest.mark.asyncio
async def test_status_and_metrics_read_sizes_from_the_db(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """A scrape never walks the archive (hundreds of thousands of files on a NAS):
    sizes come from the recorded manifests, and every request closes its DB
    connection."""
    import os
    import sqlite3

    import shakedown.server as server_module

    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x",
        files=[FakeFile(name="x.flac", content=b"audio"), FakeFile(name="y.flac", content=b"ab")],
    )
    FakePlugin.items["gd-sbd"] = FakeItem(
        identifier="gd-sbd", files=[FakeFile(name="s.flac", content=b"s")],
        is_restricted=True, restriction_reason="stream-only",
    )
    assert run_sync(config) == 0

    def no_walk(*_args, **_kwargs):
        raise AssertionError("the archive must not be walked per request")

    monkeypatch.setattr(Path, "rglob", no_walk)
    monkeypatch.setattr(os, "walk", no_walk)
    monkeypatch.setattr(os, "scandir", no_walk)
    from shakedown.state import ItemRepo

    def no_full_rows(*_args, **_kwargs):
        raise AssertionError("a scrape must not load every item's metadata")

    monkeypatch.setattr(ItemRepo, "list_for_collection", no_full_rows)
    opened: list[sqlite3.Connection] = []
    real_connect = server_module.connect

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(server_module, "connect", tracking_connect)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(config)),
        base_url="http://testserver",
    ) as client:
        r = await client.get("/status")
        assert r.status_code == 200
        assert r.json()[0]["bytes_on_disk"] == 7
        assert r.json()[0]["restricted"] == [{"identifier": "gd-sbd", "reason": "stream-only"}]
        r = await client.get("/metrics")
        assert r.status_code == 200
        assert 'shakedown_bytes_on_disk{collection="coll1",source="fake-src"} 7.0' in r.text

    assert len(opened) == 2
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


@pytest.mark.asyncio
async def test_endpoints_read_the_config_as_it_is_at_each_request(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """A config edit takes effect on the next request; a broken config is an answer
    naming the key, not a crash; fixing the file restores the endpoints without a
    restart (§spec:api-sync-runs)."""
    from shakedown.config import CollectionConfig

    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    cfg_path = write_config(archive.parent / "cfg.yaml", config)
    auth = {"Authorization": "Bearer test-token"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(cfg_path)),
        base_url="http://testserver",
    ) as client:
        assert [s["collection"] for s in (await client.get("/status")).json()] == ["coll1"]

        config.sources[0].collections.append(CollectionConfig(name="coll2", query="*"))
        write_config(cfg_path, config)
        r = await client.get("/status")
        assert [s["collection"] for s in r.json()] == ["coll1", "coll2"]
        r = await client.post("/validate", params={"collection": "coll2"})
        assert r.status_code == 200  # the scope check sees the new collection too

        good = cfg_path.read_text()
        cfg_path.write_text(good + "max_concurrent_collections: 0\n")
        for method, route, headers in (
            ("GET", "/status", {}),
            ("GET", "/metrics", {}),
            ("POST", "/validate", {}),
            ("POST", "/sync", auth),
            ("POST", "/verify", auth),
        ):
            r = await client.request(method, route, headers=headers)
            assert r.status_code == 503, route
            assert "max_concurrent_collections" in r.json()["detail"], route
        assert (await client.get("/healthz")).status_code == 200  # serve stays up
        # auth still comes first: no token, no config error
        assert (await client.post("/sync")).status_code == 401

        cfg_path.write_text(good)
        assert (await client.get("/status")).status_code == 200
        assert (await client.get("/metrics")).status_code == 200


@pytest.mark.asyncio
async def test_metrics_drop_a_collection_removed_from_the_config(
    tmp_roots: tuple[Path, Path],
) -> None:
    from shakedown.config import CollectionConfig

    archive, library = tmp_roots
    config = make_config(archive, library)
    config.sources[0].collections.append(CollectionConfig(name="coll2", query="*"))
    cfg_path = write_config(archive.parent / "cfg.yaml", config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build_app(cfg_path)),
        base_url="http://testserver",
    ) as client:
        assert 'collection="coll2"' in (await client.get("/metrics")).text
        config.sources[0].collections.pop()
        write_config(cfg_path, config)
        assert 'collection="coll2"' not in (await client.get("/metrics")).text


# --- triggered runs are fresh one-shot runs on the current config ------------------

@pytest.mark.asyncio
async def test_sync_starts_a_fresh_run_on_the_config_as_edited(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """Change a collection's layout with serve running, then trigger it: 202 at once
    with the scope and run ids, the run stages under the new layout, and /status
    shows that run (§spec:api-sync-runs user-level test path)."""
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    app = _child_app(config, items={"gd-x": {"x.flac": "audio"}})
    config.sources[0].collections[0].library_layout = "renamed/{identifier}"
    write_config(archive.parent / "cfg.yaml", config)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.post("/sync", params={"collection": "coll1"}, headers=AUTH)
        assert r.status_code == 202
        body = r.json()
        assert body == {
            "started": "sync", "source": None, "collection": "coll1",
            "run_ids": [body["run_ids"][0]],
        }
        await _finish(app)
        assert (library / "fake-src" / "coll1" / "renamed" / "gd-x" / "x.flac").is_file()
        last_run = (await client.get("/status")).json()[0]["last_run"]
        assert last_run["run_id"] == body["run_ids"][0]
        assert last_run["finished_at"] is not None and last_run["items_new"] == 1
        assert 'shakedown_sync_runs_total{result="ok"} 1.0' in (
            await client.get("/metrics")
        ).text


@pytest.mark.asyncio
async def test_sync_while_a_run_holds_the_lock_is_refused_and_nothing_is_queued(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    import sqlite3

    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    gate = archive.parent / "go"
    app = _child_app(config, items={"gd-x": {"x.flac": "audio"}}, gate=gate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        try:
            assert (await client.post("/sync", headers=AUTH)).status_code == 202
            r = await client.post("/sync", headers=AUTH)
            assert r.status_code == 409
            assert r.json()["detail"] == BUSY
        finally:
            gate.touch()
        await _finish(app)
        assert len(app.state.triggered) == 1
        metrics = (await client.get("/metrics")).text
    assert 'shakedown_sync_runs_total{result="ok"} 1.0' in metrics
    assert 'result="fail"' not in metrics  # a refusal is not a run
    assert config.state_db is not None
    conn = sqlite3.connect(config.state_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_sync_is_refused_while_a_cli_run_holds_the_lock(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """The real child (`python -m shakedown`) meets a run started elsewhere."""
    from shakedown.config import CollectionConfig, Config, SourceConfig
    from shakedown.lock import writer_lock

    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = Config(
        archive_root=archive,
        library_root=library,
        sources=[SourceConfig(
            name="ia", type="ia",
            collections=[CollectionConfig(name="gd", query="collection:GratefulDead")],
        )],
    )
    app = build_app(write_config(archive.parent / "cfg.yaml", config))
    assert config.state_db is not None
    connect(config.state_db).close()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        with writer_lock(config):
            r = await client.post("/sync", headers=AUTH)
            assert (r.status_code, r.json()["detail"]) == (409, BUSY)
            # A refused API sync is not rescheduled (§spec:rescheduled-syncs).
            r = await client.post("/sync", params={"collection": "gd"}, headers=AUTH)
            assert (r.status_code, r.json()["detail"]) == (409, BUSY)
            r = await client.post(
                "/verify", params={"deep": True, "reconform": True}, headers=AUTH
            )
            assert (r.status_code, r.json()["detail"]) == (409, BUSY)
    with closing(connect(config.state_db)) as conn:
        assert RescheduledRepo(conn).get("ia", "gd") is None


@pytest.mark.asyncio
async def test_a_killed_run_is_closed_and_counted_as_failed(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    import signal

    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    gate = archive.parent / "go"
    app = _child_app(config, items={"gd-x": {"x.flac": "audio"}}, gate=gate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.post("/sync", headers=AUTH)
        assert r.status_code == 202
        app.state.triggered[0].process.send_signal(signal.SIGKILL)
        await _finish(app)
        last_run = (await client.get("/status")).json()[0]["last_run"]
        assert last_run["run_id"] == r.json()["run_ids"][0]
        assert last_run["finished_at"] is not None
        assert "run ended unexpectedly (killed by SIGKILL)" in last_run["errors"]
        metrics = (await client.get("/metrics")).text
    assert 'shakedown_sync_runs_total{result="fail"} 1.0' in metrics


@pytest.mark.asyncio
async def test_reconform_without_deep_is_a_bad_request(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(make_config(archive, library))),
        base_url="http://testserver",
    ) as client:
        r = await client.post("/verify", params={"reconform": True}, headers=AUTH)
        assert r.status_code == 400
        assert "deep" in r.json()["detail"]


@pytest.mark.asyncio
async def test_status_carries_the_pending_handoff(tmp_roots: tuple[Path, Path]) -> None:
    """GET /status carries the same pending handoff as `status --json`
    (§spec:durable-handoff), pending identifiers included."""
    from unittest.mock import patch

    from shakedown.config import WebhookHandoff

    archive, library = tmp_roots
    config = make_config(
        archive, library, on_complete=WebhookHandoff(webhook="http://down.example/hook")
    )
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"audio")]
    )

    def refused(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    with patch("shakedown.notify.httpx.post", side_effect=refused):
        assert run_sync(config) == 0

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(config)), base_url="http://testserver"
    ) as client:
        pending = (await client.get("/status")).json()[0]["handoff_pending"]

    assert pending["shows"] == 1
    assert pending["identifiers"] == ["gd-x"]
    assert pending["last_reason"] == "webhook unreachable: connection refused"


@pytest.mark.asyncio
async def test_status_lists_shows_with_no_playable_copy(tmp_roots: tuple[Path, Path]) -> None:
    """GET /status carries the same not-playable list as `status --json`
    (§spec:quality-selection)."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-shn"] = FakeItem(
        identifier="gd-shn", files=[FakeFile(name="t.shn", content=b"shn", format="Shorten")]
    )
    assert run_sync(config) == 0

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(config)),
        base_url="http://testserver",
    ) as client:
        r = await client.get("/status")
    assert r.status_code == 200
    assert r.json()[0]["not_playable"] == [
        {"identifier": "gd-shn", "reason": "only in SHN; archive.org offers no playable copy"}
    ]
    assert r.json()[0]["restricted"] == []


@pytest.mark.asyncio
async def test_status_lists_shows_removed_upstream_from_the_db(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """GET /status carries the same removed-upstream list as `status --json`
    (§spec:item-lifecycle), read without walking the archive or loading every item."""
    import os
    from datetime import date

    from shakedown.state import ItemRepo

    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-gone"] = FakeItem(
        identifier="gd-gone", files=[FakeFile(name="g.flac", content=b"g")]
    )
    assert run_sync(config) == 0
    FakePlugin.items.clear()
    before = date.today()
    assert run_sync(config) == 0
    after = date.today()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("a scrape must neither walk the archive nor load every item")

    for target, name in ((Path, "rglob"), (os, "walk"), (os, "scandir"),
                         (ItemRepo, "list_for_collection")):
        monkeypatch.setattr(target, name, forbidden)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(config)),
        base_url="http://testserver",
    ) as client:
        r = await client.get("/status")
    assert r.status_code == 200
    [removed] = r.json()[0]["removed_upstream"]
    assert removed["identifier"] == "gd-gone"
    assert removed["removed_on"] in {before.isoformat(), after.isoformat()}
