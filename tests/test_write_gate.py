"""The write gate on serve's mutating endpoints (§spec:dashboard-run-controls,
§spec:serve): the bearer token, or a same-origin JSON request from the dashboard's
own page; anything else is `403 {"code": "cross_site"}`."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.write_gate import TOKEN_ENV
from tests.conftest import make_config
from tests.test_server import AUTH, CROSS_SITE, DASHBOARD, _app, _child_app, _finish

TOKEN = "test-token"  # the token tests.test_server.AUTH carries
MUTATING = ("/sync", "/verify", "/validate")


def _client(app) -> httpx.AsyncClient:  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


def _token(monkeypatch: pytest.MonkeyPatch, token: str | None) -> None:
    if token is None:
        monkeypatch.delenv(TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(TOKEN_ENV, token)


REFUSED = [
    pytest.param(TOKEN, {}, id="no-headers"),
    pytest.param(None, {}, id="no-headers-no-token-configured"),
    pytest.param(TOKEN, {"Authorization": "Bearer wrong"}, id="wrong-token"),
    pytest.param(TOKEN, {"Authorization": TOKEN}, id="token-without-bearer-scheme"),
    # Starlette decodes header bytes as latin-1; raw bytes reach that path, and the
    # answer is a clean refusal, never a 500.
    pytest.param(TOKEN, {"Authorization": b"Bearer \xff"}, id="non-ascii-token"),
    pytest.param(None, {"Authorization": "Bearer anything"}, id="token-none-configured"),
    pytest.param("", {"Authorization": "Bearer "}, id="empty-token-configured"),
    pytest.param(
        TOKEN,
        {"Sec-Fetch-Site": "same-origin", "Content-Type": "application/x-www-form-urlencoded"},
        id="same-origin-form",
    ),
    pytest.param(
        TOKEN,
        {"Sec-Fetch-Site": "same-origin", "Content-Type": "multipart/form-data; boundary=x"},
        id="same-origin-multipart",
    ),
    pytest.param(
        TOKEN, {"Sec-Fetch-Site": "same-origin", "Content-Type": "text/plain"},
        id="same-origin-text-plain",
    ),
    pytest.param(TOKEN, {"Sec-Fetch-Site": "same-origin"}, id="same-origin-no-content-type"),
    pytest.param(None, {**DASHBOARD, "Sec-Fetch-Site": "same-site"}, id="same-site-json"),
    pytest.param(None, {**DASHBOARD, "Sec-Fetch-Site": "cross-site"}, id="cross-site-json"),
    pytest.param(None, {**DASHBOARD, "Sec-Fetch-Site": "none"}, id="user-initiated-json"),
    pytest.param(None, {"Content-Type": "application/json"}, id="json-without-fetch-metadata"),
]


# One route carries the header matrix: every mutating route runs the same gate, and
# test_the_gate_answers_before_the_scope_and_config_checks shows each one is gated.
@pytest.mark.asyncio
@pytest.mark.parametrize(("token", "headers"), REFUSED)
async def test_a_request_neither_tokened_nor_from_the_dashboard_is_refused(
    tmp_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    headers: dict[str, str],
) -> None:
    _token(monkeypatch, token)
    async with _client(_app(make_config(*tmp_roots))) as client:
        r = await client.post("/sync", headers=headers)
    assert r.status_code == 403
    assert r.json() == CROSS_SITE


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [TOKEN, None], ids=["token-configured", "no-token"])
@pytest.mark.parametrize(
    "content_type", ["application/json", "application/json; charset=utf-8"]
)
async def test_the_dashboard_page_starts_a_sync_with_or_without_a_token(
    tmp_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    content_type: str,
) -> None:
    _token(monkeypatch, token)
    app = _child_app(make_config(*tmp_roots))
    async with _client(app) as client:
        r = await client.post(
            "/sync",
            params={"collection": "coll1"},
            headers={**DASHBOARD, "Content-Type": content_type},
            content=b"{}",
        )
        assert r.status_code == 202
        assert r.json() == {"started": "sync", "source": None, "collection": "coll1"}
        await _finish(app)


@pytest.mark.asyncio
async def test_a_script_with_the_token_starts_a_sync(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _token(monkeypatch, TOKEN)
    app = _child_app(make_config(*tmp_roots))
    async with _client(app) as client:
        r = await client.post("/sync", params={"collection": "coll1"}, headers=AUTH)
        assert r.status_code == 202
        await _finish(app)


@pytest.mark.asyncio
async def test_the_dashboard_page_verifies_and_validates(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _token(monkeypatch, None)
    app = _child_app(make_config(*tmp_roots))
    async with _client(app) as client:
        r = await client.post("/validate", headers=DASHBOARD)
        assert r.status_code == 200
        assert r.json()["ready"] is True
        r = await client.post("/verify", headers=DASHBOARD)
        assert r.status_code == 202
        await _finish(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [TOKEN, None], ids=["token-configured", "no-token"])
async def test_live_handoff_validation_takes_the_token_only(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    """A live handoff test can run the file's configured command, so the dashboard's
    own page cannot start one (§spec:dashboard-run-controls)."""
    _token(monkeypatch, token)
    live = {"live_handoff": True}
    async with _client(_app(make_config(*tmp_roots))) as client:
        r = await client.post("/validate", params=live, headers=DASHBOARD)
        assert r.status_code == 403
        assert r.json() == CROSS_SITE
        if token is not None:
            r = await client.post("/validate", params=live, headers={**DASHBOARD, **AUTH})
            assert r.status_code == 200
            r = await client.post("/validate", params=live, headers=AUTH)
            assert r.status_code == 200


@pytest.mark.asyncio
async def test_the_gate_answers_before_the_scope_and_config_checks(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused caller learns nothing about the config; a passed one gets the usual
    404 for an unknown scope and 503 for a broken file (§spec:api-sync-runs)."""
    _token(monkeypatch, None)
    config = make_config(*tmp_roots)
    app = _app(config)
    cfg_path = config.archive_root.parent / "cfg.yaml"
    async with _client(app) as client:
        for route in MUTATING:
            r = await client.post(route, params={"collection": "nope"})
            assert (r.status_code, r.json()) == (403, CROSS_SITE), route
            r = await client.post(route, params={"collection": "nope"}, headers=DASHBOARD)
            assert r.status_code == 404, route
        cfg_path.write_text(cfg_path.read_text() + "max_concurrent_collections: 0\n")
        for route in MUTATING:
            r = await client.post(route)
            assert (r.status_code, r.json()) == (403, CROSS_SITE), route
            r = await client.post(route, headers=DASHBOARD)
            assert r.status_code == 503, route


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [TOKEN, None], ids=["token-configured", "no-token"])
async def test_reads_need_neither_token_nor_dashboard_headers(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    _token(monkeypatch, token)
    async with _client(_app(make_config(*tmp_roots))) as client:
        for route in ("/healthz", "/status", "/metrics", "/api/health", "/api/runs"):
            assert (await client.get(route)).status_code == 200, route
