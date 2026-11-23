"""M4A tag filling, through sync, verify, and the v13 upgrade (§spec:m4a-tags,
§req:success-criteria #25, #34)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from shakedown import tagging
from shakedown.config import Config
from shakedown.realformat import RealFormat
from shakedown.state import TagRepo
from shakedown.sync import run_sync
from tests.audio_fixtures import make_audio, read_tags
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin
from tests.test_real_format import _archive, _db, _library, _names, _show, _verify

SHORELINE_13 = {"creator": "Dead & Company", "date": "2022-06-13",
                "venue": "Shoreline Amphitheatre", "title": "Live at Shoreline"}
SHORELINE_14 = {**SHORELINE_13, "date": "2022-06-14"}

# The uploader's iTunes tags on the Shoreline shows.
UPLOADER = {"artist": "Dead and Company", "album": "Shoreline 6/14/22",
            "date": "2022-06-14", "title": "Feel Like a Stranger",
            "tracknumber": "1/17", "discnumber": "1/2"}


def _filled(metadata: dict[str, str], title: str, track: str) -> dict[str, list[str]]:
    """Every tag shakedown fills in an untagged file of a show with this metadata."""
    creator = metadata["creator"]
    return {
        "artist": [creator], "albumartist": [creator],
        "album": [f"{metadata['date']} \N{EN DASH} {metadata['venue']}"],
        "date": [metadata["date"]], "venue": [metadata["venue"]],
        "title": [title], "tracknumber": [track],
    }


def _uploader_tagged_m4a(tmp_path: Path) -> bytes:
    return make_audio(tmp_path / "src" / "t.m4a", UPLOADER).read_bytes()


def _snapshot(config: Config, identifier: str) -> tuple[dict[str, bytes], dict]:
    """A show's archive bytes and its tag records."""
    folder = _archive(config, identifier)
    files = {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()}
    records = TagRepo(_db(config)).file_records("fake-src", "coll1", identifier)
    return files, records


def test_genuine_m4a_shows_are_filled_and_uploader_tags_stay(
    tmp_roots, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """§spec:m4a-tags user-level test path."""
    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", {"1-01 St. Stephen.m4a": "m4a", "1-02 Help.m4a": "m4a"})
    FakePlugin.items["deadco2022-06-13"].metadata = dict(SHORELINE_13)
    FakePlugin.items["deadco2022-06-14"] = FakeItem(
        "deadco2022-06-14",
        [FakeFile("1-01 Feel Like a Stranger.m4a", _uploader_tagged_m4a(tmp_path),
                  format="AAC", title="Feel Like a Stranger", track="1")],
        metadata=dict(SHORELINE_14),
    )
    with caplog.at_level(logging.INFO):
        assert run_sync(config) == 0

    library = _library(config, "deadco2022-06-13")
    assert read_tags(library / "1-01 St. Stephen.m4a", "m4a") == _filled(
        SHORELINE_13, "Track 1", "1")
    assert read_tags(library / "1-02 Help.m4a", "m4a") == _filled(
        SHORELINE_13, "Track 2", "2")

    tagged = _library(config, "deadco2022-06-14") / "1-01 Feel Like a Stranger.m4a"
    assert read_tags(tagged, "m4a") == {
        **{k: [v] for k, v in UPLOADER.items()},
        "albumartist": ["Dead & Company"], "venue": ["Shoreline Amphitheatre"],
    }
    assert "not filling tags" not in caplog.text


def test_tagged_m4a_shows_report_no_drift_and_a_second_sync_rewrites_nothing(
    tmp_roots,
) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", {"1-01 St. Stephen.m4a": "m4a", "1-02 Help.m4a": "alac"})
    assert run_sync(config) == 0
    before = _snapshot(config, "deadco2022-06-13")
    assert set(before[1]) == {"1-01 St. Stephen.m4a", "1-02 Help.m4a"}
    downloads = dict(FakePlugin.fetch_count)

    assert _verify(config) == []
    assert run_sync(config) == 0

    assert FakePlugin.fetch_count == downloads
    assert _snapshot(config, "deadco2022-06-13") == before
    assert _verify(config) == []


def test_m4a_named_flac_reaches_the_library_as_m4a_with_its_tags_filled(
    tmp_roots, caplog: pytest.LogCaptureFixture
) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", {"1-01 St. Stephen.flac": "m4a"})
    FakePlugin.items["deadco2022-06-13"].metadata = dict(SHORELINE_13)
    with caplog.at_level(logging.INFO):
        assert run_sync(config) == 0

    library = _library(config, "deadco2022-06-13")
    assert _names(library) == {"1-01 St. Stephen.m4a"}
    assert read_tags(library / "1-01 St. Stephen.m4a", "m4a") == _filled(
        SHORELINE_13, "Track 1", "1")
    assert "not filling tags" not in caplog.text
    assert _verify(config) == []


def test_flac_named_m4a_is_tagged_as_flac(
    tmp_roots, caplog: pytest.LogCaptureFixture
) -> None:
    """The writer follows the recorded format (§spec:m4a-tags); the library link
    takes the real extension (§spec:real-format)."""
    config = make_config(*tmp_roots, fill_tags=True)
    _show("gd77", {"t1.m4a": "flac"})
    FakePlugin.items["gd77"].metadata = dict(SHORELINE_13)
    with caplog.at_level(logging.INFO):
        assert run_sync(config) == 0

    library = _library(config, "gd77")
    assert _names(library) == {"t1.flac"}
    assert read_tags(library / "t1.flac", "flac") == _filled(SHORELINE_13, "Track 1", "1")
    assert "not filling tags" not in caplog.text


def test_an_untagged_m4a_show_mirrored_before_the_upgrade_is_filled_once(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v12 database with an already-mirrored, untagged .m4a show on a fill_tags
    collection: the first sync after the upgrade fills it without downloading; the
    second rewrites nothing (§spec:m4a-tags, §spec:state)."""
    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", {"1-01 St. Stephen.m4a": "m4a"})
    FakePlugin.items["deadco2022-06-13"].metadata = dict(SHORELINE_13)

    # Before the upgrade M4A had no writer: the show was visited, fingerprinted, and
    # left untagged.
    with monkeypatch.context() as m:
        m.setattr(tagging, "WRITABLE", tagging.WRITABLE - {RealFormat.M4A})
        assert run_sync(config) == 0
    archived = _archive(config, "deadco2022-06-13") / "1-01 St. Stephen.m4a"
    assert read_tags(archived, "m4a") == {}
    conn = _db(config)
    assert "deadco2022-06-13" in TagRepo(conn).fingerprints("fake-src", "coll1")
    conn.execute("DROP TABLE audio_formats")
    conn.execute("UPDATE schema_version SET version = 12")
    conn.close()
    downloads = dict(FakePlugin.fetch_count)

    assert run_sync(config) == 0

    assert FakePlugin.fetch_count == downloads
    library = _library(config, "deadco2022-06-13") / "1-01 St. Stephen.m4a"
    assert read_tags(library, "m4a") == _filled(SHORELINE_13, "Track 1", "1")
    filled = _snapshot(config, "deadco2022-06-13")
    assert set(filled[1]) == {"1-01 St. Stephen.m4a"}

    assert run_sync(config) == 0

    assert FakePlugin.fetch_count == downloads
    assert _snapshot(config, "deadco2022-06-13") == filled
    assert _verify(config) == []
