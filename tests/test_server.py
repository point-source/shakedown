"""Smoke test for shakedown serve. PRD §10."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from shakedown.server import TOKEN_ENV, build_app
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def test_serve_endpoints_smoke(tmp_roots: tuple[Path, Path], monkeypatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "test-token")
    archive, library = tmp_roots
    config = make_config(archive, library)
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"audio")]
    )
    assert run_sync(config) == 0

    client = TestClient(build_app(config))

    # healthz: open
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}

    # status: open, JSON
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert body[0]["source"] == "fake-src"
    assert body[0]["counts"]["complete"] == 1

    # metrics: open, prometheus text
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "shakedown_items_total" in r.text
    assert 'status="complete"' in r.text

    # sync: requires a bearer token
    assert client.post("/sync").status_code == 401
    # a bare token (no Bearer scheme) is rejected
    assert client.post("/sync", headers={"Authorization": "test-token"}).status_code == 401
    # a malformed (non-ASCII) token yields a clean 401, not a 500. Starlette
    # decodes header bytes as latin-1, so send raw bytes to reach that path.
    assert (
        client.post("/sync", headers={"Authorization": b"Bearer \xff"}).status_code
        == 401
    )
    r = client.post("/sync", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    assert r.json()["exit_code"] == 0
    assert FakePlugin.fetch_count["gd-x"] == 1  # idempotent

    # verify: requires a bearer token
    r = client.post(
        "/verify?deep=true",
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 200
    assert r.json()["exit_code"] == 0


def test_serve_mutating_disabled_without_token(
    tmp_roots: tuple[Path, Path], monkeypatch
) -> None:
    """No token configured => mutating endpoints are disabled (503), not open,
    while read endpoints stay reachable."""
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    archive, library = tmp_roots
    config = make_config(archive, library)
    client = TestClient(build_app(config))

    # read endpoints remain reachable
    assert client.get("/healthz").status_code == 200
    assert client.get("/status").status_code == 200
    assert client.get("/metrics").status_code == 200

    # mutating endpoints are disabled, not open — even with a bearer header
    assert client.post("/sync", headers={"Authorization": "Bearer anything"}).status_code == 503
    assert client.post("/verify").status_code == 503
