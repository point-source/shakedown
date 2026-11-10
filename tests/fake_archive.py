"""A fake archive.org below urllib3, for testing the real HTTP stack above it.

Patching at urllib3's per-request hook (instead of mocking the `internetarchive`
library or `requests`) keeps every retry layer in play: the urllib3 `Retry` the
library mounts on a session, and shakedown's own retries. Each
request that would reach the network is recorded, so a test can count exactly how
many times archive.org was asked.
"""
from __future__ import annotations

import io
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import urllib3
from urllib3._collections import HTTPHeaderDict
from urllib3.response import HTTPResponse


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


def json_reply(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Reply:
    return Reply(status, json.dumps(payload).encode(), {"Content-Type": "application/json", **(headers or {})})


@dataclass
class Call:
    method: str
    path: str
    url: str
    timeout: Any


class FakeArchive:
    """Route archive.org requests by URL path. Each path holds a queue of replies
    (a `Reply` or an exception to raise); the last one repeats."""

    def __init__(self) -> None:
        self.routes: dict[str, list[Reply | BaseException]] = {}
        self.calls: list[Call] = []
        # Seconds urllib3's Retry slept between attempts (backoff or Retry-After).
        self.sleeps: list[float] = []

    def route(self, path: str, *replies: Reply | BaseException) -> None:
        self.routes[path] = list(replies)

    def search_results(self, *hits: dict[str, Any]) -> None:
        self.route(
            "/services/search/v1/scrape",
            json_reply({"items": list(hits), "count": len(hits), "total": len(hits)}),
        )

    def metadata(self, identifier: str, *replies: Reply | BaseException) -> None:
        self.route(f"/metadata/{identifier}", *replies)

    def item(self, identifier: str, files: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> None:
        self.metadata(identifier, json_reply({"files": files, "metadata": metadata or {}}))

    def file(self, identifier: str, name: str, *replies: Reply | BaseException) -> None:
        self.route(f"/download/{identifier}/{name}", *replies)

    def calls_to(self, path: str) -> list[Call]:
        return [c for c in self.calls if c.path == path]

    def _reply(self, method: str, url: str, timeout: Any) -> Reply:
        path = unquote(urlsplit(url).path)
        self.calls.append(Call(method, path, url, timeout))
        queue = self.routes.get(path)
        if not queue:
            return Reply(404, b"not found")
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply


@contextmanager
def fake_archive() -> Iterator[FakeArchive]:
    archive = FakeArchive()

    def make_request(pool, conn, method, url, body=None, headers=None, retries=None,
                     timeout=None, **_kwargs):
        reply = archive._reply(method, url, timeout)
        return HTTPResponse(
            body=io.BytesIO(reply.body),
            headers=HTTPHeaderDict(reply.headers),
            status=reply.status,
            preload_content=False,
            decode_content=False,
            request_method=method,
            request_url=url,
            retries=retries,
            pool=pool,
        )

    with (
        patch.object(urllib3.connectionpool.HTTPConnectionPool, "_make_request", make_request),
        # urllib3's Retry sleeps between attempts; record instead of waiting.
        patch("urllib3.util.retry.time.sleep", archive.sleeps.append),
    ):
        yield archive
