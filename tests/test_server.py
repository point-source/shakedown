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

    # sync: requires token
    assert client.post("/sync").status_code == 401
    r = client.post("/sync", headers={"X-Shakedown-Token": "test-token"})
    assert r.status_code == 200
    assert r.json()["exit_code"] == 0
    assert FakePlugin.fetch_count["gd-x"] == 1  # idempotent

    # verify: requires token
    r = client.post(
        "/verify?deep=true",
        headers={"X-Shakedown-Token": "test-token"},
    )
    assert r.status_code == 200
    assert r.json()["exit_code"] == 0
