"""The best copy, judged by what it really is: ranking by a file's real format,
re-chosen inside the fetch that finds it (§spec:real-format-ranking,
§req:success-criteria #26 and #34)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from shakedown.config import Config, WebhookHandoff
from shakedown.db import connect
from shakedown.models import ItemStatus
from shakedown.plugins.base import FetchResult
from shakedown.state import FormatRepo, ItemRepo, TagRepo
from shakedown.sync import refetch_item, run_sync
from shakedown.tagging import desired_tags, fingerprint
from tests.audio_fixtures import flac_bytes, m4a_bytes, mp3_bytes, read_tags
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

SHOW = "deadco2022-06-14"
META = {"creator": "Dead & Company", "date": "2022-06-14",
        "venue": "Shoreline Amphitheatre", "title": "Live at Shoreline"}
TRACKS = ("1-01 Feel Like a Stranger", "1-02 Bertha")


def _aac_set(filler: int = 6000) -> list[FakeFile]:
    """The uploader's "Flac" set, really AAC in M4A and named `.flac`. Listed larger
    per track than the real FLAC set, so the listing ranks it first: equal-order
    lossless copies rank by size (§spec:quality-selection)."""
    return [FakeFile(f"{t}.flac", m4a_bytes(filler=filler), format="Flac", title=t,
                     track=str(i))
            for i, t in enumerate(TRACKS, start=1)]


def _flac_set() -> list[FakeFile]:
    return [FakeFile(f"{t}_24.flac", flac_bytes(), format="24bit Flac", title=t,
                     track=str(i))
            for i, t in enumerate(TRACKS, start=1)]


def _mp3_set() -> list[FakeFile]:
    """archive.org's VBR MP3 derivative of the uploader's set, listing its bitrate."""
    return [FakeFile(f"{t}.mp3", mp3_bytes(), format="VBR MP3", bitrate=180,
                     derived_from=f"{t}.flac", title=t, track=str(i))
            for i, t in enumerate(TRACKS, start=1)]


def _show(*sets: list[FakeFile], identifier: str = SHOW) -> FakeItem:
    item = FakeItem(identifier, [f for files in sets for f in files], metadata=dict(META))
    FakePlugin.items[identifier] = item
    return item


def _names(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir() if p.is_file()} if folder.is_dir() else set()


def _archive(config: Config, identifier: str = SHOW) -> Path:
    return config.archive_root / "fake-src" / "coll1" / identifier


def _library(config: Config, identifier: str = SHOW) -> Path:
    return config.library_root / "fake-src" / "coll1" / identifier


def _db(config: Config) -> sqlite3.Connection:
    assert config.state_db is not None
    return connect(config.state_db)


class _Webhooks:
    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, url: str, json: dict[str, Any] | None = None, timeout: Any = None):
        assert json is not None
        if json.get("event") == "sync.complete":
            self.bodies.append(json)
        return httpx.Response(200)


def _sync(config: Config, hooks: _Webhooks | None = None) -> int:
    with patch("shakedown.notify.httpx.post", side_effect=hooks or _Webhooks()):
        return run_sync(config)


def _config(tmp_roots, **kwargs: Any) -> Config:
    return make_config(*tmp_roots, on_complete=WebhookHandoff(webhook="http://hook.example/"),
                       **kwargs)


AAC_NAMES = {f"{t}.flac" for t in TRACKS}
FLAC_NAMES = {f"{t}_24.flac" for t in TRACKS}
MP3_NAMES = {f"{t}.mp3" for t in TRACKS}


# --- a check that changes the ranking re-runs the choice in the same run ---------------


def test_a_flac_listed_aac_set_is_replaced_by_the_real_flac_set_in_the_same_run(
    tmp_roots,
) -> None:
    config = _config(tmp_roots)
    _show(_aac_set(), _flac_set(), _mp3_set())
    hooks = _Webhooks()
    assert _sync(config, hooks) == 0

    # The AAC set first (the listing's choice), then the FLAC set; never the MP3.
    assert FakePlugin.fetched_files[SHOW] == sorted(AAC_NAMES) + sorted(FLAC_NAMES)
    assert _names(_archive(config)) == AAC_NAMES | FLAC_NAMES
    assert _names(_library(config)) == FLAC_NAMES
    # One handoff naming the show once.
    assert len(hooks.bodies) == 1
    assert [e["identifier"] for e in hooks.bodies[0]["staged"]] == [SHOW]
    # The demoted AAC set is recorded archive-only, with its checks.
    item = ItemRepo(_db(config)).get("fake-src", "coll1", SHOW)
    assert item is not None and item.recorded_manifest is not None
    assert {f.name for f in item.recorded_manifest.files if f.library} == FLAC_NAMES
    records = FormatRepo(_db(config)).for_show("fake-src", "coll1", SHOW)
    assert set(records) == AAC_NAMES | FLAC_NAMES

    fetches = FakePlugin.fetch_count[SHOW]
    assert _sync(config) == 0
    assert FakePlugin.fetch_count[SHOW] == fetches  # sync again: no download
    assert _names(_library(config)) == FLAC_NAMES


def test_an_aac_set_without_a_better_copy_is_kept_and_staged_as_m4a(tmp_roots) -> None:
    """Shaped like the Dead & Company shows: no listed bitrate on the originals, about
    twice the size per track of the VBR MP3 derivative."""
    config = _config(tmp_roots)
    _show(_aac_set(filler=16000), _mp3_set())
    assert _sync(config) == 0
    assert FakePlugin.fetched_files[SHOW] == sorted(AAC_NAMES)
    assert _names(_library(config)) == {f"{t}.m4a" for t in TRACKS}
    item = ItemRepo(_db(config)).get("fake-src", "coll1", SHOW)
    assert item is not None and item.restriction_reason is None


def test_an_aac_set_beside_an_shn_set_keeps_the_shn_archive_only(tmp_roots) -> None:
    config = _config(tmp_roots)
    shn = [FakeFile(f"{t}.shn", b"ajkw" + bytes(9000), format="Shorten") for t in TRACKS]
    _show(_aac_set(), shn)
    assert _sync(config) == 0
    assert FakePlugin.fetch_count[SHOW] == 2
    assert _names(_archive(config)) == AAC_NAMES | {f"{t}.shn" for t in TRACKS}
    assert _names(_library(config)) == {f"{t}.m4a" for t in TRACKS}
    item = ItemRepo(_db(config)).get("fake-src", "coll1", SHOW)
    assert item is not None and item.not_playable_reason is None


def test_the_newly_chosen_files_enter_the_archive_tagged(
    tmp_roots, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shakedown.sync as sync_module

    # Only fetch-time tagging runs: the run's pass over mirrored shows is off.
    monkeypatch.setattr(sync_module, "_fill_tags", lambda *_a, **_k: {})
    config = _config(tmp_roots, fill_tags=True)
    _show(_aac_set(), _flac_set())
    assert _sync(config) == 0
    for name in FLAC_NAMES:
        tags = read_tags(_archive(config) / name)
        assert tags["albumartist"] == ["Dead & Company"]
        assert tags["venue"] == ["Shoreline Amphitheatre"]
    # The recorded fingerprint is the one the tag pass computes for the final copy,
    # so the pass does not revisit the show.
    item = ItemRepo(_db(config)).get("fake-src", "coll1", SHOW)
    assert item is not None and item.recorded_manifest is not None
    expected = desired_tags(SHOW, item.source_metadata, item.recorded_manifest,
                            labelled=False)
    assert TagRepo(_db(config)).fingerprints("fake-src", "coll1")[SHOW] == fingerprint(expected)
    assert set(TagRepo(_db(config)).file_records("fake-src", "coll1", SHOW)) >= FLAC_NAMES


def test_a_failed_second_round_leaves_no_archive_copy_and_the_next_run_finishes(
    tmp_roots, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shakedown.sync as sync_module

    monkeypatch.setattr(sync_module, "_sleep", lambda _s: None)
    config = _config(tmp_roots)
    _show(_aac_set(), _flac_set())
    real_fetch = FakePlugin.fetch

    def fetch(self, identifier, files, dest_dir):
        if any(mf.name.endswith("_24.flac") for mf in files):
            return FetchResult(success=False, error="connection reset", retriable=False)
        return real_fetch(self, identifier, files, dest_dir)

    monkeypatch.setattr(FakePlugin, "fetch", fetch)
    _sync(config)
    assert not _archive(config).exists()
    item = ItemRepo(_db(config)).get("fake-src", "coll1", SHOW)
    assert item is not None and item.status == ItemStatus.FAILED

    monkeypatch.setattr(FakePlugin, "fetch", real_fetch)
    assert _sync(config) == 0
    assert _names(_library(config)) == FLAC_NAMES


# --- the recorded checks feed every later lookup ---------------------------------------


def test_a_real_flac_set_added_later_replaces_the_kept_aac_set(tmp_roots) -> None:
    config = _config(tmp_roots)
    item = _show(_aac_set(filler=16000), _mp3_set())
    assert _sync(config) == 0
    assert _names(_library(config)) == {f"{t}.m4a" for t in TRACKS}

    item.files.extend(_flac_set())
    FakePlugin.fetched_files.clear()
    assert _sync(config) == 0
    assert FakePlugin.fetched_files[SHOW] == sorted(FLAC_NAMES)  # only the new set
    assert _names(_archive(config)) == AAC_NAMES | FLAC_NAMES
    assert _names(_library(config)) == FLAC_NAMES


def test_a_refetch_chooses_by_the_recorded_real_format(tmp_roots) -> None:
    config = _config(tmp_roots)
    item = _show(_aac_set(filler=16000), _mp3_set())
    assert _sync(config) == 0
    item.files.extend(_flac_set())
    FakePlugin.fetched_files.clear()
    with patch("shakedown.notify.httpx.post", side_effect=_Webhooks()):
        assert refetch_item(config, SHOW) == 0
    assert set(FakePlugin.fetched_files[SHOW]) == FLAC_NAMES
    assert _names(_library(config)) == FLAC_NAMES
    assert _names(_archive(config)) == AAC_NAMES | FLAC_NAMES


# --- refused lossless keeps its meaning --------------------------------------------------


def test_a_served_flac_listed_aac_show_never_reports_lossless_refused(tmp_roots) -> None:
    config = _config(tmp_roots)
    _show(_aac_set(filler=16000), _mp3_set())
    assert _sync(config) == 0
    item = ItemRepo(_db(config)).get("fake-src", "coll1", SHOW)
    assert item is not None and item.restriction_reason is None


def test_a_refused_lossless_show_still_reports_the_reason(tmp_roots) -> None:
    from shakedown.selection import LOSSLESS_REFUSED_REASON

    config = _config(tmp_roots)
    refused = [FakeFile(f"{t}_24.flac", flac_bytes(), format="24bit Flac", refused=True)
               for t in TRACKS]
    _show(refused, _mp3_set())
    assert _sync(config) == 0
    item = ItemRepo(_db(config)).get("fake-src", "coll1", SHOW)
    assert item is not None and item.restriction_reason == LOSSLESS_REFUSED_REASON


# --- stored shows re-rank on the same rule -----------------------------------------------


def _mirror_at_v12(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync as shakedown did before it checked content, then put the database back at
    v12, with no check recorded (tests.test_stored_shows_checked)."""
    from contextlib import closing

    with monkeypatch.context() as m:
        m.setattr("shakedown.sync.is_checked", lambda name: False)
        assert _sync(config) == 0
    with closing(_db(config)) as conn:
        conn.execute("DROP TABLE audio_formats")
        conn.execute("UPDATE schema_version SET version = 12")


def test_a_stored_aac_set_is_replaced_by_the_real_flac_set_on_an_unchanged_signal(
    tmp_roots, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§spec:real-format-ranking: the catch-up's checks change the ranking, so the show
    is looked up even though its change signal says unchanged."""
    config = _config(tmp_roots, incremental_discovery=True)
    item = _show(_aac_set(), _flac_set(), _mp3_set())
    item.change_signal = "2022-06-20T00:00:00"
    _mirror_at_v12(config, monkeypatch)
    assert _names(_library(config)) == AAC_NAMES
    FakePlugin.fetched_files.clear()
    lookups = FakePlugin.lookups(SHOW)

    hooks = _Webhooks()
    assert _sync(config, hooks) == 0
    assert FakePlugin.lookups(SHOW) == lookups + 1
    assert FakePlugin.fetched_files[SHOW] == sorted(FLAC_NAMES)  # only the FLAC set
    assert _names(_archive(config)) == AAC_NAMES | FLAC_NAMES
    assert _names(_library(config)) == FLAC_NAMES
    assert [e["identifier"] for b in hooks.bodies for e in b["staged"]] == [SHOW]

    # Later syncs skip the lookup again: the signal is recorded with the new copy.
    assert _sync(config) == 0
    assert FakePlugin.lookups(SHOW) == lookups + 1


def test_a_stored_dead_and_company_show_is_looked_up_once_and_downloads_nothing(
    tmp_roots, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_roots, incremental_discovery=True)
    item = _show(_aac_set(filler=16000), _mp3_set())
    item.change_signal = "2022-06-20T00:00:00"
    _mirror_at_v12(config, monkeypatch)
    fetches = FakePlugin.fetch_count[SHOW]
    lookups = FakePlugin.lookups(SHOW)

    assert _sync(config) == 0
    assert FakePlugin.fetch_count[SHOW] == fetches
    assert FakePlugin.lookups(SHOW) == lookups + 1
    assert _names(_library(config)) == {f"{t}.m4a" for t in TRACKS}

    assert _sync(config) == 0
    assert FakePlugin.lookups(SHOW) == lookups + 1
    assert FakePlugin.fetch_count[SHOW] == fetches


def test_a_stored_honest_flac_show_is_not_looked_up(
    tmp_roots, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_roots, incremental_discovery=True)
    item = _show(_flac_set(), _mp3_set())
    item.change_signal = "2022-06-20T00:00:00"
    _mirror_at_v12(config, monkeypatch)
    lookups = FakePlugin.lookups(SHOW)
    assert _sync(config) == 0
    assert FakePlugin.lookups(SHOW) == lookups


def test_a_run_killed_after_the_check_still_looks_the_show_up_next_time(
    tmp_roots, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shakedown.sync as sync_module

    config = _config(tmp_roots, incremental_discovery=True)
    item = _show(_aac_set(), _flac_set())
    item.change_signal = "2022-06-20T00:00:00"
    _mirror_at_v12(config, monkeypatch)
    FakePlugin.fetched_files.clear()

    def die(*_a, **_k):
        raise KeyboardInterrupt  # the process dies after the checks, before lookups

    with monkeypatch.context() as m:
        m.setattr(sync_module, "_rank_records", die)
        with pytest.raises(KeyboardInterrupt):
            _sync(config)
    assert FakePlugin.fetched_files == {}

    assert _sync(config) == 0
    assert FakePlugin.fetched_files[SHOW] == sorted(FLAC_NAMES)
    assert _names(_library(config)) == FLAC_NAMES
