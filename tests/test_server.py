"""Smoke test for shakedown serve. PRD §10."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.server import TOKEN_ENV, build_app
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


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
        transport=httpx.ASGITransport(app=build_app(config)),
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
        transport=httpx.ASGITransport(app=build_app(config)),
        base_url="http://testserver",
    ) as client:
        # default validation is read-only: no token, same posture as /status
        r = await client.get("/validate")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert any(g["collection"] == "coll1" for g in body["groups"])

        # a live handoff test is mutating: same bearer posture as sync/verify
        assert (
            await client.get("/validate", params={"live_handoff": True})
        ).status_code == 401
        r = await client.get(
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
        transport=httpx.ASGITransport(app=build_app(config)),
        base_url="http://testserver",
    ) as client:

        # read endpoints remain reachable
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/status")).status_code == 200
        assert (await client.get("/metrics")).status_code == 200
        # default (read-only) validation stays reachable
        assert (await client.get("/validate")).status_code == 200

        # mutating endpoints are disabled, not open -- even with a bearer header
        assert (
            await client.post("/sync", headers={"Authorization": "Bearer anything"})
        ).status_code == 503
        assert (await client.post("/verify")).status_code == 503
        # a live handoff test is mutating: disabled without a configured token
        assert (
            await client.get("/validate", params={"live_handoff": True})
        ).status_code == 503
