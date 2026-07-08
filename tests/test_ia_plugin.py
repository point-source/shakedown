"""IA plugin discover/fetch/verify with the internetarchive package mocked.

The pure helpers (_build_manifest, _detect_restriction, _file_matches_filters)
are spot-checked from a different angle: end-to-end via the plugin facade.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.plugins.ia.plugin import IAPlugin


def _make_plugin() -> IAPlugin:
    src = SourceConfig(
        name="ia",
        type="ia",
        collections=[CollectionConfig(name="dead", query="collection:GratefulDead")],
    )
    with patch("shakedown.plugins.ia.plugin.ia.get_session", return_value=MagicMock()):
        return IAPlugin(src)


def _fake_ia_item(identifier: str, files: list[dict[str, Any]], metadata: dict | None = None) -> MagicMock:
    item = MagicMock()
    item.files = files
    item.metadata = metadata or {"title": "Show", "date": "1977-05-08"}
    return item


def test_discover_yields_descriptors_with_manifests() -> None:
    plugin = _make_plugin()
    plugin._session.search_items.return_value = iter([{"identifier": "gd1977-05-08"}])
    plugin._session.get_item.return_value = _fake_ia_item(
        "gd1977-05-08",
        files=[
            {"name": "d1t01.flac", "size": "1000", "md5": "aaa", "format": "Flac"},
            {"name": "d1t01_64kb.mp3", "size": "100", "md5": "bbb", "format": "VBR MP3"},
        ],
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


def test_discover_marks_restricted_items() -> None:
    plugin = _make_plugin()
    plugin._session.search_items.return_value = iter([{"identifier": "gd-stream-only"}])
    plugin._session.get_item.return_value = _fake_ia_item(
        "gd-stream-only",
        files=[{"name": "x.flac", "size": "1", "md5": "x", "format": "Flac"}],
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


def test_discover_skips_items_whose_metadata_fetch_raises(monkeypatch) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: None)

    plugin = _make_plugin()
    plugin._session.search_items.return_value = iter(
        [{"identifier": "good-1"}, {"identifier": "bad-1"}, {"identifier": "good-2"}]
    )

    def get_item(identifier, **kwargs):
        if identifier == "bad-1":
            # Retriable transient fault → retried up to the cap, then skipped.
            raise ConnectionError("connection reset by peer")
        return _fake_ia_item(
            identifier, files=[{"name": "x.flac", "size": "1", "md5": "z", "format": "Flac"}]
        )

    plugin._session.get_item.side_effect = get_item
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])
    descriptors = list(plugin.discover(coll))
    assert {d.identifier for d in descriptors} == {"good-1", "good-2"}


def test_describe_item_retries_transient_fault_then_resolves(monkeypatch) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    delays: list[float] = []
    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: delays.append(d))

    plugin = _make_plugin()
    good_item = _fake_ia_item(
        "gd-transient", files=[{"name": "x.flac", "size": "1", "md5": "z", "format": "Flac"}]
    )
    calls = {"n": 0}

    def get_item(identifier, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _HTTPError("service unavailable", _FakeResponse(503))
        return good_item

    plugin._session.get_item.side_effect = get_item
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    desc = plugin.describe_item("gd-transient", coll)
    assert desc is not None
    assert desc.identifier == "gd-transient"
    assert calls["n"] == 3  # two transient failures, third succeeds
    # Two backoff sleeps recorded (attempts 1 and 2 failed transiently).
    assert delays == [ia_plugin._metadata_backoff_seconds(1), ia_plugin._metadata_backoff_seconds(2)]


def test_describe_item_gives_up_after_max_attempts(monkeypatch) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: None)

    plugin = _make_plugin()
    calls = {"n": 0}

    def get_item(identifier, **kwargs):
        calls["n"] += 1
        raise _HTTPError("still down", _FakeResponse(503))

    plugin._session.get_item.side_effect = get_item
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    assert plugin.describe_item("gd-down", coll) is None
    assert calls["n"] == ia_plugin._MAX_METADATA_ATTEMPTS


def test_describe_item_does_not_retry_non_retriable_fault(monkeypatch) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: None)

    plugin = _make_plugin()
    calls = {"n": 0}

    def get_item(identifier, **kwargs):
        calls["n"] += 1
        raise RuntimeError("malformed metadata")

    plugin._session.get_item.side_effect = get_item
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    assert plugin.describe_item("gd-bad", coll) is None
    assert calls["n"] == 1  # non-retriable → exactly one attempt


def test_describe_item_honors_retry_after_header(monkeypatch) -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    delays: list[float] = []
    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: delays.append(d))

    plugin = _make_plugin()
    good_item = _fake_ia_item(
        "gd-429", files=[{"name": "x.flac", "size": "1", "md5": "z", "format": "Flac"}]
    )
    calls = {"n": 0}

    def get_item(identifier, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _HTTPError("rate limited", _FakeResponse(429, {"Retry-After": "7"}))
        return good_item

    plugin._session.get_item.side_effect = get_item
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    desc = plugin.describe_item("gd-429", coll)
    assert desc is not None
    assert delays == [7.0]  # honored the Retry-After delta, not the backoff schedule


def test_describe_item_clamps_hostile_retry_after(monkeypatch) -> None:
    """A describe worker holds a shared SourceBudget slot while it sleeps, so an
    upstream-controlled Retry-After must be clamped to the backoff cap — a hostile
    endpoint can't pin the slot for an attacker-chosen wait."""
    from shakedown.plugins.ia import plugin as ia_plugin

    delays: list[float] = []
    monkeypatch.setattr(ia_plugin.time, "sleep", lambda d: delays.append(d))

    plugin = _make_plugin()
    good_item = _fake_ia_item(
        "gd-evil", files=[{"name": "x.flac", "size": "1", "md5": "z", "format": "Flac"}]
    )
    calls = {"n": 0}

    def get_item(identifier, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _HTTPError("rate limited", _FakeResponse(429, {"Retry-After": "31536000"}))
        return good_item

    plugin._session.get_item.side_effect = get_item
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    desc = plugin.describe_item("gd-evil", coll)
    assert desc is not None
    assert delays == [ia_plugin._METADATA_MAX_BACKOFF_SECONDS]  # clamped, not a year


def test_describe_item_passes_bounded_timeout_in_request_kwargs() -> None:
    from shakedown.plugins.ia import plugin as ia_plugin

    plugin = _make_plugin()
    plugin._session.get_item.return_value = _fake_ia_item(
        "gd-timeout", files=[{"name": "x.flac", "size": "1", "md5": "z", "format": "Flac"}]
    )
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])

    assert plugin.describe_item("gd-timeout", coll) is not None
    _, kwargs = plugin._session.get_item.call_args
    assert kwargs["request_kwargs"] == {"timeout": ia_plugin._METADATA_TIMEOUT}


def test_fetch_flattens_ia_subdir_into_dest(tmp_path: Path) -> None:
    """ia.download writes to <dest>/<id>/<file>; the plugin flattens to <dest>/<file>.

    The plugin writes into the core-owned temp dir it is handed; the core owns the
    atomic promotion into the archive (see test_sync.py)."""
    plugin = _make_plugin()
    dest = tmp_path / ".tmp-gd1977-05-08"

    from shakedown.models import Manifest, ManifestFile
    from shakedown.plugins.base import ItemDescriptor

    desc = ItemDescriptor(
        identifier="gd1977-05-08",
        manifest=Manifest(files=(ManifestFile("d1t01.flac", 5, "abc"),)),
        metadata={},
    )

    def fake_download(identifier, files, destdir, **kwargs):
        # Mirror what real ia.download does: place files under <destdir>/<identifier>/.
        target = Path(destdir) / identifier
        target.mkdir(parents=True, exist_ok=True)
        (target / "d1t01.flac").write_bytes(b"audio")

    with patch("shakedown.plugins.ia.plugin.ia.download", side_effect=fake_download):
        result = plugin.fetch(desc, dest, format_filters=["flac"], exclude_filters=[])

    assert result.success
    assert result.bytes_downloaded == 5
    final = dest / "d1t01.flac"
    assert final.is_file()
    assert final.read_bytes() == b"audio"
    # No stale <dest>/<identifier>/ subdir from the IA layout should remain.
    assert not (dest / desc.identifier).exists()


def test_fetch_with_missing_files_after_download_returns_failure(tmp_path: Path) -> None:
    plugin = _make_plugin()
    dest = tmp_path / ".tmp-gd-x"

    from shakedown.models import Manifest, ManifestFile
    from shakedown.plugins.base import ItemDescriptor

    desc = ItemDescriptor(
        identifier="gd-x",
        manifest=Manifest(files=(ManifestFile("expected.flac", 5, "abc"),)),
        metadata={},
    )

    def fake_download(identifier, files, destdir, **kwargs):
        # Don't actually write the expected file; simulate IA dropping the request.
        target = Path(destdir) / identifier
        target.mkdir(parents=True, exist_ok=True)

    with patch("shakedown.plugins.ia.plugin.ia.download", side_effect=fake_download):
        result = plugin.fetch(desc, dest, format_filters=["flac"], exclude_filters=[])

    assert result.success is False
    assert "missing after fetch" in (result.error or "")


def test_fetch_refuses_restricted_items_without_calling_download(tmp_path: Path) -> None:
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

    with patch("shakedown.plugins.ia.plugin.ia.download") as mock_download:
        result = plugin.fetch(desc, tmp_path / "x", format_filters=[], exclude_filters=[])

    assert result.success is False
    assert "restricted" in (result.error or "")
    mock_download.assert_not_called()


def test_fetch_returns_failure_when_download_raises(tmp_path: Path) -> None:
    """When ia.download raises, the plugin reports failure and leaves promotion to
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

    def fake_download_raises(identifier, files, destdir, **kwargs):
        raise RuntimeError("simulated network drop")

    with patch("shakedown.plugins.ia.plugin.ia.download", side_effect=fake_download_raises):
        result = plugin.fetch(desc, dest, format_filters=["flac"], exclude_filters=[])

    assert result.success is False
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
