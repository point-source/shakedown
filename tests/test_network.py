"""Network activity (§spec:network-activity): the per-source, per-minute totals and the
network events a run records, and how they are kept."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import requests
import urllib3.exceptions

from shakedown.activity import ActivityKind, ActivityRecorder, Progress
from shakedown.config import CollectionConfig, SourceConfig
from shakedown.db import connect
from shakedown.discovery import LookupFailure, SourceBudget, lookup
from shakedown.network import EventKind, MinuteTotals, NetworkEvent, NetworkRepo
from shakedown.plugins.base import (
    FailureCause,
    ListingRetry,
    failure_cause,
    listing_retries_to,
)
from tests.fake_plugin import FakePlugin


def _repo(tmp_path: Path) -> NetworkRepo:
    return NetworkRepo(connect(tmp_path / "state.db"))


def test_minutes_add_up_across_writes_and_keep_the_highest_peak(tmp_path: Path) -> None:
    """Each write carries what happened since the last one; a minute written twice
    (two ticks, two writers) adds up, and its peak is the higher of the two."""
    repo = _repo(tmp_path)
    minute = datetime(2026, 11, 12, 9, 0)
    repo.add_minutes("ia", {minute: MinuteTotals(bytes=100, peak_requests_in_flight=3,
                                                 rate_limited=1, backoff_seconds=1.5)})
    repo.add_minutes("ia", {minute: MinuteTotals(bytes=50, peak_requests_in_flight=2,
                                                 retries=1, backoff_seconds=2.0)})
    repo.add_minutes("etree", {minute: MinuteTotals(bytes=7)})

    rows = repo.minutes("ia", since=datetime(2026, 11, 12))
    assert rows == [(minute, MinuteTotals(bytes=150, peak_requests_in_flight=3,
                                          rate_limited=1, retries=1, backoff_seconds=3.5))]
    assert repo.minutes("etree", since=datetime(2026, 11, 12)) == [(minute, MinuteTotals(bytes=7))]


def test_minutes_before_the_window_are_left_out(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    repo.add_minutes("ia", {
        datetime(2026, 11, 11, 8, 59): MinuteTotals(bytes=1),
        datetime(2026, 11, 11, 9, 0): MinuteTotals(bytes=2),
    })
    rows = repo.minutes("ia", since=datetime(2026, 11, 11, 9, 0))
    assert [m for m, _ in rows] == [datetime(2026, 11, 11, 9, 0)]


def _event(at: datetime, kind: EventKind = EventKind.RETRY, **kw: object) -> NetworkEvent:
    return NetworkEvent(at=at, kind=kind, **kw)  # type: ignore[arg-type]


def test_events_newest_first_limited_with_a_total(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = datetime(2026, 11, 12, 9, 0)
    repo.add_events("ia", [
        _event(base.replace(second=s), EventKind.RATE_LIMITED, http_status=429,
               collection="gd", identifier=f"show-{s}", wait_seconds=30.0, detail="HTTP 429")
        for s in range(5)
    ])
    repo.add_events("etree", [_event(base)])

    events = repo.events("ia", since=base, limit=3)
    assert [e.identifier for e in events] == ["show-4", "show-3", "show-2"]
    assert events[0] == NetworkEvent(
        at=base.replace(second=4), kind=EventKind.RATE_LIMITED, http_status=429,
        collection="gd", identifier="show-4", wait_seconds=30.0, detail="HTTP 429",
    )
    assert repo.events_total("ia", since=base.replace(second=2)) == 3
    assert repo.last_event("ia", since=base) == events[0]
    assert repo.last_event("ia", since=base.replace(minute=1)) is None


def test_trim_drops_history_older_than_the_cutoff(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    old, recent = datetime(2026, 10, 1, 9, 0), datetime(2026, 11, 1, 9, 0)
    repo.add_minutes("ia", {old: MinuteTotals(bytes=1), recent: MinuteTotals(bytes=2)})
    repo.add_events("ia", [_event(old), _event(recent)])

    repo.trim(datetime(2026, 10, 13, 12, 0))

    assert [m for m, _ in repo.minutes("ia", since=datetime(2026, 1, 1))] == [recent]
    assert [e.at for e in repo.events("ia", since=datetime(2026, 1, 1), limit=10)] == [recent]


def test_a_pre_v10_database_has_no_network_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "state.db")
    conn.execute("DROP TABLE network_minutes")
    conn.execute("DROP TABLE network_events")
    repo = NetworkRepo(conn)
    assert not repo.has_tables()


# --- the failure causes a plugin reports (§spec:source-plugins) ----------------------



def _status_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status}", response=response)


@pytest.mark.parametrize(
    ("exc", "cause"),
    [
        (_status_error(429), FailureCause.RATE_LIMITED),
        (_status_error(503), FailureCause.SERVER_ERROR),
        (_status_error(403), FailureCause.REFUSED),
        (_status_error(404), None),
        (requests.Timeout("read timed out"), FailureCause.TIMEOUT),
        (requests.ConnectTimeout("connect timed out"), FailureCause.TIMEOUT),
        (httpx.ReadTimeout("slow"), FailureCause.TIMEOUT),
        (TimeoutError("slow"), FailureCause.TIMEOUT),
        (urllib3.exceptions.ReadTimeoutError(None, "/", "slow"), FailureCause.TIMEOUT),
        (requests.ConnectionError("reset"), FailureCause.CONNECTION_ERROR),
        (requests.exceptions.ChunkedEncodingError("Connection broken"),
         FailureCause.CONNECTION_ERROR),
        (httpx.ConnectError("refused"), FailureCause.CONNECTION_ERROR),
        (ConnectionResetError("reset"), FailureCause.CONNECTION_ERROR),
        (urllib3.exceptions.NewConnectionError(None, "no route"),  # type: ignore[arg-type]
         FailureCause.CONNECTION_ERROR),
        (urllib3.exceptions.ProtocolError("Connection aborted"), FailureCause.CONNECTION_ERROR),
        (ValueError("bad json"), None),
    ],
)
def test_failure_cause_names_what_went_wrong(exc: BaseException, cause: FailureCause | None) -> None:
    assert failure_cause(exc) == cause


def _fake() -> FakePlugin:
    return FakePlugin(SourceConfig(name="f", type="fake",
                                   collections=[CollectionConfig(name="c", query="*")]))


def test_a_listing_retry_reaches_the_core_only_while_it_listens() -> None:
    plugin = _fake()
    heard: list[ListingRetry] = []
    plugin.report_listing_retry(cause=FailureCause.TIMEOUT)  # nobody listening: no-op
    with listing_retries_to(heard.append):
        plugin.report_listing_retry(cause=FailureCause.RATE_LIMITED, http_status=429,
                                    wait_seconds=3.0, detail="HTTP 429")
    plugin.report_listing_retry(cause=FailureCause.TIMEOUT)
    assert heard == [ListingRetry(FailureCause.RATE_LIMITED, 429, 3.0, "HTTP 429")]


def test_a_failing_listener_never_fails_the_listing() -> None:
    def broken(_retry: ListingRetry) -> None:
        raise RuntimeError("database busy")

    with listing_retries_to(broken):
        _fake().report_listing_retry(cause=FailureCause.SERVER_ERROR)


# --- what a run records (§spec:network-activity "Recorded by the run") ------------


class _Time:
    def __init__(self) -> None:
        self.wall = datetime(2026, 11, 12, 9, 0, 0)
        self.mono = 1000.0

    def now(self) -> datetime:
        return self.wall

    def clock(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    connect(path).close()
    return path


def _recorder(db: Path, t: _Time) -> ActivityRecorder:
    return ActivityRecorder(db, now=t.now, clock=t.clock, start_timer=False)


def _minutes(db: Path, source: str = "ia") -> dict[datetime, MinuteTotals]:
    with closing(connect(db)) as conn:
        return dict(NetworkRepo(conn).minutes(source, since=datetime(2000, 1, 1)))


def _events(db: Path, source: str = "ia") -> list[NetworkEvent]:
    with closing(connect(db)) as conn:
        return list(reversed(NetworkRepo(conn).events(source, datetime(2000, 1, 1), 1000)))


def test_bytes_land_in_the_minute_they_were_measured(db: Path, tmp_path: Path) -> None:
    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    tmp = tmp_path / ".tmp-show"
    tmp.mkdir()
    with p.request(), p.fetch_attempt("show", tmp):
        (tmp / "a.flac").write_bytes(b"a" * 1000)
        t.advance(2)
        rec.tick()
        t.advance(60)
        (tmp / "b.flac").write_bytes(b"b" * 500)
    rec.end(p, "completed")

    nine, nine_one = datetime(2026, 11, 12, 9, 0), datetime(2026, 11, 12, 9, 1)
    assert _minutes(db) == {
        nine: MinuteTotals(bytes=1000, peak_requests_in_flight=1),
        nine_one: MinuteTotals(bytes=500, peak_requests_in_flight=1),
    }
    assert p.bytes_fetched() == 1500


def test_kept_files_are_excluded_and_a_refetch_after_a_bad_checksum_counts(
    db: Path, tmp_path: Path
) -> None:
    """Bytes downloaded means bytes fetched (§spec:network-activity): a file kept from
    an earlier attempt is not this run's, a file fetched again is."""
    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    tmp = tmp_path / ".tmp-show"
    tmp.mkdir()
    (tmp / "kept.flac").write_bytes(b"k" * 700)  # an interrupted run's finished file
    with p.fetch_attempt("show", tmp):
        (tmp / "new.flac").write_bytes(b"n" * 1000)
        t.advance(2)
        rec.tick()
        (tmp / "new.flac").unlink()  # checksum mismatch: the plugin drops it
    with p.fetch_attempt("show", tmp):
        (tmp / "new.flac").write_bytes(b"n" * 1000)
    # What the core adds after the plugin returns — kept files linked in, tags, the
    # metadata sidecar — was never fetched.
    (tmp / "metadata.json").write_bytes(b"{}" * 100)
    t.advance(2)
    rec.tick()
    rec.end(p, "completed")

    assert p.bytes_fetched() == 2000
    assert sum(m.bytes for m in _minutes(db).values()) == 2000


def test_a_busy_write_carries_its_totals_and_events_to_the_next(db: Path) -> None:

    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    p.network_event(EventKind.SERVER_ERROR, identifier="show", http_status=503)
    with closing(sqlite3.connect(db, isolation_level=None)) as other:
        other.execute("BEGIN IMMEDIATE")
        rec.tick()  # skipped: another writer holds the database
        other.execute("ROLLBACK")
    assert _minutes(db) == {}
    rec.tick()
    assert _minutes(db)[datetime(2026, 11, 12, 9, 0)].server_errors == 1
    assert [e.kind for e in _events(db)] == [EventKind.SERVER_ERROR]
    rec.tick()  # written once, never twice
    assert _minutes(db)[datetime(2026, 11, 12, 9, 0)].server_errors == 1
    assert len(_events(db)) == 1


def test_collections_of_one_source_add_up_to_one_source(db: Path, tmp_path: Path) -> None:
    t = _Time()
    rec = _recorder(db, t)
    a = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    b = rec.open_entry("ia", "phish", ActivityKind.SYNC)
    other = rec.open_entry("etree", "lma", ActivityKind.SYNC)
    with a.request(), b.request(), other.request():
        rec.tick()
    rec.tick()

    assert _minutes(db)[datetime(2026, 11, 12, 9, 0)].peak_requests_in_flight == 2
    assert _minutes(db, "etree")[datetime(2026, 11, 12, 9, 0)].peak_requests_in_flight == 1


def test_requests_in_flight_reach_the_entry_and_clear_when_it_ends(db: Path) -> None:
    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    with p.request(), p.request():
        rec.tick()
        assert _entry(db, p)["requests_in_flight"] == 2
    rec.end(p, "completed")
    assert _entry(db, p)["requests_in_flight"] is None


def _entry(db: Path, p: Progress) -> sqlite3.Row:
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM run_activity WHERE id=?", (p.activity_id,)).fetchone()


def test_events_count_into_their_minute(db: Path) -> None:

    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    p.network_event(EventKind.RATE_LIMITED, identifier="show", http_status=429,
                    wait_seconds=30.0, detail="HTTP 429")
    p.network_event(EventKind.RETRY, identifier="show", detail="HTTP 429")
    p.network_event(EventKind.BACKOFF, identifier="show", wait_seconds=30.0)
    t.advance(61)
    p.network_event(EventKind.TIMEOUT, identifier="other")
    p.network_event(EventKind.CONNECTION_ERROR, identifier="other")
    p.network_event(EventKind.REFUSED, identifier="sbd", http_status=403)
    rec.end(p, "completed")

    assert _minutes(db) == {
        datetime(2026, 11, 12, 9, 0): MinuteTotals(rate_limited=1, retries=1,
                                                   backoff_seconds=30.0),
        datetime(2026, 11, 12, 9, 1): MinuteTotals(timeouts=1, connection_errors=1),
    }
    first = _events(db)[0]
    assert first == NetworkEvent(
        at=datetime(2026, 11, 12, 9, 0), kind=EventKind.RATE_LIMITED, http_status=429,
        collection="gd", identifier="show", wait_seconds=30.0, detail="HTTP 429",
    )
    assert [e.kind for e in _events(db)][-1] == EventKind.REFUSED


def test_a_listing_is_a_request_and_its_retries_are_events(db: Path) -> None:

    t = _Time()
    rec = _recorder(db, t)
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    plugin = _fake()
    with p.listing():
        plugin.report_listing_retry(cause=FailureCause.RATE_LIMITED, http_status=429,
                                    wait_seconds=3.0, detail="HTTP 429")
        rec.tick()
        assert _entry(db, p)["requests_in_flight"] == 1
    rec.end(p, "completed")

    assert [(e.kind, e.collection, e.identifier, e.wait_seconds) for e in _events(db)] == [
        (EventKind.RATE_LIMITED, "gd", None, None),
        (EventKind.RETRY, "gd", None, None),
        (EventKind.BACKOFF, "gd", None, 3.0),
    ]
    assert _minutes(db)[datetime(2026, 11, 12, 9, 0)] == MinuteTotals(
        peak_requests_in_flight=1, rate_limited=1, retries=1, backoff_seconds=3.0,
    )


def test_a_listing_that_fails_records_its_cause(db: Path) -> None:

    rec = _recorder(db, _Time())
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    with pytest.raises(requests.ConnectionError), p.listing():
        raise requests.ConnectionError("unreachable")
    rec.end(p, "failed_before_completion")
    assert [(e.kind, e.detail) for e in _events(db)] == [
        (EventKind.CONNECTION_ERROR, "unreachable"),
    ]


def test_a_run_that_ends_trims_network_history_older_than_30_days(db: Path) -> None:
    t = _Time()
    with closing(connect(db)) as conn:
        repo = NetworkRepo(conn)
        repo.add_minutes("ia", {t.wall - timedelta(days=31): MinuteTotals(bytes=1)})
        repo.add_events("ia", [NetworkEvent(at=t.wall - timedelta(days=31), kind=EventKind.RETRY)])
    rec = _recorder(db, t)
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    p.network_event(EventKind.RETRY)
    rec.end(p, "completed")
    rec.close()

    assert list(_minutes(db)) == [datetime(2026, 11, 12, 9, 0)]
    assert [e.at for e in _events(db)] == [t.wall]


def test_recording_off_still_counts_the_runs_bytes(tmp_path: Path) -> None:
    """A run whose progress cannot be recorded still reports what it fetched."""
    p = Progress(ActivityKind.SYNC)
    tmp = tmp_path / ".tmp-show"
    tmp.mkdir()
    with p.fetch_attempt("show", tmp):
        (tmp / "a.flac").write_bytes(b"a" * 1234)
    assert p.bytes_fetched() == 1234


# --- events from the core's own retries ----------------------------------------------


def test_a_rate_limited_lookup_records_the_hit_the_retry_and_the_wait(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr("shakedown.discovery._sleep", lambda _s: None)
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "7"
    FakePlugin.lookup_faults["a"] = [requests.HTTPError("429", response=response),
                                     _status_error(404)]
    rec = _recorder(db, _Time())
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)

    result = lookup(_fake(), "a", SourceBudget(2), p)

    assert isinstance(result, LookupFailure)
    rec.end(p, "completed")
    assert [(e.kind, e.identifier, e.http_status, e.wait_seconds) for e in _events(db)] == [
        (EventKind.RATE_LIMITED, "a", 429, 7.0),
        (EventKind.RETRY, "a", None, None),
        (EventKind.BACKOFF, "a", None, 7.0),
    ]


def test_a_fetch_that_fails_then_succeeds_records_its_cause_retry_and_wait(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shakedown.sync as sync_module
    from shakedown.discovery import ItemDescriptor
    from shakedown.models import Manifest, ManifestFile
    from shakedown.plugins.base import FetchResult

    monkeypatch.setattr(sync_module.time, "sleep", lambda _s: None)
    answers = [
        FetchResult(success=False, error="HTTP 503", retriable=True,
                    cause=FailureCause.SERVER_ERROR, http_status=503),
        FetchResult(success=True),
    ]

    class Flaky(FakePlugin):
        def fetch(self, identifier, files, dest_dir):  # type: ignore[override]
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "a.flac").write_bytes(b"a")
            return answers.pop(0)

    rec = _recorder(db, _Time())
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    desc = ItemDescriptor("show", Manifest(files=(ManifestFile("a.flac", 1, None),)))

    result = sync_module._fetch_with_retries(Flaky(_fake().source_config), desc,
                                             tmp_path / ".tmp-show", progress=p)

    assert result.success
    rec.end(p, "completed")
    assert [(e.kind, e.http_status, e.wait_seconds) for e in _events(db)] == [
        (EventKind.SERVER_ERROR, 503, None),
        (EventKind.RETRY, None, None),
        (EventKind.BACKOFF, None, sync_module._backoff_seconds(1)),
    ]


def test_a_refused_fetch_is_a_refused_event(
    db: Path, tmp_path: Path
) -> None:
    import shakedown.sync as sync_module
    from shakedown.discovery import ItemDescriptor
    from shakedown.models import Manifest, ManifestFile
    from shakedown.plugins.base import FetchResult

    class Refusing(FakePlugin):
        def fetch(self, identifier, files, dest_dir):  # type: ignore[override]
            return FetchResult(success=False, error="refused", unavailable=True)

    rec = _recorder(db, _Time())
    p = rec.open_entry("ia", "gd", ActivityKind.SYNC)
    desc = ItemDescriptor("sbd", Manifest(files=(ManifestFile("a.flac", 1, None),)))

    sync_module._fetch_with_retries(Refusing(_fake().source_config), desc,
                                    tmp_path / ".tmp-sbd", progress=p)

    rec.end(p, "completed")
    assert [(e.kind, e.identifier) for e in _events(db)] == [(EventKind.REFUSED, "sbd")]


# --- a sync's bytes (§spec:network-activity "Bytes downloaded means bytes fetched") --


def test_a_syncs_bytes_are_what_it_fetched_and_its_series_sums_to_them(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed show's kept file is not counted; a show that failed still fetched its
    bytes. The run record and the per-minute series agree."""
    import shakedown.sync as sync_module
    from shakedown.models import ManifestFile
    from shakedown.plugins.base import FetchResult
    from shakedown.state import RunRepo
    from shakedown.sync import run_sync
    from tests.conftest import make_config
    from tests.fake_plugin import FakeFile, FakeItem

    monkeypatch.setattr(sync_module.time, "sleep", lambda _s: None)
    config = make_config(*tmp_roots)
    FakePlugin.items["resumed"] = FakeItem("resumed", files=[
        FakeFile("d1.flac", b"k" * 700, format="Flac"),
        FakeFile("d2.flac", b"n" * 300, format="Flac"),
    ])
    FakePlugin.items["broken"] = FakeItem("broken", files=[
        FakeFile("x.flac", b"b" * 400, format="Flac"),
    ])
    # An interrupted run finished d1.flac of "resumed".
    tmp = config.archive_root / "fake-src" / "coll1" / ".tmp-resumed"
    tmp.mkdir(parents=True)
    (tmp / "d1.flac").write_bytes(b"k" * 700)
    real_fetch = FakePlugin.fetch

    def fetch(self, identifier: str, files, dest_dir: Path) -> FetchResult:
        if identifier == "broken":
            dest_dir.mkdir(parents=True, exist_ok=True)
            (dest_dir / "x.flac").write_bytes(b"b" * 400)
            return FetchResult(success=False, error="malformed", retriable=False)
        wanted: list[ManifestFile] = [
            mf for mf in files if not (dest_dir / mf.name).is_file()
        ]
        return real_fetch(self, identifier, wanted, dest_dir)

    monkeypatch.setattr(FakePlugin, "fetch", fetch)

    run_sync(config)

    assert config.state_db is not None
    with closing(connect(config.state_db)) as conn:
        run = RunRepo(conn).latest("fake-src", "coll1")
        minutes = NetworkRepo(conn).minutes("fake-src", since=datetime(2000, 1, 1))
    assert run is not None
    assert run.bytes_downloaded == 300 + 400
    assert sum(t.bytes for _, t in minutes) == run.bytes_downloaded
    assert max(t.peak_requests_in_flight for _, t in minutes) >= 1
