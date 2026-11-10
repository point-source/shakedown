"""IA plugin discover/fetch/verify with the internetarchive package mocked.

The pure helpers (_build_manifest, _detect_restriction, _file_matches_filters)
are spot-checked from a different angle: end-to-end via the plugin facade.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.plugins.ia.plugin import IAPlugin
from tests.fake_archive import FakeArchive, Reply, fake_archive

SHOW = {"title": "Show", "date": "1977-05-08"}


@pytest.fixture()
def archive() -> Iterator[FakeArchive]:
    """archive.org, faked below urllib3 (see tests/fake_archive.py)."""
    with fake_archive() as fake:
        yield fake


def _make_plugin() -> IAPlugin:
    src = SourceConfig(
        name="ia",
        type="ia",
        collections=[CollectionConfig(name="dead", query="collection:GratefulDead")],
    )
    return IAPlugin(src)


def _flac_item(archive: FakeArchive, identifier: str, metadata: dict | None = None) -> None:
    archive.item(
        identifier, [{"name": "x.flac", "size": "1", "md5": "z", "format": "Flac"}], metadata or SHOW
    )


def test_discover_yields_descriptors_with_manifests(archive) -> None:
    plugin = _make_plugin()
    archive.search_results({"identifier": "gd1977-05-08"})
    archive.item(
        "gd1977-05-08",
        [
            {"name": "d1t01.flac", "size": "1000", "md5": "aaa", "format": "Flac"},
            {"name": "d1t01_64kb.mp3", "size": "100", "md5": "bbb", "format": "VBR MP3"},
        ],
        SHOW,
    )

    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])
    descriptors = list(plugin.discover(coll))
    assert len(descriptors) == 1
    desc = descriptors[0]
    assert desc.identifier == "gd1977-05-08"
    assert [f.name for f in desc.manifest.files] == ["d1t01.flac"]
    assert desc.manifest.files[0].md5 == "aaa"
    assert desc.metadata["year"] == "1977"  # derived from date
    assert desc.is_restricted is False


def test_discover_surfaces_combined_change_signal(archive) -> None:
    plugin = _make_plugin()
    archive.search_results(
        {"identifier": "gd1977-05-08", "oai_updatedate": "2024-05-01T00:00:00Z", "item_size": 12345}
    )
    archive.item(
        "gd1977-05-08", [{"name": "d1t01.flac", "size": "1000", "md5": "aaa", "format": "Flac"}]
    )

    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])
    desc = next(plugin.discover(coll))
    assert desc.change_signal == "2024-05-01T00:00:00Z|12345"


def test_combine_change_signal_handles_list_and_missing() -> None:
    from shakedown.plugins.ia.plugin import _combine_change_signal

    assert _combine_change_signal(
        {"oai_updatedate": ["2020-01-01", "2024-05-01"], "item_size": 99}
    ) == "2024-05-01|99"
    assert _combine_change_signal({"identifier": "x"}) is None
    # Size alone is not monotone in contents (a same-size re-derivation would
    # be missed), so a signal missing oai_updatedate falls through to the full
    # comparison rather than engaging the skip — §spec:incremental-discovery
    # correctness bound.
    assert _combine_change_signal({"item_size": 42}) is None


def test_enumerate_with_signals_requests_extra_fields(archive) -> None:
    plugin = _make_plugin()
    archive.search_results()

    coll = CollectionConfig(name="dead", query="x")
    list(plugin.enumerate_with_signals(coll))
    (search,) = archive.calls_to("/services/search/v1/scrape")
    assert "fields=identifier%2Coai_updatedate%2Citem_size" in search.url


def test_discover_marks_restricted_items(archive) -> None:
    plugin = _make_plugin()
    archive.search_results({"identifier": "gd-stream-only"})
    _flac_item(
        archive,
        "gd-stream-only",
        metadata={
            "date": "1977-05-08",
            "access-restricted": "true",
            "access-restricted-item-reason": "stream-only soundboard per band request",
        },
    )

    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])
    desc = next(plugin.discover(coll))
    assert desc.is_restricted is True
    assert "soundboard" in (desc.restriction_reason or "")


def test_discover_skips_items_whose_metadata_fetch_raises(archive, monkeypatch) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: None)

    plugin = _make_plugin()
    archive.search_results({"identifier": "good-1"}, {"identifier": "bad-1"}, {"identifier": "good-2"})
    _flac_item(archive, "good-1")
    _flac_item(archive, "good-2")
    # Retriable transient fault → retried up to the cap, then skipped.
    archive.metadata("bad-1", ConnectionResetError("connection reset by peer"))
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])
    descriptors = list(plugin.discover(coll))
    assert {d.identifier for d in descriptors} == {"good-1", "good-2"}


def test_describe_item_retries_transient_fault_then_resolves(archive, monkeypatch) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    delays: list[float] = []
    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: delays.append(d))

    plugin = _make_plugin()
    _flac_item(archive, "gd-transient")
    good = archive.routes["/metadata/gd-transient"][0]
    archive.metadata("gd-transient", Reply(503), Reply(503), good)
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    desc = plugin.describe_item("gd-transient", coll)
    assert desc is not None
    assert desc.identifier == "gd-transient"
    # two transient failures, third succeeds
    assert len(archive.calls_to("/metadata/gd-transient")) == 3
    # Two backoff sleeps recorded (attempts 1 and 2 failed transiently).
    assert delays == [ia_plugin._metadata_backoff_seconds(1), ia_plugin._metadata_backoff_seconds(2)]


def test_describe_item_gives_up_after_max_attempts(archive, monkeypatch) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: None)

    plugin = _make_plugin()
    archive.metadata("gd-down", Reply(503, b"still down"))
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    assert plugin.describe_item("gd-down", coll) is None
    assert len(archive.calls_to("/metadata/gd-down")) == ia_plugin._MAX_METADATA_ATTEMPTS


def test_describe_item_does_not_retry_non_retriable_fault(archive, monkeypatch) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: None)

    plugin = _make_plugin()
    archive.metadata("gd-bad", Reply(200, b"malformed metadata"))
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    assert plugin.describe_item("gd-bad", coll) is None
    # non-retriable → exactly one attempt
    assert len(archive.calls_to("/metadata/gd-bad")) == 1


def test_describe_item_honors_retry_after_header(archive, monkeypatch) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    delays: list[float] = []
    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: delays.append(d))

    plugin = _make_plugin()
    _flac_item(archive, "gd-429")
    good = archive.routes["/metadata/gd-429"][0]
    archive.metadata("gd-429", Reply(429, b"rate limited", {"Retry-After": "7"}), good)
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    desc = plugin.describe_item("gd-429", coll)
    assert desc is not None
    assert delays == [7.0]  # honored the Retry-After delta, not the backoff schedule


def test_describe_item_clamps_hostile_retry_after(archive, monkeypatch) -> None:
    """A describe worker holds a shared SourceBudget slot while it sleeps, so an
    upstream-controlled Retry-After must be clamped to the backoff cap — a hostile
    endpoint can't pin the slot for an attacker-chosen wait."""
    from shakedown.plugins.ia import plugin as ia_plugin

    delays: list[float] = []
    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: delays.append(d))

    plugin = _make_plugin()
    _flac_item(archive, "gd-evil")
    good = archive.routes["/metadata/gd-evil"][0]
    archive.metadata("gd-evil", Reply(429, b"rate limited", {"Retry-After": "31536000"}), good)
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    desc = plugin.describe_item("gd-evil", coll)
    assert desc is not None
    assert delays == [ia_plugin._METADATA_MAX_BACKOFF_SECONDS]  # clamped, not a year


def test_describe_item_passes_bounded_timeout(archive) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    plugin = _make_plugin()
    _flac_item(archive, "gd-timeout")
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    assert plugin.describe_item("gd-timeout", coll) is not None
    (call,) = archive.calls_to("/metadata/gd-timeout")
    assert (call.timeout.connect_timeout, call.timeout.read_timeout) == ia_plugin._METADATA_TIMEOUT


def _served_calls(archive: FakeArchive, identifier: str) -> list[str]:
    """File names downloaded, in request order."""
    prefix = f"/download/{identifier}/"
    return [c.path[len(prefix):] for c in archive.calls if c.path.startswith(prefix)]


def _serve(archive: FakeArchive, identifier: str, served: dict[str, bytes]) -> None:
    for name, content in served.items():
        archive.file(identifier, name, Reply(200, content))


def test_fetch_writes_files_directly_into_dest(archive, tmp_path: Path) -> None:
    """Files land at <dest>/<file>, never under an IA <dest>/<id>/ subdir.

    The plugin writes into the core-owned temp dir it is handed; the core owns the
    atomic promotion into the archive (see test_sync.py)."""
    plugin = _make_plugin()
    dest = tmp_path / ".tmp-gd1977-05-08"

    from shakedown.models import Manifest, ManifestFile
    from shakedown.plugins.base import ItemDescriptor

    desc = ItemDescriptor(
        identifier="gd1977-05-08",
        # md5 of b"audio": the plugin checks downloads against the manifest.
        manifest=Manifest(
            files=(ManifestFile("d1t01.flac", 5, "a5ca0b5894324f8bb54bb9fffad29d1e"),)
        ),
        metadata={},
    )
    archive.file(
        "gd1977-05-08", "d1t01.flac",
        Reply(200, b"audio", {"Last-Modified": "Sun, 08 May 1977 23:00:00 GMT"}),
    )

    result = plugin.fetch(desc, dest, format_filters=["flac"], exclude_filters=[])

    assert result.success
    assert result.bytes_downloaded == 5
    final = dest / "d1t01.flac"
    assert final.is_file()
    assert final.read_bytes() == b"audio"
    # No <dest>/<identifier>/ subdir from the IA layout.
    assert not (dest / desc.identifier).exists()
    # The upstream timestamp is kept, as the internetarchive library did.
    assert final.stat().st_mtime == datetime(1977, 5, 8, 23, tzinfo=UTC).timestamp()
    (call,) = archive.calls_to("/download/gd1977-05-08/d1t01.flac")
    assert "cnt=0" in call.url  # mirroring doesn't count as views


def test_fetch_quotes_file_names_in_the_url(archive, tmp_path: Path) -> None:
    plugin = _make_plugin()
    served = {"disc 1/a#1.flac": b"first"}
    archive.file("gd-x", "disc 1/a#1.flac", Reply(200, b"first"))

    result = plugin.fetch(_desc_for("gd-x", served), tmp_path / ".tmp-gd-x", [], [])

    assert result.success is True
    (call,) = archive.calls_to("/download/gd-x/disc 1/a#1.flac")
    assert "/download/gd-x/disc%201/a%231.flac" in call.url


def test_fetch_refuses_restricted_items_without_calling_download(archive, tmp_path: Path) -> None:
    plugin = _make_plugin()
    from shakedown.models import Manifest
    from shakedown.plugins.base import ItemDescriptor

    desc = ItemDescriptor(
        identifier="gd-restricted",
        manifest=Manifest(files=()),
        metadata={},
        is_restricted=True,
        restriction_reason="soundboard request",
    )

    result = plugin.fetch(desc, tmp_path / "x", format_filters=[], exclude_filters=[])

    assert result.success is False
    assert "restricted" in (result.error or "")
    assert archive.calls == []


def test_fetch_returns_failure_when_download_raises(archive, tmp_path: Path) -> None:
    """When a download raises, the plugin reports failure and leaves promotion to
    the core (which keeps prior archive bytes intact — see test_sync.py)."""
    plugin = _make_plugin()
    dest = tmp_path / ".tmp-gd-x"

    from shakedown.models import Manifest, ManifestFile
    from shakedown.plugins.base import ItemDescriptor
    desc = ItemDescriptor(
        identifier="gd-x",
        manifest=Manifest(files=(ManifestFile("real.flac", 8, "abc"),)),
        metadata={},
    )
    archive.file("gd-x", "real.flac", ConnectionResetError("simulated network drop"))

    result = plugin.fetch(desc, dest, format_filters=["flac"], exclude_filters=[])

    assert result.success is False
    assert result.retriable is True
    assert "simulated network drop" in (result.error or "")



class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _HTTPError(Exception):
    def __init__(self, message: str, response: _FakeResponse | None = None) -> None:
        super().__init__(message)
        self.response = response


def test_is_retriable_classifies_transient_faults() -> None:
    from shakedown.plugins.ia.plugin import _is_retriable

    # HTTP status-based
    assert _is_retriable(_HTTPError("boom", _FakeResponse(429))) is True
    assert _is_retriable(_HTTPError("boom", _FakeResponse(503))) is True
    # Message-based (no response attached)
    assert _is_retriable(RuntimeError("checksum mismatch on d1t01.flac")) is True
    assert _is_retriable(RuntimeError("Connection reset by peer")) is True
    # Permanent faults fail fast
    assert _is_retriable(_HTTPError("not found", _FakeResponse(404))) is False
    assert _is_retriable(RuntimeError("malformed metadata")) is False


def test_retry_after_seconds_parses_delta_header() -> None:
    from shakedown.plugins.ia.plugin import _retry_after_seconds

    assert _retry_after_seconds(_HTTPError("429", _FakeResponse(429, {"Retry-After": "12"}))) == 12.0
    # HTTP-date form is not delta-seconds → fall back to core backoff (None).
    assert _retry_after_seconds(
        _HTTPError("429", _FakeResponse(429, {"Retry-After": "Wed, 21 Oct 2025 07:28:00 GMT"}))
    ) is None
    # No response / no header → None.
    assert _retry_after_seconds(RuntimeError("plain error")) is None
    assert _retry_after_seconds(_HTTPError("boom", _FakeResponse(500))) is None


def test_verify_existence_only_does_not_hash(tmp_path: Path) -> None:
    plugin = _make_plugin()
    archive_path = tmp_path / "gd-x"
    archive_path.mkdir()
    (archive_path / "present.flac").write_bytes(b"whatever")

    from shakedown.models import Manifest, ManifestFile
    from shakedown.plugins.base import ItemDescriptor
    desc = ItemDescriptor(
        identifier="gd-x",
        manifest=Manifest(files=(
            ManifestFile("present.flac", 100, "wrong-md5-on-purpose"),
            ManifestFile("missing.flac", 50, "x"),
        )),
        metadata={},
    )

    result = plugin.verify(desc, archive_path)
    assert result.ok is False  # missing.flac is missing
    assert result.missing_files == ["missing.flac"]
    # Note: present.flac's md5 is wrong but verify() must still consider it "present".
    # The point: verify() never hashes — that's verify --deep's job.


def test_fetch_with_empty_manifest_fails_without_download(archive, tmp_path: Path) -> None:
    """Zero files upstream is never a successful (empty) fetch: promoting it would
    replace the archive copy with an empty folder."""
    plugin = _make_plugin()
    from shakedown.models import Manifest
    from shakedown.plugins.base import ItemDescriptor

    desc = ItemDescriptor(identifier="gd-dark", manifest=Manifest(files=()), metadata={})

    result = plugin.fetch(desc, tmp_path / "x", format_filters=[], exclude_filters=[])

    assert result.success is False
    assert result.retriable is False
    assert archive.calls == []


def _desc_for(identifier: str, files: dict[str, bytes], *, md5s: dict[str, str] | None = None):
    import hashlib

    from shakedown.models import Manifest, ManifestFile
    from shakedown.plugins.base import ItemDescriptor

    md5s = md5s or {}
    return ItemDescriptor(
        identifier=identifier,
        manifest=Manifest(files=tuple(
            ManifestFile(name, len(content), md5s.get(name, hashlib.md5(content).hexdigest()))
            for name, content in files.items()
        )),
        metadata={},
    )


def test_fetch_rejects_download_whose_md5_does_not_match(archive, tmp_path: Path) -> None:
    """The plugin must check what it just downloaded against the manifest before
    reporting success."""
    plugin = _make_plugin()
    dest = tmp_path / ".tmp-gd-x"
    desc = _desc_for("gd-x", {"a.flac": b"audio"})
    _serve(archive, "gd-x", {"a.flac": b"AUDIO"})  # same size, corrupted bytes

    result = plugin.fetch(desc, dest, format_filters=[], exclude_filters=[])

    assert result.success is False
    assert result.retriable is True
    assert "checksum mismatch" in (result.error or "")
    assert not (dest / "a.flac").exists(), "a corrupt file must not stay in the temp dir"


def test_fetch_rejects_download_whose_size_does_not_match(archive, tmp_path: Path) -> None:
    plugin = _make_plugin()
    dest = tmp_path / ".tmp-gd-x"
    desc = _desc_for("gd-x", {"a.flac": b"audio"})
    _serve(archive, "gd-x", {"a.flac": b"aud"})  # truncated

    result = plugin.fetch(desc, dest, format_filters=[], exclude_filters=[])

    assert result.success is False
    assert result.retriable is True
    assert not (dest / "a.flac").exists()


def test_fetch_downloads_only_files_not_already_verified(archive, tmp_path: Path) -> None:
    """Files a failed attempt left in the temp dir are kept when they match the
    manifest; only the rest are downloaded."""
    plugin = _make_plugin()
    dest = tmp_path / ".tmp-gd-x"
    dest.mkdir()
    served = {"a.flac": b"first", "b.flac": b"second", "c.flac": b"third"}
    desc = _desc_for("gd-x", served)
    (dest / "a.flac").write_bytes(b"first")  # verified leftover
    (dest / "b.flac").write_bytes(b"sec")  # partial leftover
    _serve(archive, "gd-x", served)

    result = plugin.fetch(desc, dest, format_filters=[], exclude_filters=[])

    assert result.success is True
    assert _served_calls(archive, "gd-x") == ["b.flac", "c.flac"]
    assert {n: (dest / n).read_bytes() for n in served} == served


def test_fetch_writes_nested_names_beside_reused_leftovers(archive, tmp_path: Path) -> None:
    plugin = _make_plugin()
    dest = tmp_path / ".tmp-gd-x"
    (dest / "disc1").mkdir(parents=True)
    served = {"disc1/a.flac": b"first", "disc1/b.flac": b"second"}
    desc = _desc_for("gd-x", served)
    (dest / "disc1" / "a.flac").write_bytes(b"first")
    _serve(archive, "gd-x", served)

    result = plugin.fetch(desc, dest, format_filters=[], exclude_filters=[])

    assert result.success is True
    assert {n: (dest / n).read_bytes() for n in served} == served
    assert not (dest / "gd-x").exists()


def test_fetch_skips_download_when_every_file_is_already_verified(archive, tmp_path: Path) -> None:
    plugin = _make_plugin()
    dest = tmp_path / ".tmp-gd-x"
    dest.mkdir()
    desc = _desc_for("gd-x", {"a.flac": b"first"})
    (dest / "a.flac").write_bytes(b"first")

    result = plugin.fetch(desc, dest, format_filters=[], exclude_filters=[])

    assert result.success is True
    assert archive.calls == []


def test_fetch_keeps_finished_files_when_download_raises_midway(archive, tmp_path: Path) -> None:
    """A fetch can finish some files and then fail on a later one; the finished
    files are kept so the retry fetches only the rest."""
    plugin = _make_plugin()
    dest = tmp_path / ".tmp-gd-x"
    served = {"a.flac": b"first", "b.flac": b"second"}
    desc = _desc_for("gd-x", served)
    archive.file("gd-x", "a.flac", Reply(200, served["a.flac"]))
    archive.file("gd-x", "b.flac", TimeoutError("read timed out"), Reply(200, served["b.flac"]))

    assert plugin.fetch(desc, dest, format_filters=[], exclude_filters=[]).success is False
    assert plugin.fetch(desc, dest, format_filters=[], exclude_filters=[]).success is True

    assert _served_calls(archive, "gd-x") == ["a.flac", "b.flac", "b.flac"]


def test_files_finished_before_the_process_died_are_reused(archive, tmp_path: Path, monkeypatch) -> None:
    """A run killed mid-show (container stopped) leaves finished files in the temp
    dir; the next run's fetch keeps them and downloads only the rest."""
    import shakedown.sync as sync_module
    from shakedown.config import CollectionConfig

    monkeypatch.setattr(sync_module.time, "sleep", lambda d: None)
    plugin = _make_plugin()
    tmp_dir = tmp_path / ".tmp-gd-x"
    served = {"a.flac": b"first", "b.flac": b"second"}
    desc = _desc_for("gd-x", served)
    collection = CollectionConfig(name="dead", query="q")
    archive.file("gd-x", "a.flac", Reply(200, served["a.flac"]))
    archive.file("gd-x", "b.flac", SystemExit("container stopped"), Reply(200, served["b.flac"]))

    with pytest.raises(SystemExit):
        sync_module._fetch_with_retries(plugin, desc, tmp_dir, collection)
    assert sync_module._fetch_with_retries(plugin, desc, tmp_dir, collection).success

    assert _served_calls(archive, "gd-x") == ["a.flac", "b.flac", "b.flac"]


def test_discover_marks_stream_only_items_from_their_metadata_flag(archive) -> None:
    """archive.org flags stream-only shows (e.g. gd69-12-26.sbd.murphy.1821.sbeok.shnf)
    with `access-restricted-item` and a `stream_only` collection, and answers 403 for
    their lossless files. The flag marks them unavailable before any download."""
    from shakedown.plugins.ia.plugin import DOWNLOAD_REFUSED_REASON

    plugin = _make_plugin()
    archive.search_results({"identifier": "gd69-12-26.sbd"})
    _flac_item(
        archive,
        "gd69-12-26.sbd",
        metadata={
            "date": "1969-12-26",
            "collection": ["GratefulDead", "etree", "stream_only"],
            "access-restricted-item": "true",
        },
    )

    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])
    desc = next(plugin.discover(coll))
    assert desc.is_restricted is True
    assert desc.restriction_reason == DOWNLOAD_REFUSED_REASON
