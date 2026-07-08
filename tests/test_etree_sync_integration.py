"""End-to-end vertical-slice test: `type: etree` through the registry into sync.

Exercises the same functions the CLI's `shakedown sync` and `shakedown status`
call (`run_sync`, `print_status`), proving an etree source mirrors into the
archive and library trees and appears in status. archive.org HTTP is served by
httpx.MockTransport, so no network is used.
"""
from __future__ import annotations

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.status import print_status
from shakedown.sync import run_sync

_FLAC = b"fake-flac-audio-bytes"
_MD5 = hashlib.md5(_FLAC).hexdigest()
_IDENT = "gd1977-05-08.etree"
_RealClient = httpx.Client


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/advancedsearch.php":
        return httpx.Response(
            200, json={"response": {"numFound": 1, "docs": [{"identifier": _IDENT}]}}
        )
    if path == f"/metadata/{_IDENT}":
        return httpx.Response(
            200,
            json={
                "metadata": {
                    "title": "Barton Hall",
                    "creator": "Grateful Dead",
                    "date": "1977-05-08",
                    "venue": "Barton Hall, Cornell University",
                    "coverage": "Ithaca, NY",
                },
                "files": [
                    {"name": "gd77-05-08d1t01.flac", "size": str(len(_FLAC)),
                     "md5": _MD5, "format": "Flac"},
                    {"name": "gd77-05-08d1t01_64kb.mp3", "size": "10",
                     "md5": "x", "format": "VBR MP3"},
                ],
            },
        )
    if path == f"/download/{_IDENT}/gd77-05-08d1t01.flac":
        return httpx.Response(200, content=_FLAC)
    return httpx.Response(404, json={"error": f"unexpected path {path}"})


def _mock_client(*_args: object, **_kwargs: object) -> httpx.Client:
    return _RealClient(base_url="https://archive.org", transport=httpx.MockTransport(_handler))


def _etree_config(archive: Path, library: Path) -> Config:
    return Config(
        archive_root=archive,
        library_root=library,
        sources=[
            SourceConfig(
                name="etree-lma",
                type="etree",
                collections=[
                    CollectionConfig(
                        name="etree",
                        query="collection:etree",
                        format_filters=["flac", "shn"],
                        exclude_filters=["*_64kb.mp3"],
                        library_layout="{year}/{date} - {venue|sanitize}",
                    )
                ],
            )
        ],
    )


def test_etree_source_syncs_into_archive_library_and_status(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = _etree_config(archive, library)

    with patch("shakedown.plugins.etree.plugin.httpx.Client", _mock_client):
        assert run_sync(config) == 0

        archived = archive / "etree-lma" / "etree" / _IDENT / "gd77-05-08d1t01.flac"
        assert archived.is_file()
        assert archived.read_bytes() == _FLAC

        # exclude_filter drops the lossy mp3.
        assert not (archive / "etree-lma" / "etree" / _IDENT / "gd77-05-08d1t01_64kb.mp3").exists()

        # Library staging rendered from the template and hardlinked to the archive inode.
        staged = (
            library / "etree-lma" / "etree" / "1977"
            / "1977-05-08 - Barton Hall, Cornell University" / "gd77-05-08d1t01.flac"
        )
        assert staged.is_file()
        assert staged.stat().st_ino == archived.stat().st_ino

        buf = io.StringIO()
        with redirect_stdout(buf):
            print_status(config, as_json=True)
        summary = next(
            s for s in json.loads(buf.getvalue())
            if (s["source"], s["collection"]) == ("etree-lma", "etree")
        )
        assert summary["counts"]["complete"] == 1
        assert summary["last_run"]["items_new"] == 1


def test_etree_sync_is_idempotent(tmp_roots: tuple[Path, Path]) -> None:
    """A second sync re-discovers but does not re-download the unchanged item."""
    archive, library = tmp_roots
    config = _etree_config(archive, library)

    downloads: list[str] = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/download/"):
            downloads.append(request.url.path)
        return _handler(request)

    def counting_client(*_a: object, **_k: object) -> httpx.Client:
        return _RealClient(
            base_url="https://archive.org", transport=httpx.MockTransport(counting_handler)
        )

    with patch("shakedown.plugins.etree.plugin.httpx.Client", counting_client):
        assert run_sync(config) == 0
        assert len(downloads) == 1
        assert run_sync(config) == 0
        assert len(downloads) == 1, "unchanged item must not be re-downloaded"
