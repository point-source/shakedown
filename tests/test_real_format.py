"""Audio known by its content, through sync, restage, and verify (§spec:real-format,
§req:success-criteria #34)."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from shakedown import realformat
from shakedown.config import Config
from shakedown.db import connect
from shakedown.realformat import AudioClass, FormatCheck, RealFormat
from shakedown.state import FormatRepo
from shakedown.sync import run_sync
from tests.audio_fixtures import CONTENT
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

AAC = FormatCheck(RealFormat.M4A, AudioClass.LOSSY)
MP3 = FormatCheck(RealFormat.MP3, AudioClass.LOSSY)
FLAC = FormatCheck(RealFormat.FLAC, AudioClass.LOSSLESS)
UNIDENTIFIED = FormatCheck(RealFormat.UNIDENTIFIED, None)

META = {"creator": "Dead & Company", "date": "2022-06-13",
        "venue": "Shoreline Amphitheatre", "title": "Live at Shoreline"}
DEADCO = {"1-01 St. Stephen.flac": "m4a", "1-02 Help on the Way.flac": "m4a",
          "1-03 Slipknot!.flac": "m4a"}


def _show(identifier: str, files: dict[str, str], *, listed_as: str = "Flac") -> FakeItem:
    """A show whose files hold `files[name]` audio (an audio_fixtures CONTENT key)
    whatever their names, listed by the source under `listed_as`, as archive.org
    repeats an uploader's mistake."""
    item = FakeItem(
        identifier,
        [FakeFile(name, CONTENT[content](), format=listed_as, title=f"Track {i}",
                  track=str(i))
         for i, (name, content) in enumerate(files.items(), start=1)],
        metadata=dict(META),
    )
    FakePlugin.items[identifier] = item
    return item


def _archive(config: Config, identifier: str) -> Path:
    return config.archive_root / "fake-src" / "coll1" / identifier


def _library(config: Config, identifier: str) -> Path:
    return config.library_root / "fake-src" / "coll1" / identifier


def _names(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir() if p.is_file()}


def _db(config: Config) -> sqlite3.Connection:
    assert config.state_db is not None
    return connect(config.state_db)


def _formats(config: Config, identifier: str) -> dict[str, FormatCheck]:
    records = FormatRepo(_db(config)).for_show("fake-src", "coll1", identifier)
    return {name: record[2] for name, record in records.items()}


# --- checked once, when the file arrives -----------------------------------------------


def test_a_fetched_show_records_each_files_real_format(tmp_roots) -> None:
    config = make_config(*tmp_roots)
    item = _show("deadco2022-06-13", {**DEADCO, "info.txt": "wav", "notes.shn": "wav"})
    assert run_sync(config) == 0

    assert _formats(config, item.identifier) == dict.fromkeys(DEADCO, AAC)
    records = FormatRepo(_db(config)).for_show("fake-src", "coll1", item.identifier)
    by_name = {f.name: f for f in item.files}
    assert {n: r[:2] for n, r in records.items()} == {
        n: (by_name[n].size, by_name[n].md5) for n in DEADCO
    }


def test_the_run_log_has_one_line_per_mislabelled_file(
    tmp_roots, caplog: pytest.LogCaptureFixture
) -> None:
    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", {**DEADCO, "1-04 honest.flac": "flac", "wav.flac": "wav"})
    with caplog.at_level(logging.INFO):
        assert run_sync(config) == 0

    lines = [r.getMessage() for r in caplog.records
             if "audio" in r.getMessage() and "deadco2022-06-13:" in r.getMessage()]
    assert sorted(lines) == sorted(
        [f"[fake-src/coll1] deadco2022-06-13: {name} holds M4A (AAC) audio, not FLAC"
         for name in DEADCO]
        + ["[fake-src/coll1] deadco2022-06-13: wav.flac is not audio shakedown "
           "recognises; it keeps its name"]
    )


def test_a_file_that_cannot_be_read_for_its_check_fails_the_fetch_for_a_retry(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An I/O fault is not a verdict: nothing is recorded, and the next run fetches
    and checks the show again."""
    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", DEADCO)
    real = realformat.check_file

    def unreadable(path: Path) -> FormatCheck:
        raise OSError("I/O error")

    monkeypatch.setattr(realformat, "check_file", unreadable)
    assert run_sync(config) != 0
    assert not _archive(config, "deadco2022-06-13").exists()
    assert _formats(config, "deadco2022-06-13") == {}

    monkeypatch.setattr(realformat, "check_file", real)
    assert run_sync(config) == 0
    assert _formats(config, "deadco2022-06-13") == dict.fromkeys(DEADCO, AAC)


def test_a_file_the_source_changes_is_checked_again(tmp_roots) -> None:
    config = make_config(*tmp_roots)
    item = _show("deadco2022-06-13", {"t1.flac": "m4a", "t2.flac": "flac"})
    assert run_sync(config) == 0
    assert _formats(config, item.identifier) == {"t1.flac": AAC, "t2.flac": FLAC}

    item.files[0] = FakeFile("t1.flac", CONTENT["flac"](), format="Flac")
    assert run_sync(config) == 0
    assert _formats(config, item.identifier) == {"t1.flac": FLAC, "t2.flac": FLAC}


# --- the library carries the real extension -------------------------------------------


def test_m4a_named_flac_is_archived_by_its_name_and_staged_as_m4a(
    tmp_roots, caplog: pytest.LogCaptureFixture
) -> None:
    """§spec:real-format user-level test path, on a tag-filling collection."""
    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", DEADCO)
    with caplog.at_level(logging.INFO):
        assert run_sync(config) == 0

    assert _names(_archive(config, "deadco2022-06-13")) == set(DEADCO)
    stems = {name.removesuffix(".flac") for name in DEADCO}
    assert _names(_library(config, "deadco2022-06-13")) == {f"{s}.m4a" for s in stems}
    for name in DEADCO:
        library = _library(config, "deadco2022-06-13") / name.replace(".flac", ".m4a")
        archive = _archive(config, "deadco2022-06-13") / name
        assert library.stat().st_ino == archive.stat().st_ino
    assert "cannot be read as" not in caplog.text


def test_restage_rebuilds_the_real_names_without_opening_audio(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    import mutagen

    from shakedown import tagging
    from shakedown.restage import run_restage

    config = make_config(*tmp_roots)
    _show("deadco2022-06-13", DEADCO)
    assert run_sync(config) == 0
    before = _names(_library(config, "deadco2022-06-13"))
    shutil.rmtree(config.library_root / "fake-src")

    def no_audio(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("restage opened an audio file")

    monkeypatch.setattr(realformat, "check_file", no_audio)
    for module in (realformat, tagging):
        for reader in ("FLAC", "MP3", "MP4", "OggVorbis"):
            if hasattr(module, reader):
                monkeypatch.setattr(module, reader, no_audio)
    monkeypatch.setattr(mutagen, "File", no_audio)

    assert run_restage(config, source_filter=None, collection_filter=None) == 0
    assert _names(_library(config, "deadco2022-06-13")) == before


def test_each_mislabel_takes_its_real_extension_and_honest_names_stay(tmp_roots) -> None:
    config = make_config(*tmp_roots)
    _show("mixed", {"t1.ogg": "mp3", "t2.mp3": "flac", "Track 3.FLAC": "flac"},
          listed_as="Flac")
    assert run_sync(config) == 0
    assert _names(_library(config, "mixed")) == {"t1.mp3", "t2.flac", "Track 3.FLAC"}


def test_a_wav_named_flac_is_kept_under_its_name_and_never_tagged(
    tmp_roots, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.audio_fixtures import wav_bytes

    config = make_config(*tmp_roots, fill_tags=True)
    _show("wav", {"t1.flac": "wav"})
    assert run_sync(config) == 0

    assert _names(_archive(config, "wav")) == {"t1.flac"}
    assert _names(_library(config, "wav")) == {"t1.flac"}
    assert (_archive(config, "wav") / "t1.flac").read_bytes() == wav_bytes()
    assert "not filling tags" not in caplog.text
    assert _formats(config, "wav") == {"t1.flac": UNIDENTIFIED}


# --- re-runs stay quiet; reconform keeps the real name ----------------------------------


def _verify(config: Config, *, reconform: bool = False) -> list[tuple[str, str]]:
    from shakedown.state import DriftRepo
    from shakedown.verify import run_verify

    assert run_verify(config, source_filter=None, collection_filter=None, deep=True,
                      reconform=reconform, list_drift=True, assume_yes=True) == 0
    return sorted((row["identifier"], row["file_name"])
                  for row in DriftRepo(_db(config)).list("fake-src", "coll1"))


def test_sync_again_downloads_nothing_and_verify_finds_no_drift(tmp_roots) -> None:
    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", DEADCO)
    _show("deadco2022-06-18", {"t1.flac": "mp3"})
    assert run_sync(config) == 0
    library = sorted(p.relative_to(config.library_root) for p in config.library_root.rglob("*"))
    downloads = dict(FakePlugin.fetch_count)

    assert run_sync(config) == 0

    assert FakePlugin.fetch_count == downloads
    assert sorted(
        p.relative_to(config.library_root) for p in config.library_root.rglob("*")
    ) == library
    assert _verify(config) == []


def test_reconform_keeps_the_real_name_and_refills_with_the_real_writer(tmp_roots) -> None:
    from tests.audio_fixtures import read_tags

    config = make_config(*tmp_roots, fill_tags=True)
    _show("deadco2022-06-13", DEADCO)
    _show("deadco2022-06-18", {"t1.flac": "mp3"})
    assert run_sync(config) == 0
    mp3 = _archive(config, "deadco2022-06-18") / "t1.flac"
    assert read_tags(mp3, "mp3")["venue"] == ["Shoreline Amphitheatre"]
    aac = _archive(config, "deadco2022-06-13") / "1-01 St. Stephen.flac"
    for path in (mp3, aac):
        with path.open("r+b") as f:  # another tool changes the bytes in place
            f.seek(-1, 2)
            f.write(b"\x01")
    drifted = [("deadco2022-06-13", "1-01 St. Stephen.flac"), ("deadco2022-06-18", "t1.flac")]
    assert _verify(config) == drifted

    assert _verify(config, reconform=True) == drifted  # what it found, then repaired
    assert _verify(config) == []

    assert _names(_library(config, "deadco2022-06-13")) == {
        name.replace(".flac", ".m4a") for name in DEADCO
    }
    assert _names(_library(config, "deadco2022-06-18")) == {"t1.mp3"}
    assert read_tags(mp3, "mp3")["venue"] == ["Shoreline Amphitheatre"]
    assert (_library(config, "deadco2022-06-18") / "t1.mp3").stat().st_ino == mp3.stat().st_ino


# --- the observed shape, through the archive.org plugin ---------------------------------


def test_an_archive_org_show_listed_as_flac_holding_m4a_is_staged_as_m4a(
    tmp_roots, monkeypatch: pytest.MonkeyPatch
) -> None:
    """archive.org repeats the uploader's mistake (format "Flac"); the content decides."""
    import hashlib

    import shakedown.sync as sync_module
    from shakedown.config import CollectionConfig, SourceConfig
    from tests.audio_fixtures import m4a_bytes
    from tests.fake_archive import Reply, fake_archive

    monkeypatch.setattr("time.sleep", lambda _s: None)
    monkeypatch.setattr(sync_module, "_sleep", lambda _s: None)
    body = m4a_bytes()
    config = Config(
        archive_root=tmp_roots[0], library_root=tmp_roots[1],
        sources=[SourceConfig(name="ia", type="ia", collections=[
            CollectionConfig(name="deadco", query="collection:DeadAndCompany"),
        ])],
    )
    with fake_archive() as archive:
        archive.search_results({"identifier": "deadco2022-06-14"})
        archive.item("deadco2022-06-14", [
            {"name": "1-01 Feel Like a Stranger.flac", "size": str(len(body)),
             "md5": hashlib.md5(body).hexdigest(), "format": "Flac", "source": "original"},
        ], metadata={"creator": "Dead & Company", "date": "2022-06-14"})
        archive.file("deadco2022-06-14", "1-01 Feel Like a Stranger.flac", Reply(200, body))
        assert run_sync(config) == 0

    archive_dir = config.archive_root / "ia" / "deadco" / "deadco2022-06-14"
    library_dir = config.library_root / "ia" / "deadco" / "deadco2022-06-14"
    assert _names(archive_dir) == {"1-01 Feel Like a Stranger.flac"}
    assert _names(library_dir) == {"1-01 Feel Like a Stranger.m4a"}
