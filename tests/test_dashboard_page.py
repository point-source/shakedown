"""serve answers the built dashboard page at its root (§spec:dashboard-page).

Each test serves a temp folder shaped like the page build (`index.html` plus a
content-hashed file under `assets/`), so no Node build is needed.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.server import build_app
from shakedown.sync import run_sync
from tests.conftest import make_config, write_config

INDEX = '<!doctype html><script type="module" src="./assets/index-abc123.js"></script>'
SCRIPT = "console.log('page')"


def _page(root: Path) -> Path:
    page = root / "dashboard_web"
    (page / "assets").mkdir(parents=True)
    (page / "index.html").write_text(INDEX)
    (page / "assets" / "index-abc123.js").write_text(SCRIPT)
    return page


def _app(tmp_roots: tuple[Path, Path], page_dir: Path):  # type: ignore[no-untyped-def]
    archive, library = tmp_roots
    config = make_config(archive, library)
    assert run_sync(config) == 0  # creates the state DB /status and /metrics read
    return build_app(write_config(archive.parent / "cfg.yaml", config), page_dir=page_dir)


def _client(app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.mark.asyncio
async def test_root_serves_the_page_and_its_assets(tmp_roots: tuple[Path, Path]) -> None:
    """GET / is index.html, never cached stale after an upgrade; the content-hashed
    assets may be cached."""
    app = _app(tmp_roots, _page(tmp_roots[0].parent))
    async with _client(app) as client:
        r = await client.get("/")
        assert r.status_code == 200
        assert r.text == INDEX
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-cache"

        r = await client.get("/assets/index-abc123.js")
        assert r.status_code == 200
        assert r.text == SCRIPT
        assert "javascript" in r.headers["content-type"]
        assert "max-age=" in r.headers["cache-control"]
        assert "no-cache" not in r.headers["cache-control"]


@pytest.mark.asyncio
async def test_without_a_build_root_is_404_and_the_api_still_works(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A source checkout with no page build still serves every API route."""
    app = _app(tmp_roots, tmp_roots[0].parent / "dashboard_web")  # never created
    async with _client(app) as client:
        r = await client.get("/")
        assert r.status_code == 404
        assert r.headers["content-type"] == "application/json"
        assert (await client.get("/assets/index-abc123.js")).status_code == 404
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/status")).status_code == 200
        assert (await client.get("/api/health")).status_code == 200


@pytest.mark.asyncio
async def test_removing_index_html_turns_root_into_404(tmp_roots: tuple[Path, Path]) -> None:
    page = _page(tmp_roots[0].parent)
    app = _app(tmp_roots, page)
    async with _client(app) as client:
        assert (await client.get("/")).status_code == 200
        (page / "index.html").unlink()
        assert (await client.get("/")).status_code == 404
        assert (await client.get("/healthz")).status_code == 200


@pytest.mark.asyncio
async def test_api_routes_win_over_the_page(tmp_roots: tuple[Path, Path]) -> None:
    """With a page present, existing routes answer as before and an unknown API path
    is a JSON 404, not the page."""
    app = _app(tmp_roots, _page(tmp_roots[0].parent))
    async with _client(app) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        r = await client.get("/status")
        assert r.status_code == 200 and isinstance(r.json(), list)
        r = await client.get("/metrics")
        assert r.status_code == 200 and "shakedown_items_total" in r.text
        r = await client.get("/api/health")
        assert r.status_code == 200 and "generated_at" in r.json()

        for path in ("/api/nonexistent", "/assets/missing.js", "/nonexistent"):
            r = await client.get(path)
            assert r.status_code == 404, path
            assert r.headers["content-type"] == "application/json", path


@pytest.mark.asyncio
async def test_page_is_get_only_and_sends_no_cors_allowance(
    tmp_roots: tuple[Path, Path],
) -> None:
    app = _app(tmp_roots, _page(tmp_roots[0].parent))
    origin = {"Origin": "https://elsewhere.example"}
    async with _client(app) as client:
        for path in ("/", "/assets/index-abc123.js"):
            r = await client.get(path, headers=origin)
            assert r.status_code == 200, path
            assert not [h for h in r.headers if h.lower().startswith("access-control-")]
            assert (await client.post(path)).status_code == 405, path
