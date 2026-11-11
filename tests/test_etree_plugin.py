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
from shakedown.discovery import ItemDescriptor, SourceBudget, look_up_one
from shakedown.models import Manifest, ManifestFile
from shakedown.plugins.base import ListedItem
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


def _collection(*, query: str = "collection:etree") -> CollectionConfig:
    return CollectionConfig(name="c", query=query)


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


# -- list / look up ------------------------------------------------------------

MD5 = "a" * 32


def _look_up(plugin: EtreePlugin, identifier: str):
    """The core's lookup of one listed item, as sync runs it."""
    return look_up_one(plugin, ListedItem(identifier), SourceBudget(2))


def _gd77_handler(files: list[dict[str, Any]], metadata: dict[str, Any] | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metadata/gd77":
            return _metadata_response(
                metadata
                or {
                    "title": "Cornell 5/8/77",
                    "creator": "Grateful Dead",
                    "date": "1977-05-08",
                    "venue": "Barton Hall",
                    "coverage": "Ithaca, NY",
                },
                files,
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    return handler


def test_lookup_describes_item_with_core_selected_manifest_and_metadata() -> None:
    files = [
        {"name": "show.flac", "format": "Flac", "size": "10", "md5": MD5},
        {"name": "show_64kb.mp3", "format": "VBR MP3", "size": "3", "md5": "b" * 32},
        {"name": "notes.txt", "format": "Text", "size": "2", "md5": "c" * 32},
    ]
    plugin = _make_plugin(_gd77_handler(files))

    item = _look_up(plugin, "gd77")

    assert isinstance(item, ItemDescriptor)
    assert item.identifier == "gd77"
    # The best copy (the flac) and every non-audio file; no lossy derivative.
    assert [f.name for f in item.manifest.files] == ["notes.txt", "show.flac"]
    mf = item.manifest.files[1]
    assert mf.size == 10
    assert mf.md5 == MD5
    # Every advertised template field is populated.
    for key in EtreePlugin.template_fields:
        assert key in item.metadata
    assert item.metadata["identifier"] == "gd77"
    assert item.metadata["title"] == "Cornell 5/8/77"
    assert item.metadata["year"] == "1977"
    assert item.metadata["venue"] == "Barton Hall"
    assert not item.is_restricted


def test_lookup_item_passes_every_raw_file_through() -> None:
    files = [
        {"name": "d1t01.shn", "format": "Shorten", "size": "9", "md5": MD5, "private": "true"},
        {"name": "d1t01.mp3", "format": "VBR MP3", "size": "3", "md5": "b" * 32,
         "original": "d1t01.shn", "bitrate": "192"},
    ]
    plugin = _make_plugin(_gd77_handler(files))

    answer = plugin.lookup_item("gd77", 30.0)

    shn, mp3 = answer.files
    assert (shn["name"], shn["refused"], shn["derived_from"]) == ("d1t01.shn", True, None)
    assert (mp3["refused"], mp3["derived_from"], mp3["bitrate"]) == (False, "d1t01.shn", "192")


def test_lookup_passes_the_core_timeout() -> None:
    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return _metadata_response({"date": "1977"}, [])

    _make_plugin(handler).lookup_item("gd77", 12.5)
    assert seen == [{"connect": 12.5, "read": 12.5, "write": 12.5, "pool": 12.5}]


def test_refused_lossless_falls_back_like_ia() -> None:
    """A stream-only show: archive.org flags the item ``access-restricted-item`` and
    marks its lossless files ``private``, while the MP3s stay public. etree gets the
    same core selection as IA: the best lossy copy, with the reason."""
    from shakedown.selection import LOSSLESS_REFUSED_REASON

    files = [
        {"name": "d1t01.shn", "format": "Shorten", "size": "9", "md5": MD5, "private": "true"},
        {"name": "d1t01.mp3", "format": "VBR MP3", "size": "3", "md5": "b" * 32},
    ]
    plugin = _make_plugin(
        _gd77_handler(files, {"date": "1969-12-26", "access-restricted-item": "true"})
    )
    item = _look_up(plugin, "gd77")

    assert isinstance(item, ItemDescriptor)
    assert item.is_restricted is False
    assert [f.name for f in item.manifest.files] == ["d1t01.mp3"]
    assert item.restriction_reason == LOSSLESS_REFUSED_REASON


def test_listing_paginates_across_pages() -> None:
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
    items = list(plugin.list_items(_collection()))
    assert [i.identifier for i in items] == ["a", "b", "c"]


def test_listing_raises_when_listing_ends_short_of_num_found() -> None:
    """An early empty page is a failed listing, not the whole collection: treating
    it as complete would mark every unlisted show disappeared (or prune it)."""
    pages: dict[str, list[str]] = {"1": ["a"], "2": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/advancedsearch.php":
            page = request.url.params.get("page", "1")
            return _search_response(pages.get(page, []), 3)
        ident = request.url.path.rsplit("/", 1)[-1]
        return _metadata_response({"identifier": ident}, [])

    plugin = _make_plugin(handler)
    with pytest.raises(RuntimeError, match="1 of 3"):
        list(plugin.list_items(_collection()))


def test_listing_raises_when_page_ceiling_hit_short_of_num_found(monkeypatch) -> None:
    import shakedown.plugins.etree.plugin as etree_module

    monkeypatch.setattr(etree_module, "_MAX_PAGES", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/advancedsearch.php":
            page = request.url.params.get("page", "1")
            return _search_response([f"id{page}"], 1_000)
        ident = request.url.path.rsplit("/", 1)[-1]
        return _metadata_response({"identifier": ident}, [])

    plugin = _make_plugin(handler)
    with pytest.raises(RuntimeError, match="2 of 1000"):
        list(plugin.list_items(_collection()))


def test_listing_raises_when_unstable_paging_repeats_an_item() -> None:
    """A duplicate row must not stand in for a skipped show: count unique ids."""
    pages: dict[str, list[str]] = {"1": ["a", "b"], "2": ["b"]}
    seen_sort: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/advancedsearch.php":
            seen_sort.append(request.url.params.get("sort[]"))
            page = request.url.params.get("page", "1")
            return _search_response(pages.get(page, []), 3)
        ident = request.url.path.rsplit("/", 1)[-1]
        return _metadata_response({"identifier": ident}, [])

    plugin = _make_plugin(handler)
    with pytest.raises(RuntimeError, match="2 of 3"):
        ids = [d.identifier for d in plugin.list_items(_collection())]
        assert ids == ["a", "b"]  # never yields "b" twice
    assert seen_sort and all(s == "identifier asc" for s in seen_sort)


def test_listing_empty_collection_is_complete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _search_response([], 0)

    plugin = _make_plugin(handler)
    assert list(plugin.list_items(_collection())) == []


def test_listing_raises_on_enumeration_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/advancedsearch.php":
            return httpx.Response(500, text="boom")
        raise AssertionError("metadata should not be reached")

    plugin = _make_plugin(handler)
    with pytest.raises(httpx.HTTPStatusError):
        list(plugin.list_items(_collection()))


def test_lookup_marks_access_restricted_item() -> None:
    files = [{"name": "x.flac", "format": "Flac", "size": "1", "md5": MD5}]
    plugin = _make_plugin(_gd77_handler(files, {"date": "1980", "access-restricted": "true"}))

    item = _look_up(plugin, "gd77")
    assert isinstance(item, ItemDescriptor)
    assert item.is_restricted is True
    assert item.restriction_reason == "access-restricted"


# -- fetch ------------------------------------------------------------------


def _item_with_files(files: list[ManifestFile]) -> ItemDescriptor:
    return ItemDescriptor(identifier="gd77", manifest=Manifest(files=tuple(files)))


def test_fetch_writes_files_and_reports_bytes(tmp_path: Path) -> None:
    content = b"flac-bytes"
    md5 = hashlib.md5(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/download/gd77/show.flac"
        return httpx.Response(200, content=content)

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", len(content), md5)])
    result = plugin.fetch(item.identifier, item.manifest.files, tmp_path)

    assert result.success is True
    assert result.bytes_downloaded == len(content)
    assert (tmp_path / "show.flac").read_bytes() == content


def test_fetch_retriable_on_503(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", None, None)])
    result = plugin.fetch(item.identifier, item.manifest.files, tmp_path)

    assert result.success is False
    assert result.retriable is True


def test_fetch_retriable_on_checksum_mismatch(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"wrong-bytes")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", None, "deadbeef")])
    result = plugin.fetch(item.identifier, item.manifest.files, tmp_path)

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
    result = plugin.fetch(item.identifier, item.manifest.files, tmp_path)

    assert result.success is False
    assert result.retriable is False


def test_fetch_reports_a_refused_download_unavailable(tmp_path: Path) -> None:
    """archive.org answers 403 for stream-only shows whose metadata carries no
    restriction flag; that is a refusal, not a failure (§spec:failure-behavior)."""
    from shakedown.selection import DOWNLOAD_REFUSED_REASON

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("sbd.flac", None, None)])
    result = plugin.fetch(item.identifier, item.manifest.files, tmp_path)

    assert result.success is False
    assert result.unavailable is True
    assert result.retriable is False
    assert result.error == DOWNLOAD_REFUSED_REASON


def test_remote_names_are_quoted_in_request_urls(tmp_path: Path) -> None:
    """A '#' or '?' in a name must reach archive.org as part of the path."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        if request.url.path.startswith("/metadata/"):
            return _metadata_response({"date": "1977"}, [])
        return httpx.Response(200, content=b"x")

    plugin = _make_plugin(handler)
    plugin.lookup_item("gd#1", 30.0)
    plugin.fetch("gd77", [ManifestFile("disc 1/t#1?.flac", 1, None)], tmp_path)

    assert seen == ["/metadata/gd%231", "/download/gd77/disc%201/t%231%3F.flac"]


def test_fetch_parses_numeric_retry_after(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "42"}, text="slow down")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", None, None)])
    result = plugin.fetch(item.identifier, item.manifest.files, tmp_path)

    assert result.success is False
    assert result.retriable is True
    assert result.retry_after == 42.0


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
        result = plugin.fetch(item.identifier, item.manifest.files, dest)
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
    result = plugin.fetch(item.identifier, item.manifest.files, tmp_path)

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

    missing = plugin.verify(item.manifest.files, tmp_path)
    assert missing.ok is False
    assert set(missing.missing_files) == {"a.flac", "b.flac"}

    (tmp_path / "a.flac").write_bytes(b"1")
    (tmp_path / "b.flac").write_bytes(b"2")
    present = plugin.verify(item.manifest.files, tmp_path)
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


def test_retry_after_late_failure_fetches_only_the_missing_file(
    tmp_path: Path, monkeypatch
) -> None:
    """One late error must not re-download a whole multi-gigabyte show: the next
    attempt keeps files that match the manifest and fetches only the rest."""
    import shakedown.sync as sync_module

    monkeypatch.setattr(sync_module.time, "sleep", lambda d: None)
    contents = {f"t{i:02d}.flac": f"track {i}".encode() for i in range(1, 4)}
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        requests.append(name)
        if name == "t03.flac" and requests.count(name) == 1:
            return httpx.Response(503, text="timeout")
        return httpx.Response(200, content=contents[name])

    plugin = _make_plugin(handler)
    item = _item_with_files([
        ManifestFile(n, len(c), hashlib.md5(c).hexdigest()) for n, c in contents.items()
    ])
    tmp_dir = tmp_path / ".tmp-gd77"

    result = sync_module._fetch_with_retries(plugin, item, tmp_dir)

    assert result.success is True
    assert requests == ["t01.flac", "t02.flac", "t03.flac", "t03.flac"]
    assert {p.name: p.read_bytes() for p in tmp_dir.iterdir()} == contents


def test_fetch_refetches_leftover_file_that_does_not_match(tmp_path: Path) -> None:
    content = b"flac-bytes"
    (tmp_path / "show.flac").write_bytes(b"FLAC-BYTES")  # same size, wrong bytes
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, content=content)

    plugin = _make_plugin(handler)
    item = _item_with_files(
        [ManifestFile("show.flac", len(content), hashlib.md5(content).hexdigest())]
    )
    result = plugin.fetch(item.identifier, item.manifest.files, tmp_path)

    assert result.success is True
    assert requests == ["/download/gd77/show.flac"]
    assert (tmp_path / "show.flac").read_bytes() == content


def test_fetch_refetches_leftover_file_it_cannot_check(tmp_path: Path) -> None:
    (tmp_path / "show.flac").write_bytes(b"partial")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, content=b"complete")

    plugin = _make_plugin(handler)
    item = _item_with_files([ManifestFile("show.flac", None, None)])
    assert plugin.fetch(item.identifier, item.manifest.files, tmp_path).success is True
    assert requests == ["/download/gd77/show.flac"]
    assert (tmp_path / "show.flac").read_bytes() == b"complete"


def test_listing_raises_when_num_found_falls_mid_listing() -> None:
    """A total that drops between pages must not lower the completeness bar."""
    pages = {"1": (["a", "b"], 3), "2": ([], 2)}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/advancedsearch.php":
            ids, total = pages.get(request.url.params.get("page", "1"), ([], 0))
            return _search_response(ids, total)
        ident = request.url.path.rsplit("/", 1)[-1]
        return _metadata_response({"identifier": ident}, [])

    plugin = _make_plugin(handler)
    with pytest.raises(RuntimeError, match="2 of 3"):
        list(plugin.list_items(_collection()))


def test_files_index_is_mirrored_without_its_unmatchable_checksum() -> None:
    """Same archive.org quirk as on the ia source: `<identifier>_files.xml` lists a
    checksum it can never match, so the core must not check it."""
    files = [
        {"name": "show.flac", "format": "Flac", "size": "10", "md5": MD5},
        {"name": "gd77_files.xml", "format": "Metadata", "md5": "b" * 32},
    ]
    item = _look_up(_make_plugin(_gd77_handler(files)), "gd77")
    assert isinstance(item, ItemDescriptor)
    by_name = {f.name: f for f in item.manifest.files}
    assert by_name["gd77_files.xml"].md5 is None
    assert by_name["show.flac"].md5 == MD5
