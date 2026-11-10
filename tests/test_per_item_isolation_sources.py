"""One bad item fails alone, on every shipped source (SPEC §spec:per-item-isolation,
§req:success-criteria #22).

The real `ia` and `etree` plugins run through `run_sync`, `print_status`, and
`run_reconcile` against a fake archive.org: `ia` below urllib3
(tests/fake_archive.py), `etree` through httpx.MockTransport. No network is used.
"""
from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.db import connect
from shakedown.models import ItemStatus
from shakedown.reconcile import run_reconcile
from shakedown.selection import LOSSLESS_REFUSED_REASON
from shakedown.state import ItemRepo, RunRepo
from shakedown.status import print_status
from shakedown.sync import run_sync
from tests.fake_archive import Reply, fake_archive, json_reply

SHOWS = [f"gd77-05-{day:02d}" for day in range(1, 9)]
SERVER_ERROR = "gd77-05-02"
NAMELESS = "gd77-05-05"


def _md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def _file(name: str, content: bytes, fmt: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "size": str(len(content)), "md5": _md5(content), "format": fmt, **extra}


class FakeSource:
    """archive.org as one shipped plugin sees it: a listing, per-show metadata, and
    file downloads, each of which a test can break."""

    type_name: str

    def __init__(self) -> None:
        self.listing: list[str] = []
        self.metadata: dict[str, Any] = {}  # identifier -> JSON body, or an HTTP status
        self.content: dict[tuple[str, str], bytes] = {}
        self.listing_fails = False

    def add_show(self, identifier: str, files: list[tuple[str, bytes, dict[str, Any]]]) -> None:
        self.listing.append(identifier)
        self.metadata[identifier] = {
            "metadata": {"title": identifier, "date": "1977-05-08"},
            "files": [_file(name, content, "Flac", **extra) for name, content, extra in files],
        }
        for name, content, _extra in files:
            self.content[(identifier, name)] = content

    def flac_show(self, identifier: str) -> None:
        self.add_show(identifier, [(f"{identifier}.flac", identifier.encode(), {})])

    def heal(self, identifier: str) -> None:
        """archive.org answers for the show again."""
        self.listing.remove(identifier)
        self.flac_show(identifier)

    def config(self, archive: Path, library: Path, **collection: Any) -> Config:
        return Config(
            archive_root=archive,
            library_root=library,
            sources=[
                SourceConfig(
                    name="src",
                    type=self.type_name,
                    collections=[CollectionConfig(name="coll", query="collection:x", **collection)],
                )
            ],
        )

    @contextmanager
    def serving(self) -> Iterator[None]:
        raise NotImplementedError


class FakeIA(FakeSource):
    type_name = "ia"

    @contextmanager
    def serving(self) -> Iterator[None]:
        with fake_archive() as archive:
            this = self

            class Routes(dict):
                # Answer from the source's current state on every request.
                def get(self, path, default=None):  # type: ignore[override]
                    return this._replies(path)

            archive.routes = Routes()
            yield

    def _replies(self, path: str) -> list[Reply] | None:
        if path == "/services/search/v1/scrape":
            if self.listing_fails:
                return [Reply(404, b"no such collection")]
            hits = [{"identifier": i} for i in self.listing]
            return [json_reply({"items": hits, "count": len(hits), "total": len(hits)})]
        if path.startswith("/metadata/"):
            body = self.metadata.get(path.removeprefix("/metadata/"))
            if isinstance(body, int):
                return [Reply(body, b"server error")]
            return [json_reply(body)] if body is not None else None
        if path.startswith("/download/"):
            identifier, name = path.removeprefix("/download/").split("/", 1)
            content = self.content.get((identifier, name))
            return [Reply(200, content)] if content is not None else None
        return None


class FakeEtree(FakeSource):
    type_name = "etree"

    @contextmanager
    def serving(self) -> Iterator[None]:
        real_client = httpx.Client

        def client(*_a: object, **_k: object) -> httpx.Client:
            return real_client(base_url="https://archive.org", transport=httpx.MockTransport(self._handle))

        with patch("shakedown.plugins.etree.plugin.httpx.Client", client):
            yield

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/advancedsearch.php":
            if self.listing_fails:
                # The search ends before its own total: a short, failed listing.
                return httpx.Response(200, json={"response": {"numFound": 99, "docs": []}})
            docs = [{"identifier": i} for i in self.listing]
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(
                200,
                json={"response": {"numFound": len(docs), "docs": docs if page == 1 else []}},
            )
        if path.startswith("/metadata/"):
            body = self.metadata.get(path.removeprefix("/metadata/"))
            if isinstance(body, int):
                return httpx.Response(body, text="server error")
            return httpx.Response(200, json=body) if body is not None else httpx.Response(404)
        if path.startswith("/download/"):
            identifier, name = path.removeprefix("/download/").split("/", 1)
            content = self.content.get((identifier, name))
            return httpx.Response(200, content=content) if content is not None else httpx.Response(404)
        return httpx.Response(404)


@pytest.fixture(params=[FakeIA, FakeEtree], ids=["ia", "etree"])
def source(request) -> FakeSource:
    return request.param()


def _status_json(config: Config) -> dict[str, Any]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_status(config, as_json=True)
    return next(s for s in json.loads(buf.getvalue()) if s["collection"] == "coll")


def _status_text(config: Config) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_status(config, as_json=False)
    return buf.getvalue()


def _row(config: Config, identifier: str):
    return ItemRepo(connect(config.state_db)).get("src", "coll", identifier)  # type: ignore[arg-type]


def test_one_server_error_and_one_malformed_listing_fail_only_their_shows(
    source: FakeSource, tmp_roots: tuple[Path, Path]
) -> None:
    archive, library = tmp_roots
    config = source.config(archive, library)
    for identifier in SHOWS:
        source.flac_show(identifier)
    source.metadata[SERVER_ERROR] = 503
    source.metadata[NAMELESS]["files"].append({"size": "3", "md5": "a" * 32})

    with source.serving():
        assert run_sync(config) == 1

        latest = RunRepo(connect(config.state_db)).latest("src", "coll")  # type: ignore[arg-type]
        assert latest is not None
        assert sorted(latest.errors) == [
            f"{SERVER_ERROR}: lookup failed after 3 attempts: HTTP 503",
            f"{NAMELESS}: source sent a malformed file list: file entry 2 has no name",
        ]
        for identifier in SHOWS:
            row = _row(config, identifier)
            assert row is not None
            expected = (
                ItemStatus.FAILED if identifier in (SERVER_ERROR, NAMELESS) else ItemStatus.COMPLETE
            )
            assert row.status == expected, identifier
        assert (archive / "src" / "coll" / SHOWS[0] / f"{SHOWS[0]}.flac").is_file()

        status = _status_json(config)
        assert status["stale"] is False
        assert status["counts"]["complete"] == len(SHOWS) - 2
        assert "STALE" not in _status_text(config)

        # The next run retries both; once archive.org answers, they sync.
        source.heal(SERVER_ERROR)
        source.heal(NAMELESS)
        assert run_sync(config) == 0
        for identifier in (SERVER_ERROR, NAMELESS):
            row = _row(config, identifier)
            assert row is not None and row.status == ItemStatus.COMPLETE


def test_mirrored_show_whose_lookup_fails_keeps_status_files_and_is_not_disappeared(
    source: FakeSource, tmp_roots: tuple[Path, Path]
) -> None:
    archive, library = tmp_roots
    config = source.config(archive, library, prune_disappeared=True)
    for identifier in SHOWS[:3]:
        source.flac_show(identifier)
    target = SHOWS[1]
    archived = archive / "src" / "coll" / target / f"{target}.flac"

    with source.serving():
        assert run_sync(config) == 0
        source.metadata[target] = 503

        assert run_sync(config) == 1
        row = _row(config, target)
        assert row is not None and row.status == ItemStatus.COMPLETE
        assert row.recorded_manifest is not None
        assert archived.read_bytes() == target.encode()

        assert run_reconcile(config) == 1
        row = _row(config, target)
        assert row is not None and row.status == ItemStatus.COMPLETE
        assert archived.is_file()


def test_refused_lossless_show_mirrors_its_best_lossy_copy(
    source: FakeSource, tmp_roots: tuple[Path, Path]
) -> None:
    """A stream-only show: archive.org flags the item and marks the lossless files
    `private`; the MP3s stay public. Both sources mirror the fallback."""
    archive, library = tmp_roots
    config = source.config(
        archive, library
    )
    show = "gd69-12-26.sbd"
    source.add_show(show, [("d1t01.shn", b"lossless", {"private": "true"})])
    source.metadata[show]["metadata"]["access-restricted-item"] = "true"
    source.metadata[show]["files"].append(_file("d1t01.mp3", b"mp3", "VBR MP3"))
    source.content[(show, "d1t01.mp3")] = b"mp3"

    with source.serving():
        assert run_sync(config) == 0

    row = _row(config, show)
    assert row is not None and row.status == ItemStatus.COMPLETE
    assert row.restriction_reason == LOSSLESS_REFUSED_REASON
    assert (archive / "src" / "coll" / show / "d1t01.mp3").read_bytes() == b"mp3"
    assert not (archive / "src" / "coll" / show / "d1t01.shn").exists()
    assert _status_json(config)["restricted"] == [{"identifier": show, "reason": LOSSLESS_REFUSED_REASON}]


def test_listing_failure_marks_the_collection_stale_and_prunes_nothing(
    source: FakeSource, tmp_roots: tuple[Path, Path]
) -> None:
    archive, library = tmp_roots
    config = source.config(archive, library, prune_disappeared=True)
    for identifier in SHOWS[:2]:
        source.flac_show(identifier)

    with source.serving():
        assert run_sync(config) == 0
        source.listing_fails = True
        assert run_sync(config) == 1

    status = _status_json(config)
    assert status["stale"] is True
    assert "STALE" in _status_text(config)
    for identifier in SHOWS[:2]:
        row = _row(config, identifier)
        assert row is not None and row.status == ItemStatus.COMPLETE
        assert (archive / "src" / "coll" / identifier / f"{identifier}.flac").is_file()
