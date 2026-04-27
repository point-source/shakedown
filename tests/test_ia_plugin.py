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


def test_discover_skips_items_whose_metadata_fetch_raises() -> None:
    plugin = _make_plugin()
    plugin._session.search_items.return_value = iter(
        [{"identifier": "good-1"}, {"identifier": "bad-1"}, {"identifier": "good-2"}]
    )

    def get_item(identifier):
        if identifier == "bad-1":
            raise ConnectionError("upstream hiccup")
        return _fake_ia_item(
            identifier, files=[{"name": "x.flac", "size": "1", "md5": "z", "format": "Flac"}]
        )

    plugin._session.get_item.side_effect = get_item
    coll = CollectionConfig(name="dead", query="x", format_filters=["flac"])
    descriptors = list(plugin.discover(coll))
    assert {d.identifier for d in descriptors} == {"good-1", "good-2"}


def test_fetch_atomic_rename_moves_files_into_place(tmp_path: Path) -> None:
    """ia.download writes to <tmp>/<id>/<file>; plugin must promote to <dest>/<file>."""
    plugin = _make_plugin()
    dest = tmp_path / "dead" / "gd1977-05-08"

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
    # The temp directory must not be left behind on success.
    assert not (dest.parent / f".tmp-{dest.name}").exists()
    # And no stale <dest>/<identifier>/ subdir from the IA layout.
    assert not (dest / desc.identifier).exists()


def test_fetch_with_missing_files_after_download_returns_failure(tmp_path: Path) -> None:
    plugin = _make_plugin()
    dest = tmp_path / "dead" / "gd-x"

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
    assert not dest.exists(), "failed fetch must not promote temp dir to dest"


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


def test_fetch_cleans_stale_temp_dir_from_prior_failure(tmp_path: Path) -> None:
    """A prior partial fetch leaves a .tmp-<id> dir; the next fetch must clear it."""
    plugin = _make_plugin()
    dest = tmp_path / "dead" / "gd-x"
    dest.parent.mkdir(parents=True)

    stale = dest.parent / f".tmp-{dest.name}"
    stale.mkdir()
    (stale / "junk-from-last-time.flac").write_bytes(b"garbage")

    from shakedown.models import Manifest, ManifestFile
    from shakedown.plugins.base import ItemDescriptor
    desc = ItemDescriptor(
        identifier="gd-x",
        manifest=Manifest(files=(ManifestFile("real.flac", 4, "abc"),)),
        metadata={},
    )

    def fake_download(identifier, files, destdir, **kwargs):
        target = Path(destdir) / identifier
        target.mkdir(parents=True, exist_ok=True)
        (target / "real.flac").write_bytes(b"good")

    with patch("shakedown.plugins.ia.plugin.ia.download", side_effect=fake_download):
        result = plugin.fetch(desc, dest, format_filters=["flac"], exclude_filters=[])

    assert result.success
    assert (dest / "real.flac").read_bytes() == b"good"
    # The junk file must not have leaked through.
    assert not (dest / "junk-from-last-time.flac").exists()


def test_fetch_failure_preserves_existing_dest(tmp_path: Path) -> None:
    """PRD §5: archive durability. If a re-fetch fails after the prior dest existed,
    the prior bytes must remain at dest_dir. The atomic side-temp swap covers this:
    the staged-aside copy is renamed back into place."""
    plugin = _make_plugin()
    dest = tmp_path / "dead" / "gd-x"

    from shakedown.models import Manifest, ManifestFile
    from shakedown.plugins.base import ItemDescriptor
    desc = ItemDescriptor(
        identifier="gd-x",
        manifest=Manifest(files=(ManifestFile("real.flac", 8, "abc"),)),
        metadata={},
    )

    # Seed an existing archive copy at dest (pretend a prior successful fetch).
    dest.mkdir(parents=True)
    (dest / "real.flac").write_bytes(b"original")

    # Now simulate a fetch where ia.download itself raises mid-download.
    def fake_download_raises(identifier, files, destdir, **kwargs):
        # Touch the temp dir to mimic a partial write before the crash.
        target = Path(destdir) / identifier
        target.mkdir(parents=True, exist_ok=True)
        (target / "real.flac").write_bytes(b"partial-overwrite")
        raise RuntimeError("simulated network drop")

    with patch("shakedown.plugins.ia.plugin.ia.download", side_effect=fake_download_raises):
        result = plugin.fetch(desc, dest, format_filters=["flac"], exclude_filters=[])

    assert result.success is False
    # Prior bytes must survive untouched.
    assert dest.is_dir()
    assert (dest / "real.flac").read_bytes() == b"original"
    # No leftover .tmp- or .stale- siblings on success path; .tmp- may persist on
    # failure for next-run cleanup, which the next call to fetch() will sweep.
    # The .stale- variant must NOT be present (the old dest was never moved aside,
    # since the rename only happens after a successful download).
    assert not (dest.parent / f".stale-{dest.name}").exists()


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
