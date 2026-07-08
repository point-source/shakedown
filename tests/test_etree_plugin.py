"""Unit tests for the etree source plugin. No network: HTTP is driven entirely
through httpx.MockTransport by swapping the plugin's client instance attribute.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import Manifest, ManifestFile
from shakedown.plugins.base import ItemDescriptor
from shakedown.plugins.etree.plugin import EtreePlugin

Handler = Callable[[httpx.Request], httpx.Response]


def _make_plugin(handler: Handler) -> EtreePlugin:
    """Construct a plugin and replace its client with a MockTransport-backed one."""
    plugin = EtreePlugin(
        SourceConfig(
            name="etree-lma",
            type="etree",
            collections=[CollectionConfig(name="c", query="collection:etree")],
        )
    )
    # `_client` is a plain instance attribute (declared on EtreePlugin, built in
    # __init__); swap it for a MockTransport-backed client so no network is used.
    plugin._client = httpx.Client(
        base_url="https://archive.org",
        transport=httpx.MockTransport(handler),
    )
    return plugin


def _collection(
    *,
    query: str = "collection:etree",
    format_filters: list[str] | None = None,
    exclude_filters: list[str] | None = None,
) -> CollectionConfig:
    return CollectionConfig(
        name="c",
        query=query,
        format_filters=format_filters or [],
        exclude_filters=exclude_filters or [],
    )


def _search_response(identifiers: list[str], num_found: int) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "response": {
                "numFound": num_found,
                "docs": [{"identifier": i} for i in identifiers],
            }
        },
    )


def _metadata_response(
    metadata: dict[str, Any], files: Sequence[dict[str, Any]]
) -> httpx.Response:
    return httpx.Response(200, json={"metadata": metadata, "files": list(files)})


# -- discover ---------------------------------------------------------------


def test_discover_yields_filtered_manifest_and_metadata() -> None:
    files = [
        {"name": "show.flac", "format": "Flac", "size": "10", "md5": "aaa"},
        {"name": "show_64kb.mp3", "format": "VBR MP3", "size": "3", "md5": "bbb"},
        {"name": "notes.txt", "format": "Text", "size": "2", "md5": "ccc"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/advancedsearch.php":
            return _search_response(["gd77"], 1)
        if request.url.path == "/metadata/gd77":
            return _metadata_response(
                {
                    "title": "Cornell 5/8/77",
                    "creator": "Grateful Dead",
                    "date": "1977-05-08",
                    "venue": "Barton Hall",
                    "coverage": "Ithaca, NY",
                },
                files,
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    plugin = _make_plugin(handler)
    items = list(plugin.discover(_collection(format_filters=["flac"])))

    assert len(items) == 1
    item = items[0]
    assert item.identifier == "gd77"
    # Only the flac survives the keep-filter.
    names = {f.name for f in item.manifest.files}
    assert names == {"show.flac"}
    mf = item.manifest.files[0]
    assert mf.size == 10
    assert mf.md5 == "aaa"
    # Every advertised template field is populated.
    for key in EtreePlugin.template_fields:
        assert key in item.metadata
    assert item.metadata["identifier"] == "gd77"
    assert item.metadata["title"] == "Cornell 5/8/77"
    assert item.metadata["year"] == "1977"
    assert item.metadata["venue"] == "Barton Hall"
    assert not item.is_restricted


def test_discover_exclude_filters_drop_files() -> None:
    files = [
        {"name": "a.flac", "format": "Flac", "size": "1", "md5": "1"},
        {"name": "a_vbr.mp3", "format": "VBR MP3", "size": "1", "md5": "2"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/advancedsearch.php":
            return _search_response(["x"], 1)
        return _metadata_response({"date": "1990-01-01"}, files)

    plugin = _make_plugin(handler)
    # Empty format_filters keeps everything not excluded.
    items = list(plugin.discover(_collection(exclude_filters=["*_vbr.mp3"])))
    names = {f.name for f in items[0].manifest.files}
    assert names == {"a.flac"}


def test_discover_paginates_across_pages() -> None:
    # 3 items total, page size in the plugin is 100 but numFound drives the loop;
    # we return one doc per page and a numFound of 3 to force three requests.
    pages: dict[str, list[str]] = {"1": ["a"], "2": ["b"], "3": ["c"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/advancedsearch.php":
            page = request.url.params.get("page", "1")
            return _search_response(pages.get(page, []), 3)
        ident = request.url.path.rsplit("/", 1)[-1]
        return _metadata_response({"identifier": ident, "date": "2000"}, [])

    plugin = _make_plugin(handler)
    items = list(plugin.discover(_collection()))
    assert [i.identifier for i in items] == ["a", "b", "c"]


def test_discover_raises_on_enumeration_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/advancedsearch.php":
            return httpx.Response(500, text="boom")
        raise AssertionError("metadata should not be reached")

    plugin = _make_plugin(handler)
    with pytest.raises(httpx.HTTPStatusError):
        list(plugin.discover(_collection()))


def test_discover_marks_restricted_item() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/advancedsearch.php":
            return _search_response(["dark1"], 1)
        return _metadata_response(
            {"date": "1980", "access-restricted-item": "true"},
            [{"name": "x.flac", "format": "Flac", "size": "1", "md5": "1"}],
        )

    plugin = _make_plugin(handler)
    items = list(plugin.discover(_collection()))
    assert items[0].is_restricted is True
    assert items[0].restriction_reason == "access-restricted-item"


# -- fetch ------------------------------------------------------------------


def _item_with_files(files: list[ManifestFile], *, restricted: bool = False) -> ItemDescriptor:
    return ItemDescriptor(
        identifier="gd77",
        manifest=Manifest(files=tuple(files)),
        is_restricted=restricted,
        restriction_reason="dark" if restricted else None,
    )


def test_fetch_writes_files_and_reports_bytes(tmp_path: Path) -> None:
    content = b"flac-bytes"
    md5 = hashlib.md5(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/download/gd77/show.flac"
        return httpx.Response(200, content=content)

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", len(content), md5)])
    result = plugin.fetch(item, tmp_path, [], [])

    assert result.success is True
    assert result.bytes_downloaded == len(content)
    assert (tmp_path / "show.flac").read_bytes() == content


def test_fetch_retriable_on_503(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", None, None)])
    result = plugin.fetch(item, tmp_path, [], [])

    assert result.success is False
    assert result.retriable is True


def test_fetch_retriable_on_checksum_mismatch(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"wrong-bytes")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", None, "deadbeef")])
    result = plugin.fetch(item, tmp_path, [], [])

    assert result.success is False
    assert result.retriable is True
    assert "checksum" in (result.error or "")
    # Nothing was written on a checksum failure.
    assert not (tmp_path / "show.flac").exists()


def test_fetch_non_retriable_on_404(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("gone.flac", None, None)])
    result = plugin.fetch(item, tmp_path, [], [])

    assert result.success is False
    assert result.retriable is False


def test_fetch_parses_numeric_retry_after(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "42"}, text="slow down")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", None, None)])
    result = plugin.fetch(item, tmp_path, [], [])

    assert result.success is False
    assert result.retriable is True
    assert result.retry_after == 42.0


def test_fetch_fails_fast_on_restricted_without_download(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=b"should-not-happen")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", None, None)], restricted=True)
    result = plugin.fetch(item, tmp_path, [], [])

    assert result.success is False
    assert result.retriable is False
    assert result.bytes_downloaded == 0
    assert calls == []  # no download attempted


def test_fetch_rejects_path_traversal_file_name(tmp_path: Path) -> None:
    """A remote file name that escapes the item dir is refused without a write."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=b"pwned")

    dest = tmp_path / "item"
    dest.mkdir()
    plugin = _make_plugin(handler)
    for evil in ("../escape.flac", "/etc/evil.flac", "a/../../escape.flac", "", "."):
        item = _item_with_files([ManifestFile(evil, None, None)])
        result = plugin.fetch(item, dest, [], [])
        assert result.success is False
        assert result.retriable is False
        assert "unsafe file name" in (result.error or "")
    assert calls == []  # no download attempted for any traversal name
    assert not (tmp_path / "escape.flac").exists()


def test_fetch_is_idempotent_over_existing_file(tmp_path: Path) -> None:
    content = b"flac-bytes"
    md5 = hashlib.md5(content).hexdigest()
    (tmp_path / "show.flac").write_bytes(content)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no download expected when file already present")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", len(content), md5)])
    result = plugin.fetch(item, tmp_path, [], [])

    assert result.success is True
    assert result.bytes_downloaded == len(content)


# -- verify -----------------------------------------------------------------


def test_verify_reports_missing_and_ok(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("verify performs no HTTP")

    plugin = _make_plugin(handler)
    item = _item_with_files(
        [ManifestFile("a.flac", None, None), ManifestFile("b.flac", None, None)]
    )

    missing = plugin.verify(item, tmp_path)
    assert missing.ok is False
    assert set(missing.missing_files) == {"a.flac", "b.flac"}

    (tmp_path / "a.flac").write_bytes(b"1")
    (tmp_path / "b.flac").write_bytes(b"2")
    present = plugin.verify(item, tmp_path)
    assert present.ok is True
    assert present.missing_files == []


# -- registration -----------------------------------------------------------


def test_etree_registered_in_registry() -> None:
    from shakedown.plugins import registry

    assert registry.plugin_class("etree") is EtreePlugin


def test_example_config_etree_layout_validates() -> None:
    # The example YAML's etree library_layout references only advertised fields,
    # so config validation (which checks layouts against template_fields) passes.
    from shakedown.config import load

    root = Path(__file__).resolve().parent.parent
    config = load(root / "shakedown.example.yaml")
    etree_sources = [s for s in config.sources if s.type == "etree"]
    assert etree_sources, "example config should define an etree source"
