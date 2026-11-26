"""The write gate on serve's mutating endpoints (§spec:dashboard-run-controls,
§spec:serve): the bearer token, or a JSON request from the dashboard's own page —
marked same-origin by the browser, or, when the browser sends no fetch metadata (a
plain-HTTP page at a LAN address), carrying an Origin whose host name is the host
the request was sent to; anything else is `403 {"code": "cross_site"}`."""
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

# A dashboard page opened over plain HTTP at a LAN address: the browser sends Origin
# but no fetch metadata. NAS is the address the operator typed.
NAS = "http://10.20.111.219:8090"
JSON = {"Content-Type": "application/json"}


def _page(origin: str = NAS, **headers: str) -> dict[str, str]:
    return {**JSON, "Origin": origin, **headers}


PLAIN_HTTP_PAGE = [
    pytest.param(_page(Host="10.20.111.219:8090"), id="host-with-port"),
    # nginx's $host (Nginx Proxy Manager's default) drops the port the browser used.
    pytest.param(_page(Host="10.20.111.219"), id="proxy-drops-the-port"),
    # A proxy with Docker port mapping passes its own port, not the one in the URL.
    pytest.param(_page(Host="10.20.111.219:80"), id="proxy-passes-another-port"),
    # A proxy that sends serve's own address as Host names the page's in a header.
    pytest.param(
        _page(Host="127.0.0.1:8080", **{"X-Forwarded-Host": "10.20.111.219:8090"}),
        id="x-forwarded-host",
    ),
    pytest.param(
        _page(Host="127.0.0.1:8080", **{"X-Forwarded-Host": "10.20.111.219, 127.0.0.1"}),
        id="x-forwarded-host-chain-first",
    ),
    pytest.param(
        _page(
            Host="shakedown-serve:8080",
            Forwarded='for=10.20.111.5;proto=http;host="10.20.111.219:8090", for=10.0.0.2',
        ),
        id="forwarded-host",
    ),
    pytest.param(_page("http://NAS.lan:8090", Host="nas.LAN"), id="host-name-case"),
    pytest.param(_page("http://[fd00::5]:8090", Host="[FD00::5]"), id="ipv6"),
    pytest.param(_page("https://nas.example.net", Host="nas.example.net"), id="https-proxy-strips-metadata"),
    pytest.param(
        _page(Host="10.20.111.219:8090", **{"Content-Type": "application/json; charset=utf-8"}),
        id="json-with-charset",
    ),
]

# What a plain-HTTP page at another origin, or a non-browser caller without the
# token, sends. Matching-looking Origins are paired with what still refuses them.
NOT_THE_PAGE = [
    pytest.param(_page("http://evil.example", Host="10.20.111.219:8090"), id="other-origin"),
    pytest.param(_page("http://10.20.111.21:8090", Host="10.20.111.219"), id="host-prefix"),
    pytest.param(
        _page("http://10.20.111.219.evil.example", Host="10.20.111.219"), id="host-as-subdomain"
    ),
    pytest.param(_page("null", Host="10.20.111.219"), id="origin-null"),
    pytest.param({**JSON, "Host": "10.20.111.219"}, id="no-origin"),
    pytest.param(_page("10.20.111.219", Host="10.20.111.219"), id="origin-without-scheme"),
    pytest.param(_page("http://[fd00::5", Host="[fd00::5"), id="malformed-ipv6"),
    pytest.param(_page("http://", Host=""), id="empty-host-names"),
    pytest.param(
        _page(Host="127.0.0.1:8080", **{"X-Forwarded-Host": "nas.lan"}),
        id="forwarded-host-names-another",
    ),
    # A browser that sends fetch metadata decides; the Origin fallback never overrides it.
    pytest.param(_page(Host="10.20.111.219", **{"Sec-Fetch-Site": "cross-site"}), id="cross-site"),
    pytest.param(_page(Host="10.20.111.219", **{"Sec-Fetch-Site": "same-site"}), id="same-site"),
    pytest.param(_page(Host="10.20.111.219", **{"Sec-Fetch-Site": "none"}), id="user-initiated"),
    # A form can be sent cross-site without a preflight, so only JSON passes.
    pytest.param(
        _page(Host="10.20.111.219", **{"Content-Type": "application/x-www-form-urlencoded"}),
        id="form",
    ),
    pytest.param(_page(Host="10.20.111.219", **{"Content-Type": "text/plain"}), id="text-plain"),
    pytest.param({"Origin": NAS, "Host": "10.20.111.219"}, id="no-content-type"),
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
@pytest.mark.parametrize("headers", PLAIN_HTTP_PAGE)
async def test_a_plain_http_dashboard_page_passes_the_gate(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    """Past the gate, an unknown scope answers 404 (§spec:api-sync-runs)."""
    _token(monkeypatch, None)
    async with _client(_app(make_config(*tmp_roots))) as client:
        r = await client.post("/sync", params={"collection": "nope"}, headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [TOKEN, None], ids=["token-configured", "no-token"])
@pytest.mark.parametrize("headers", NOT_THE_PAGE)
async def test_a_request_not_from_the_dashboard_page_is_refused_without_fetch_metadata(
    tmp_roots: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
    headers: dict[str, str],
) -> None:
    _token(monkeypatch, token)
    async with _client(_app(make_config(*tmp_roots))) as client:
        r = await client.post("/sync", params={"collection": "nope"}, headers=headers)
    assert r.status_code == 403
    assert r.json() == CROSS_SITE


@pytest.mark.asyncio
async def test_a_plain_http_dashboard_page_starts_a_sync(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported setup end to end: Nginx Proxy Manager by IP on :8090."""
    _token(monkeypatch, None)
    app = _child_app(make_config(*tmp_roots))
    async with _client(app) as client:
        r = await client.post(
            "/sync",
            params={"collection": "coll1"},
            headers=_page(Host="10.20.111.219"),
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
