"""Network activity (§spec:network-activity): the per-source, per-minute totals and the
network events a run records, and how they are kept."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import httpx
import pytest
import requests
import urllib3.exceptions

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.db import connect
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
