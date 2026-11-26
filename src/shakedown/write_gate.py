"""The write gate on serve's mutating endpoints (§spec:dashboard-run-controls).

A request that writes or starts work passes when it either carries the configured
bearer token (``SHAKEDOWN_API_TOKEN``), as scripts and ``curl`` do, or comes from
the dashboard's own page: a JSON request, which a cross-site form cannot send, that
the browser marks ``Sec-Fetch-Site: same-origin``. A browser sends fetch metadata
only to HTTPS and localhost, so a page opened over plain HTTP at a LAN address is
recognised by its ``Origin`` instead: its host name must be the host the request was
sent to (``Host``, ``X-Forwarded-Host`` or ``Forwarded``). Anything else answers
``403 {"code": "cross_site"}``. With no token configured only the dashboard page
writes.

Attach it to a route, never copy it::

    @app.post("/thing", dependencies=[Depends(write_gate)])

``Refusal`` is the coded answer every such endpoint gives (``{"code": ..., **extra}``,
§spec:dashboard "Codes, not copy"); ``install_refusals`` registers its handler.
"""
from __future__ import annotations

import hmac
import os
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

TOKEN_ENV = "SHAKEDOWN_API_TOKEN"
CROSS_SITE = "cross_site"


class Refusal(Exception):
    """An endpoint's refusal, answered as ``status`` with ``{"code": code, **extra}``."""

    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


def install_refusals(app: FastAPI) -> None:
    async def answer(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, Refusal)
        return JSONResponse({"code": exc.code, **exc.extra}, status_code=exc.status)

    app.add_exception_handler(Refusal, answer)


def has_token(request: Request) -> bool:
    """The request carries the configured bearer token. No token configured: never."""
    expected = os.environ.get(TOKEN_ENV)
    if not expected:
        return False
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    # Compare as bytes: hmac.compare_digest rejects non-ASCII str operands with
    # TypeError, which would surface a malformed header as a 500.
    return scheme.lower() == "bearer" and hmac.compare_digest(
        token.encode(), expected.encode()
    )


def from_dashboard(request: Request) -> bool:
    """A JSON request the browser marks same-origin, or, when it sends no fetch
    metadata, whose Origin names the host it was sent to. A cross-site page can set
    neither Sec-Fetch-Site nor Origin, nor send JSON without a CORS preflight, which
    serve never allows (§spec:dashboard-run-controls)."""
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        return False
    site = request.headers.get("sec-fetch-site", "").strip().lower()
    if site:
        return site == "same-origin"
    return _host_name(request.headers.get("origin", "")) in _request_host_names(request)


def _host_name(url: str) -> str | None:
    """The lowercased host name of an origin or ``//host[:port]``; None if it has none.
    Port and scheme do not count: a proxy often drops or replaces the port."""
    try:
        return urlsplit(url.strip()).hostname
    except ValueError:  # a malformed IPv6 literal
        return None


def _request_host_names(request: Request) -> set[str]:
    """The host names the request was sent to: Host, and the first proxy's
    X-Forwarded-Host and Forwarded ``host=``. Only a browser's cross-site request
    matters here, and it cannot set the forwarded headers without a preflight."""
    hosts = [
        request.headers.get("host", ""),
        request.headers.get("x-forwarded-host", "").split(",")[0],
    ]
    first_hop = request.headers.get("forwarded", "").split(",")[0]
    for pair in first_hop.split(";"):
        key, _, value = pair.partition("=")
        if key.strip().lower() == "host":
            hosts.append(value.strip().strip('"'))
    return {name for host in hosts if (name := _host_name("//" + host))}


def write_gate(request: Request) -> None:
    """FastAPI dependency: the token or the dashboard page, else 403 cross_site."""
    if not (has_token(request) or from_dashboard(request)):
        raise Refusal(403, CROSS_SITE)


def require_token(request: Request) -> None:
    """The token only, for what the dashboard never offers (live handoff validation)."""
    if not has_token(request):
        raise Refusal(403, CROSS_SITE)
