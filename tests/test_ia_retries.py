"""Exactly one retry layer talks to archive.org per request kind (SPEC §spec:failure-behavior).

The `internetarchive` library re-enables its own retries: a search remounts five
urllib3 retries on the session, and a file download forces at least two library
retries on top of urllib3's. These tests drive the real plugin over a fake archive.org
below urllib3 and count the requests that would reach it.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import requests
import urllib3.exceptions
from requests.adapters import HTTPAdapter

import shakedown.sync as sync_module
from shakedown.config import CollectionConfig, SourceConfig
from shakedown.discovery import LOOKUP_ATTEMPTS, ItemDescriptor, SourceBudget, look_up_one
from shakedown.plugins.ia import plugin as ia_plugin
from shakedown.plugins.ia.plugin import IAPlugin
from tests.fake_archive import FakeArchive, Reply, fake_archive

AUDIO = b"audio"
AUDIO_MD5 = "a5ca0b5894324f8bb54bb9fffad29d1e"
FILES = [{"name": "d1t01.flac", "size": "5", "md5": AUDIO_MD5, "format": "Flac"}]
COLLECTION = CollectionConfig(name="dead", query="collection:GratefulDead")


@pytest.fixture()
def archive(monkeypatch) -> Iterator[FakeArchive]:
    monkeypatch.setattr(sync_module.time, "sleep", lambda _s: None)
    with fake_archive() as fake:
        yield fake


def _plugin() -> IAPlugin:
    return IAPlugin(SourceConfig(name="ia", type="ia", collections=[COLLECTION]))


def _search_then_describe(archive: FakeArchive, plugin: IAPlugin, identifier: str = "gd1"):
    """List the collection, then run the core's lookup of its one item."""
    archive.search_results({"identifier": identifier})
    (row,) = plugin.list_items(COLLECTION)
    assert row.identifier == identifier
    return look_up_one(plugin, row, SourceBudget(4))


def test_metadata_5xx_after_a_search_is_asked_once_per_shakedown_attempt(archive) -> None:
    """A search must not leave its retries on the session the metadata lookups use, and
    a 5xx must reach shakedown's own retry (the library strips the response from it)."""
    plugin = _plugin()
    archive.metadata("gd1", Reply(500, b"oops"))

    assert not isinstance(_search_then_describe(archive, plugin), ItemDescriptor)
    assert len(archive.calls_to("/metadata/gd1")) == LOOKUP_ATTEMPTS


def test_metadata_retry_after_reaches_shakedown(archive, monkeypatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr("shakedown.discovery._sleep", delays.append)
    plugin = _plugin()
    archive.metadata(
        "gd1",
        Reply(429, b"slow down", {"Retry-After": "7"}),
        Reply(200, b'{"files": [], "metadata": {"date": "1977-05-08"}}'),
    )

    desc = _search_then_describe(archive, plugin)

    assert isinstance(desc, ItemDescriptor)
    assert delays == [7.0]
    assert len(archive.calls_to("/metadata/gd1")) == 2


def test_download_5xx_after_a_search_is_one_request_and_retriable(archive, tmp_path: Path) -> None:
    plugin = _plugin()
    archive.item("gd1", FILES)
    archive.file("gd1", "d1t01.flac", Reply(503, b"busy", {"Retry-After": "7"}))
    desc = _search_then_describe(archive, plugin)
    assert isinstance(desc, ItemDescriptor)

    result = plugin.fetch(desc.identifier, desc.manifest.files, tmp_path / ".tmp-gd1")

    assert result.success is False
    assert result.retriable is True
    assert result.retry_after == 7.0
    assert len(archive.calls_to("/download/gd1/d1t01.flac")) == 1


def test_hours_long_retry_after_on_a_download_waits_only_the_core_cap(archive, monkeypatch, tmp_path: Path) -> None:
    """A Retry-After of six hours must not hold a request slot for six hours."""
    delays: list[float] = []
    monkeypatch.setattr(sync_module.time, "sleep", delays.append)
    plugin = _plugin()
    archive.item("gd1", FILES)
    archive.file(
        "gd1", "d1t01.flac",
        Reply(429, b"slow down", {"Retry-After": "21600"}),
        Reply(200, AUDIO),
    )
    desc = _search_then_describe(archive, plugin)
    assert isinstance(desc, ItemDescriptor)

    result = sync_module._fetch_with_retries(plugin, desc, tmp_path / ".tmp-gd1")

    assert result.success is True
    assert delays == [sync_module._MAX_BACKOFF_SECONDS]
    assert archive.sleeps == []  # nothing below shakedown waited
    assert len(archive.calls_to("/download/gd1/d1t01.flac")) == 2


def test_search_retries_are_bounded_and_cap_retry_after(archive) -> None:
    """The search has no shakedown retry loop around it, so the session's own retry is
    the one layer: bounded, and never waiting longer than shakedown's cap."""
    plugin = _plugin()
    archive.route(
        "/services/search/v1/scrape", Reply(503, b"busy", {"Retry-After": "21600"})
    )

    with pytest.raises(requests.exceptions.RetryError):
        list(plugin.list_items(COLLECTION))

    assert len(archive.calls_to("/services/search/v1/scrape")) == 1 + ia_plugin._SEARCH_RETRIES
    assert archive.sleeps
    assert max(archive.sleeps) <= ia_plugin._SEARCH_MAX_BACKOFF_SECONDS


def test_session_used_for_metadata_and_downloads_keeps_retries_off(archive, tmp_path: Path) -> None:
    plugin = _plugin()
    archive.item("gd1", FILES)
    archive.file("gd1", "d1t01.flac", Reply(200, AUDIO))
    desc = _search_then_describe(archive, plugin)
    assert isinstance(desc, ItemDescriptor)
    assert plugin.fetch(desc.identifier, desc.manifest.files, tmp_path / ".tmp-gd1").success

    adapter = plugin._session.get_adapter("https://archive.org/download/gd1/x")
    assert isinstance(adapter, HTTPAdapter)
    assert adapter.max_retries.total == 0


def test_403_on_a_download_reports_the_show_unavailable(archive, tmp_path: Path) -> None:
    """archive.org answers 403 for stream-only shows (often soundboards) whose
    metadata carries no restriction flag."""
    plugin = _plugin()
    files = [*FILES, {"name": "d1t02.flac", "size": "5", "md5": AUDIO_MD5, "format": "Flac"}]
    archive.item("gd1", files)
    archive.file("gd1", "d1t01.flac", Reply(403, b"Forbidden"))
    archive.file("gd1", "d1t02.flac", Reply(200, AUDIO))
    desc = _search_then_describe(archive, plugin)
    assert isinstance(desc, ItemDescriptor)

    result = plugin.fetch(desc.identifier, desc.manifest.files, tmp_path / ".tmp-gd1")

    assert result.success is False
    assert result.unavailable is True
    assert result.retriable is False
    assert result.error == ia_plugin.DOWNLOAD_REFUSED_REASON
    assert archive.calls_to("/download/gd1/d1t02.flac") == [], "stop at the first refusal"


def test_download_cut_off_mid_stream_is_retriable(archive, tmp_path: Path) -> None:
    """A connection that drops mid-file must reach the core's bounded retry."""
    plugin = _plugin()
    archive.item("gd1", FILES)
    archive.file("gd1", "d1t01.flac", Reply(200, AUDIO[:3], {"Content-Length": "5"}))
    desc = _search_then_describe(archive, plugin)
    assert isinstance(desc, ItemDescriptor)

    result = plugin.fetch(desc.identifier, desc.manifest.files, tmp_path / ".tmp-gd1")

    assert result.success is False
    assert result.retriable is True


def test_search_retries_are_reported_to_the_core(archive) -> None:
    """The listing is the one request the plugin retries itself; each retry reaches
    the core with its cause and the wait urllib3 takes (§spec:network-activity)."""
    from shakedown.plugins.base import FailureCause, ListingRetry, listing_retries_to

    plugin = _plugin()
    archive.route(
        "/services/search/v1/scrape",
        Reply(429, b"slow down", {"Retry-After": "3"}),
        urllib3.exceptions.ProtocolError("Connection aborted"),
        Reply(200, b'{"items": [{"identifier": "gd1"}], "count": 1, "total": 1}',
              {"Content-Type": "application/json"}),
    )
    heard: list[ListingRetry] = []

    with listing_retries_to(heard.append):
        rows = list(plugin.list_items(COLLECTION))

    assert [r.identifier for r in rows] == ["gd1"]
    assert [(r.cause, r.http_status) for r in heard] == [
        (FailureCause.RATE_LIMITED, 429),
        (FailureCause.CONNECTION_ERROR, None),
    ]
    assert heard[0].wait_seconds == 3.0
    assert archive.sleeps[0] == 3.0


@pytest.mark.parametrize(
    ("reply", "cause", "status"),
    [
        (Reply(503, b"busy"), "server_error", 503),
        (Reply(429, b"slow", {"Retry-After": "5"}), "rate_limited", 429),
        (Reply(403, b"Forbidden"), "refused", 403),
    ],
)
def test_a_failed_download_names_its_cause(archive, tmp_path: Path, reply, cause, status) -> None:
    plugin = _plugin()
    archive.item("gd1", FILES)
    archive.file("gd1", "d1t01.flac", reply)
    desc = _search_then_describe(archive, plugin)
    assert isinstance(desc, ItemDescriptor)

    result = plugin.fetch(desc.identifier, desc.manifest.files, tmp_path / ".tmp-gd1")

    assert (result.success, result.cause, result.http_status) == (False, cause, status)


def test_a_download_cut_off_mid_stream_is_a_connection_error(archive, tmp_path: Path) -> None:
    plugin = _plugin()
    archive.item("gd1", FILES)
    archive.file("gd1", "d1t01.flac", Reply(200, AUDIO[:3], {"Content-Length": "5"}))
    desc = _search_then_describe(archive, plugin)
    assert isinstance(desc, ItemDescriptor)

    result = plugin.fetch(desc.identifier, desc.manifest.files, tmp_path / ".tmp-gd1")

    assert result.cause == "connection_error"
