"""Smoke test for shakedown serve. PRD §10."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.db import connect
from shakedown.server import build_app
from shakedown.state import RescheduledRepo
from shakedown.sync import run_sync
from shakedown.write_gate import TOKEN_ENV
from tests.conftest import make_config, write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin
from tests.serve_child import SPEC_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent
CHILD_PREFIX = (sys.executable, "-m", "tests.serve_child")
BUSY = "a run is in progress; try again when it finishes"
AUTH = {"Authorization": "Bearer test-token"}
# What the dashboard page's fetch carries (§spec:dashboard-run-controls).
DASHBOARD = {"Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}
CROSS_SITE = {"code": "cross_site"}


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

        # sync: passes the write gate (tests/test_write_gate.py has every case)
        r = await client.post("/sync")
        assert (r.status_code, r.json()) == (403, CROSS_SITE)
        r = await client.post("/sync", headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 202
        assert r.json() == {"started": "sync", "source": None, "collection": None}
        await _finish(app)
        [opened] = app.state.triggered[0].runs
        last_run = (await client.get("/status")).json()[0]["last_run"]
        assert last_run["run_id"] == opened["run_id"]
        assert last_run["items_new"] == 0  # idempotent: the separate run saw it mirrored

        # verify: passes the write gate too
        r = await client.post(
            "/verify?deep=true",
            headers={"Authorization": "Bearer test-token"},
        )
        assert r.status_code == 202
        assert r.json() == {"started": "verify", "source": None, "collection": None}
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
        # default validation records outcomes: the write gate, which the dashboard
        # page passes without a token
        assert (await client.post("/validate")).status_code == 403
        r = await client.post("/validate", headers=DASHBOARD)
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert any(g["collection"] == "coll1" for g in body["groups"])

        # a live handoff test can run the configured command: the token only
        assert (
            await client.post("/validate", params={"live_handoff": True}, headers=DASHBOARD)
        ).status_code == 403
        r = await client.post(
            "/validate",
            params={"live_handoff": True},
            headers={"Authorization": "Bearer test-token"},
        )
        assert r.status_code == 200
        assert r.json()["ready"] is True


@pytest.mark.asyncio
async def test_without_a_token_only_the_dashboard_page_writes(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """No token configured: scripts are refused, the dashboard's own page still
    writes, and read endpoints stay open (§spec:dashboard-run-controls)."""
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
        # the dashboard page validates without a token
        assert (await client.post("/validate", headers=DASHBOARD)).status_code == 200

        # scripts are refused, even with a bearer header
        r = await client.post("/sync", headers={"Authorization": "Bearer anything"})
        assert (r.status_code, r.json()) == (403, CROSS_SITE)
        assert (await client.post("/verify")).status_code == 403
        # a live handoff test takes the token only: none configured, none passes
        assert (
            await client.post("/validate", params={"live_handoff": True}, headers=DASHBOARD)
        ).status_code == 403


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
        r = await client.post("/validate", params={"collection": "nope"}, headers=auth)
        assert r.status_code == 404
        r = await client.post("/sync", params={"source": "nope"}, headers=auth)
        assert r.status_code == 404
        assert "fake-src" in r.json()["detail"]
        # the write gate still comes first: no token, no config names
        assert (await client.post("/sync", params={"collection": "nope"})).status_code == 403


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
    real_connect = server_module.connect_reader

    def tracking_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(server_module, "connect_reader", tracking_connect)

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
async def test_status_and_metrics_read_an_older_schema_without_migrating_it(
    tmp_roots: tuple[Path, Path],
) -> None:
    """serve never migrates the database: GET /status and /metrics while the previous
    release still writes leave it at its version, with no rescheduled entries
    (Q-1-1, operator 2026-11-12)."""
    import sqlite3

    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"audio")]
    )
    assert run_sync(config) == 0
    assert config.state_db is not None
    with closing(sqlite3.connect(config.state_db)) as conn:
        conn.execute("DROP TABLE rescheduled_syncs")
        conn.execute("UPDATE schema_version SET version = 8")
        conn.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(config)), base_url="http://testserver"
    ) as client:
        r = await client.get("/status")
        assert r.status_code == 200
        assert r.json()[0]["counts"]["complete"] == 1
        assert r.json()[0]["rescheduled"] is None
        r = await client.get("/metrics")
        assert r.status_code == 200
        assert 'shakedown_bytes_on_disk{collection="coll1",source="fake-src"} 5.0' in r.text

    with closing(sqlite3.connect(config.state_db)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 8


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
        r = await client.post("/validate", params={"collection": "coll2"}, headers=auth)
        assert r.status_code == 200  # the scope check sees the new collection too

        good = cfg_path.read_text()
        cfg_path.write_text(good + "max_concurrent_collections: 0\n")
        for method, route, headers in (
            ("GET", "/status", {}),
            ("GET", "/metrics", {}),
            ("POST", "/validate", auth),
            ("POST", "/sync", auth),
            ("POST", "/verify", auth),
        ):
            r = await client.request(method, route, headers=headers)
            assert r.status_code == 503, route
            assert "max_concurrent_collections" in r.json()["detail"], route
        assert (await client.get("/healthz")).status_code == 200  # serve stays up
        # the write gate still comes first: no token, no config error
        assert (await client.post("/sync")).status_code == 403

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
    with the scope, the run stages under the new layout, and /status shows that run
    (§spec:api-sync-runs user-level test path)."""
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
        assert r.json() == {"started": "sync", "source": None, "collection": "coll1"}
        await _finish(app)
        assert (library / "fake-src" / "coll1" / "renamed" / "gd-x" / "x.flac").is_file()
        last_run = (await client.get("/status")).json()[0]["last_run"]
        assert last_run["run_id"] == app.state.triggered[0].runs[0]["run_id"]
        assert last_run["finished_at"] is not None and last_run["items_new"] == 1
        assert 'shakedown_sync_runs_total{result="ok"} 1.0' in (
            await client.get("/metrics")
        ).text


@pytest.mark.asyncio
async def test_triggered_sync_catches_up_rescheduled_collections_after_its_own(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """A triggered sync catches up like any sync (§spec:api-sync-runs,
    §spec:rescheduled-syncs): the catch-up shows in /status as that collection's
    latest run, its entry gone, and serve learns of both runs' records."""
    from shakedown.config import CollectionConfig

    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    config.sources[0].collections.append(CollectionConfig(name="coll2", query="*"))
    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        RescheduledRepo(conn).record("fake-src", "coll2", datetime(2026, 11, 10, 3, 0, 0))
    app = _child_app(config, items={"gd-x": {"x.flac": "audio"}})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.post("/sync", params={"collection": "coll1"}, headers=AUTH)
        assert r.status_code == 202
        await _finish(app)
        opened = {o["collection"]: o["run_id"] for o in app.state.triggered[0].runs}
        status = {c["collection"]: c for c in (await client.get("/status")).json()}
        metrics = (await client.get("/metrics")).text
    assert status["coll1"]["last_run"]["run_id"] == opened["coll1"]
    caught_up = status["coll2"]["last_run"]
    assert caught_up["run_id"] == opened["coll2"]
    assert caught_up["kind"] == "catch_up" and caught_up["finished_at"] is not None
    assert caught_up["items_new"] == 1
    assert status["coll1"]["rescheduled"] is None and status["coll2"]["rescheduled"] is None
    assert 'shakedown_sync_runs_total{result="ok"} 1.0' in metrics


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
        triggered = app.state.triggered[0]
        # Its collection has started: serve knows its run record.
        deadline = time.monotonic() + 30
        while not triggered.runs:
            assert time.monotonic() < deadline, "the run record never opened"
            await asyncio.sleep(0.02)
        triggered.process.send_signal(signal.SIGKILL)
        await _finish(app)
        last_run = (await client.get("/status")).json()[0]["last_run"]
        assert last_run["run_id"] == triggered.runs[0]["run_id"]
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


# --- dashboard read endpoints: shared answers, GET only, no CORS (§spec:dashboard) --

class _Clock:
    """A monotonic clock the test moves by hand, so an interval passes without sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def run_now(work: Callable[[], None]) -> None:
    """Refresh a dashboard answer inside the request that found it expired, so a test
    reads the new answer as soon as the interval passes. `serve` refreshes on a
    thread and serves the old answer meanwhile (tests/test_shared_answers.py)."""
    work()


def _probe_app(config, clock, *, append: str = ""):  # type: ignore[no-untyped-def]
    """serve with one extra dashboard view that counts how often it is computed,
    over a config file holding ``config`` followed by ``append``."""
    from shakedown.dashboard import VIEW_TTL_S, register_api

    cfg_path = write_config(config.archive_root.parent / "cfg.yaml", config)
    cfg_path.write_text(cfg_path.read_text() + append)
    app = build_app(cfg_path, clock=clock, spawn=run_now)
    app.state.probe_builds = 0

    def probe(ctx):  # type: ignore[no-untyped-def]
        app.state.probe_builds += 1
        tables = ctx.conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        return {"collections": [c.name for s in ctx.config.sources for c in s.collections],
                "tables": tables}

    register_api(app, "/api/probe", VIEW_TTL_S, probe)
    return app


@pytest.mark.asyncio
async def test_api_answer_is_shared_until_its_interval_passes(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Ten viewers cost what one does: within the refresh interval every request gets
    the one computed answer, with the same generated_at; after it, a fresh one."""
    from shakedown.dashboard import VIEW_TTL_S

    archive, library = tmp_roots
    clock = _Clock()
    app = _probe_app(make_config(archive, library), clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        first = (await client.get("/api/probe")).json()
        assert first["collections"] == ["coll1"] and first["tables"] > 0
        assert datetime.fromisoformat(first["generated_at"]).tzinfo is not None
        clock.now = VIEW_TTL_S - 0.1
        assert (await client.get("/api/probe")).json() == first
        assert app.state.probe_builds == 1

        clock.now = VIEW_TTL_S
        second = (await client.get("/api/probe")).json()
        assert app.state.probe_builds == 2
        assert second["generated_at"] != first["generated_at"]


@pytest.mark.asyncio
async def test_api_views_are_get_only_and_send_no_cors_allowance(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    app = _probe_app(make_config(archive, library), _Clock())
    origin = {"Origin": "https://elsewhere.example"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.get("/api/probe", headers=origin)
        assert r.status_code == 200
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]
        assert (await client.post("/api/probe")).status_code == 405
        r = await client.options(
            "/api/probe", headers={**origin, "Access-Control-Request-Method": "GET"}
        )
        assert r.status_code == 405
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]


def test_iso_carries_the_utc_offset() -> None:
    from shakedown.dashboard import iso

    stamp = iso(datetime(2026, 11, 12, 3, 0, 0))
    assert stamp is not None
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert parsed.replace(tzinfo=None) == datetime(2026, 11, 12, 3, 0, 0)
    assert iso(None) is None


# --- dashboard views on a broken config (§spec:dashboard "When the config is broken")

TYPO = "archive_rot: /typo\n"  # a misspelled key: the config fails to load, naming it


@pytest.mark.asyncio
async def test_api_answers_from_the_last_good_config_while_the_file_is_broken(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Each view reads the config at the request; while the file is invalid it keeps
    answering from the last config this serve loaded, and the other endpoints keep
    their 503 naming the key."""
    from shakedown.config import CollectionConfig
    from shakedown.dashboard import VIEW_TTL_S

    archive, library = tmp_roots
    config = make_config(archive, library)
    clock = _Clock()
    app = _probe_app(config, clock)
    cfg_path = archive.parent / "cfg.yaml"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/probe")).json()["collections"] == ["coll1"]

        config.sources[0].collections.append(CollectionConfig(name="coll2", query="*"))
        write_config(cfg_path, config)
        clock.now += VIEW_TTL_S
        assert (await client.get("/api/probe")).json()["collections"] == ["coll1", "coll2"]

        good = cfg_path.read_text()
        cfg_path.write_text(good + TYPO)
        clock.now += VIEW_TTL_S
        r = await client.get("/api/probe")
        assert r.status_code == 200
        assert r.json()["collections"] == ["coll1", "coll2"]
        assert app.state.probe_builds == 3
        r = await client.get("/status")
        assert r.status_code == 503 and "archive_rot" in r.json()["detail"]


@pytest.mark.asyncio
async def test_api_on_a_serve_that_never_loaded_a_valid_config_is_503(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library)
    app = _probe_app(config, _Clock(), append=TYPO)
    cfg_path = archive.parent / "cfg.yaml"
    good = cfg_path.read_text().removesuffix(TYPO)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.get("/api/probe")
        assert r.status_code == 503
        assert "archive_rot" in r.json()["detail"]
        # a failure is never the cached answer: fixed file, next request answers
        cfg_path.write_text(good)
        assert (await client.get("/api/probe")).status_code == 200


# --- GET /api/health (§spec:dashboard-health) ---------------------------------------

def _health_app(config, clock, *, append: str = ""):  # type: ignore[no-untyped-def]
    cfg_path = write_config(config.archive_root.parent / "cfg.yaml", config)
    cfg_path.write_text(cfg_path.read_text() + append)
    return build_app(cfg_path, clock=clock, spawn=run_now)


@pytest.mark.asyncio
async def test_api_health_reports_release_config_and_last_validation(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Release, config valid, validation null; after `validate`, each collection's
    outcome and time once the refresh interval passes. Rapid requests share one
    answer; the view is GET only and sends no CORS allowance."""
    from importlib.metadata import version

    from shakedown.dashboard import VIEW_TTL_S

    archive, library = tmp_roots
    config = make_config(archive, library)
    clock = _Clock()
    app = _health_app(config, clock)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.get("/api/health", headers={"Origin": "https://elsewhere.example"})
        assert r.status_code == 200
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]
        body = r.json()
        assert set(body) == {"generated_at", "release", "config", "validation"}
        assert body["release"] == version("shakedown")
        assert body["config"]["valid"] is True and body["config"]["error"] is None
        assert datetime.fromisoformat(body["config"]["last_loaded_at"]).tzinfo is not None
        assert body["validation"] == [
            {"source": "fake-src", "collection": "coll1", "status": None, "at": None}
        ]
        assert (await client.get("/api/health")).json() == body
        assert (await client.post("/api/health")).status_code == 405

        assert (await client.post("/validate", headers=DASHBOARD)).json()["ready"] is True
        assert (await client.get("/api/health")).json() == body  # same shared answer
        clock.now += VIEW_TTL_S
        after = (await client.get("/api/health")).json()
        assert after["generated_at"] != body["generated_at"]
        [entry] = after["validation"]
        assert entry["status"] == "completed"
        assert datetime.fromisoformat(entry["at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_api_health_shows_a_failed_validation(tmp_roots: tuple[Path, Path]) -> None:
    from shakedown.models import OperationStatus, OperationType
    from shakedown.state import OperationOutcomeRepo

    archive, library = tmp_roots
    config = make_config(archive, library)
    assert config.state_db is not None
    at = datetime(2026, 11, 12, 3, 0, 0)
    with closing(connect(config.state_db)) as conn:
        outcomes = OperationOutcomeRepo(conn)
        outcome = outcomes.start(OperationType.VALIDATE, "fake-src", "coll1", at)
        outcomes.finish(outcome, OperationStatus.FAILED_BEFORE_COMPLETION, at)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_health_app(config, _Clock())),
        base_url="http://testserver",
    ) as client:
        [entry] = (await client.get("/api/health")).json()["validation"]
    assert entry["status"] == "failed_before_completion"
    assert datetime.fromisoformat(entry["at"]).replace(tzinfo=None) == at


@pytest.mark.asyncio
async def test_api_health_reports_a_broken_config_and_when_it_last_loaded(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A typo'd key: 200 with config.valid false, the error naming the key, and the
    time of the last good load. A fresh serve on the broken file: 503 naming it."""
    from shakedown.dashboard import VIEW_TTL_S

    archive, library = tmp_roots
    config = make_config(archive, library)
    clock = _Clock()
    app = _health_app(config, clock)
    cfg_path = archive.parent / "cfg.yaml"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        loaded_at = (await client.get("/api/health")).json()["config"]["last_loaded_at"]
        cfg_path.write_text(cfg_path.read_text() + TYPO)
        clock.now += VIEW_TTL_S
        r = await client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["config"]["valid"] is False
        assert "archive_rot" in body["config"]["error"]
        assert body["config"]["last_loaded_at"] == loaded_at
        assert [v["collection"] for v in body["validation"]] == ["coll1"]

    fresh = _health_app(config, _Clock(), append=TYPO)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fresh), base_url="http://testserver"
    ) as client:
        r = await client.get("/api/health")
        assert r.status_code == 503
        assert "archive_rot" in r.json()["detail"]


@pytest.mark.asyncio
async def test_api_health_never_writes_or_waits_for_the_writer_lock(
    tmp_roots: tuple[Path, Path],
) -> None:
    """Answering reads only: it answers while a run holds the writer lock, and the
    state database holds exactly what it held. (Compared by content: closing a
    reader may checkpoint SQLite's WAL into the main file, which changes bytes but
    no data.)"""
    import sqlite3

    from shakedown.lock import writer_lock

    archive, library = tmp_roots
    config = make_config(archive, library)
    assert run_sync(config) == 0
    db = config.state_db
    assert db is not None

    def dump() -> list[str]:
        with closing(sqlite3.connect(db)) as conn:
            return list(conn.iterdump())

    before = dump()
    app = _health_app(config, _Clock())
    with writer_lock(config):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/api/health")).status_code == 200
    assert dump() == before


@pytest.mark.asyncio
async def test_api_health_contains_no_token_or_credential(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """Operational data only (§spec:dashboard): neither the API token, the source
    credentials in env, nor a secret in the config file reaches the answer — not
    even in the error for a broken file."""
    from shakedown.config import WebhookHandoff
    from shakedown.dashboard import VIEW_TTL_S

    secret = "SECRET-7f3a"
    monkeypatch.setenv(TOKEN_ENV, f"token-{secret}")
    monkeypatch.setenv("IA_EMAIL", f"email-{secret}")
    monkeypatch.setenv("IA_PASSWORD", f"password-{secret}")
    archive, library = tmp_roots
    config = make_config(
        archive, library, on_complete=WebhookHandoff(webhook=f"https://hooks.example/{secret}")
    )
    clock = _Clock()
    app = _health_app(config, clock)
    cfg_path = archive.parent / "cfg.yaml"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.post("/validate", headers=DASHBOARD)).status_code == 200
        clock.now += VIEW_TTL_S
        r = await client.get("/api/health")
        assert r.status_code == 200 and secret not in r.text
        broken = cfg_path.read_text().replace("webhook:", "webhok:")
        assert "webhok" in broken
        cfg_path.write_text(broken)
        clock.now += VIEW_TTL_S
        r = await client.get("/api/health")
        assert r.json()["config"]["valid"] is False
        assert "webhok" in r.json()["config"]["error"]
        assert secret not in r.text


def test_example_compose_runs_serve_for_the_dashboard() -> None:
    """The example stack runs serve enabled, since the dashboard needs it; its token
    stays optional (empty disables only the mutating endpoints) (§spec:deployment)."""
    import yaml

    compose = yaml.safe_load((REPO_ROOT / "docker-compose.example.yaml").read_text())
    serve = compose["services"]["shakedown-serve"]
    assert serve["command"][0] == "serve"
    assert serve["restart"] == "unless-stopped"
    assert "SHAKEDOWN_API_TOKEN=${SHAKEDOWN_API_TOKEN:-}" in serve["environment"]
    assert "/share/data:/data" in serve["volumes"]


@pytest.mark.asyncio
async def test_stopping_serve_stops_the_runs_it_started(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """serve's shutdown asks each run it started to stop and waits for it
    (§spec:graceful-stop): the run ends `stopped` and exits 143, instead of being
    killed with the container when serve, its PID 1, exits."""
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    app = _child_app(config, items={"gd-x": {"x.flac": "audio"}}, gate=archive.parent / "never")
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.post("/sync", headers=AUTH)
        assert r.status_code == 202
        # Ctrl-C in serve's terminal signals serve's process group; the run is
        # outside it, so serve's SIGTERM is its first signal, not a second one.
        (running,) = app.state.triggered
        assert os.getpgid(running.process.pid) != os.getpgrp()
    (triggered,) = app.state.triggered
    assert triggered.done.is_set()
    assert triggered.process.returncode == 143
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        last_run = (await client.get("/status")).json()[0]["last_run"]
        assert last_run["result"] == "stopped"
        metrics = (await client.get("/metrics")).text
    assert 'shakedown_sync_runs_total{result="stopped"} 1.0' in metrics



@pytest.mark.asyncio
async def test_answers_over_a_kilobyte_are_gzipped_for_a_client_that_asks(
    tmp_roots: tuple[Path, Path],
) -> None:
    """§spec:dashboard "Compressed": a proxy that passes Content-Encoding through (as
    nginx-proxy-manager does) hands the browser the compressed answer; a small
    answer, or a client that does not ask, gets it as it is."""
    from shakedown.dashboard import VIEW_TTL_S, register_api

    archive, library = tmp_roots
    app = _probe_app(make_config(archive, library), _Clock())
    register_api(app, "/api/big", VIEW_TTL_S, lambda ctx: {"ids": [f"gd-{i:05d}" for i in range(500)]})
    gzip_ok = {"Accept-Encoding": "gzip"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        big = await client.get("/api/big", headers=gzip_ok)
        small = await client.get("/api/probe", headers=gzip_ok)
        plain = await client.get("/api/big", headers={"Accept-Encoding": "identity"})

    assert big.headers["content-encoding"] == "gzip"
    assert big.json()["ids"][499] == "gd-00499"  # decoded by the client
    assert int(big.headers["content-length"]) < len(plain.content) / 4
    assert "content-encoding" not in small.headers
    assert "content-encoding" not in plain.headers
