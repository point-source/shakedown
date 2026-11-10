"""Smoke test for shakedown serve. PRD §10."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.server import TOKEN_ENV, build_app
from shakedown.sync import run_sync
from tests.conftest import make_config, write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _app(config):  # type: ignore[no-untyped-def]
    """serve over a config file holding ``config`` (written beside the archive root)."""
    return build_app(write_config(config.archive_root.parent / "cfg.yaml", config))


@pytest.mark.asyncio
async def test_serve_endpoints_smoke(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"audio")]
    )
    assert run_sync(config) == 0

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(config)),
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
        assert r.status_code == 200
        assert r.json()["exit_code"] == 0
        assert FakePlugin.fetch_count["gd-x"] == 1  # idempotent

        # verify: requires a bearer token
        r = await client.post(
            "/verify?deep=true",
            headers={"Authorization": "Bearer test-token"},
        )
        assert r.status_code == 200
        assert r.json()["exit_code"] == 0


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
