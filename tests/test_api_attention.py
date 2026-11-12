"""`GET /api/attention` (§spec:needs-attention): one entry per collection per kind of
thing that needs the operator, and per-collection library health, each from the same
computation `status` uses."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.activity import ActivityKind, ActivityRepo
from shakedown.config import CollectionConfig, Config, SourceConfig, WebhookHandoff
from shakedown.dashboard import VIEW_TTL_S
from shakedown.db import connect
from shakedown.lock import writer_lock
from shakedown.models import Item, ItemStatus, OperationStatus, OperationType, RunKind
from shakedown.server import build_app
from shakedown.state import ItemRepo, OperationOutcomeRepo, PlacementRepo, RunRepo
from shakedown.status import print_status
from shakedown.sync import run_sync
from tests.conftest import make_config, write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin
from tests.test_durable_handoff import DOWN, GOOD, _Webhooks
from tests.test_server import _Clock

SRC = "fake-src"
ENTRY_FIELDS = {"kind", "source", "collection", "since", "identifiers", "detail"}
HEALTH_FIELDS = {
    "source", "collection", "restricted", "removed_upstream", "drift_files",
    "not_playable", "playable_copies",
}


def _app(config: Config, clock: _Clock | None = None):  # type: ignore[no-untyped-def]
    path = write_config(config.archive_root.parent / "cfg.yaml", config)
    return build_app(path, clock=clock or _Clock())


async def _get(app, path: str = "/api/attention") -> dict[str, Any]:  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        r = await client.get(path)
        assert r.status_code == 200, r.text
        return r.json()


def _attention(config: Config) -> dict[str, Any]:
    """A fresh answer: a new app, so no shared answer is reused."""
    return asyncio.run(_get(_app(config)))


def _status(config: Config, capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    capsys.readouterr()
    print_status(config, as_json=True)
    return json.loads(capsys.readouterr().out)


def _summary(status: list[dict[str, Any]], collection: str = "coll1") -> dict[str, Any]:
    return next(s for s in status if s["collection"] == collection)


def _kinds(answer: dict[str, Any]) -> list[tuple[str, str]]:
    return [(e["collection"], e["kind"]) for e in answer["entries"]]


def _entry(answer: dict[str, Any], kind: str, collection: str = "coll1") -> dict[str, Any]:
    found = [e for e in answer["entries"] if e["kind"] == kind and e["collection"] == collection]
    assert len(found) == 1, answer["entries"]
    return found[0]


def _same_instant(api_time: str | None, status_time: str | None) -> bool:
    """An API time (with its offset) names the same instant as status's naive time."""
    assert api_time is not None and status_time is not None
    assert datetime.fromisoformat(api_time).utcoffset() is not None
    return datetime.fromisoformat(api_time) == datetime.fromisoformat(status_time).astimezone()


def _show(identifier: str, *files: tuple[str, bytes, str | None]) -> None:
    FakePlugin.items[identifier] = FakeItem(
        identifier=identifier,
        files=[FakeFile(name=n, content=c, format=f) for n, c, f in files]
        or [FakeFile(name=f"{identifier}.flac", content=identifier.encode(), format="Flac")],
    )


def _two_collections(archive: Path, library: Path, **coll2: Any) -> Config:
    return Config(
        archive_root=archive,
        library_root=library,
        sources=[
            SourceConfig(
                name=SRC,
                type="fake",
                collections=[
                    CollectionConfig(name="coll1", query="*", library_layout="passthrough"),
                    CollectionConfig(
                        name="coll2", query="*", library_layout="passthrough", **coll2
                    ),
                ],
            )
        ],
    )


# --- shape and the healthy mirror ---------------------------------------------------


def test_a_healthy_mirror_needs_nothing(tmp_roots: tuple[Path, Path]) -> None:
    config = _two_collections(*tmp_roots)
    _show("gd-a")
    assert run_sync(config) == 0

    answer = _attention(config)

    assert set(answer) == {"generated_at", "entries", "health"}
    assert answer["entries"] == []
    assert answer["health"] == [
        {
            "source": SRC, "collection": name, "restricted": [], "removed_upstream": [],
            "drift_files": 0, "not_playable": [], "playable_copies": [],
        }
        for name in ("coll1", "coll2")
    ]


# --- one entry per collection per kind ----------------------------------------------


def test_a_failed_show_is_listed_until_a_good_sync(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots)
    _show("gd-ok")
    _show("gd-bad")
    FakePlugin.lookup_faults["gd-bad"] = [RuntimeError("source said no")]
    assert run_sync(config) == 1

    answer = _attention(config)
    summary = _summary(_status(config, capsys))
    # The sync's own recovery (finished with item issues) rides on the entry it
    # explains, not as a second entry for the same show.
    assert _kinds(answer) == [("coll1", "failed_shows")]
    entry = answer["entries"][0]
    assert set(entry) == ENTRY_FIELDS
    recovery = entry["detail"].pop("recovery")
    assert entry == {
        "kind": "failed_shows", "source": SRC, "collection": "coll1", "since": None,
        "identifiers": ["gd-bad"], "detail": {},
    }
    assert len(entry["identifiers"]) == summary["counts"]["failed"]
    assert recovery["status"] == summary["recovery"]["status"] == "completed_with_item_issues"
    assert recovery["affected_item"] == "gd-bad"
    assert recovery["safe_next_action"] == summary["recovery"]["safe_next_action"]

    FakePlugin.lookup_faults.clear()
    assert run_sync(config) == 0
    assert _attention(config)["entries"] == []


def test_three_hundred_failed_shows_are_one_entry(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        items = ItemRepo(conn)
        for i in range(300):
            items.upsert(Item(SRC, "coll1", f"gd-{i:03d}", ItemStatus.FAILED))
        conn.commit()

    answer = _attention(config)

    assert _kinds(answer) == [("coll1", "failed_shows")]
    assert answer["entries"][0]["identifiers"] == [f"gd-{i:03d}" for i in range(300)]


def test_a_recovery_carries_status_recovery_object(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots)
    started = datetime(2026, 11, 10, 3, 0, 0)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        outcomes = OperationOutcomeRepo(conn)
        outcome = outcomes.start(OperationType.RESTAGE, SRC, "coll1", started)
        outcomes.finish(
            outcome, OperationStatus.FAILED_BEFORE_COMPLETION, started + timedelta(minutes=2),
            phase="link", affected_item="gd-a", affected_path="/library/gd-a/t1.flac",
            safe_next_action="rerun restage", errors=["disk full"],
        )
        conn.commit()

    entry = _entry(_attention(config), "recovery")
    recovery = _summary(_status(config, capsys))["recovery"]

    assert entry["identifiers"] == ["gd-a"]
    assert _same_instant(entry["since"], recovery["finished_at"])
    times = ("started_at", "updated_at", "finished_at")
    assert {k: v for k, v in entry["detail"].items() if k not in times} == {
        k: v for k, v in recovery.items() if k not in times
    }
    for key in times:
        assert _same_instant(entry["detail"][key], recovery[key])
    assert entry["detail"]["safe_next_action"] == "rerun restage"


def test_a_stale_collection_has_one_collection_wide_entry(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    _show("gd-a")
    assert run_sync(config) == 0
    with patch.object(FakePlugin, "list_items", side_effect=RuntimeError("down")):
        assert run_sync(config) == 1

    answer = _attention(config)
    stale = _entry(answer, "stale")
    assert stale == {
        "kind": "stale", "source": SRC, "collection": "coll1", "since": None,
        "identifiers": [], "detail": {},
    }

    assert run_sync(config) == 0
    assert "stale" not in {e["kind"] for e in _attention(config)["entries"]}


def test_a_rescheduled_collection_is_listed_until_it_syncs(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = _two_collections(*tmp_roots)
    _show("gd-a")
    assert run_sync(config) == 0
    with writer_lock(config):
        assert run_sync(config, source_filter=SRC, collection_filter="coll2") == 1

    entry = _entry(_attention(config), "rescheduled", "coll2")
    since = _summary(_status(config, capsys), "coll2")["rescheduled"]["since"]
    assert entry["identifiers"] == [] and entry["detail"] == {}
    assert _same_instant(entry["since"], since)

    assert run_sync(config, source_filter=SRC, collection_filter="coll2") == 0
    assert _attention(config)["entries"] == []


def test_a_blocked_handoff_is_listed_until_delivered(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    archive, library = tmp_roots
    config = make_config(archive, library, on_complete=WebhookHandoff(webhook=DOWN))
    _show("gd-a")
    with patch("shakedown.notify.httpx.post", side_effect=_Webhooks()):
        assert run_sync(config) == 0

    entry = _entry(_attention(config), "handoff_pending")
    pending = _summary(_status(config, capsys))["handoff_pending"]
    assert entry["identifiers"] == pending["identifiers"] == ["gd-a"]
    assert entry["detail"] == {"shows": 1, "last_reason": pending["last_reason"]}
    assert _same_instant(entry["since"], pending["since"])

    fixed = make_config(archive, library, on_complete=WebhookHandoff(webhook=GOOD))
    with patch("shakedown.notify.httpx.post", side_effect=_Webhooks()):
        assert run_sync(fixed) == 0
    assert _attention(fixed)["entries"] == []


def test_an_undelivered_failure_notice_names_the_run(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    from shakedown.config import FailureNotification, NotificationsConfig

    config = make_config(*tmp_roots)
    config.notifications = NotificationsConfig(on_failure=FailureNotification(webhook=DOWN))
    _show("gd-a")
    with (
        patch("shakedown.notify.httpx.post", side_effect=_Webhooks()),
        patch.object(FakePlugin, "list_items", side_effect=RuntimeError("down")),
    ):
        assert run_sync(config) == 1

    entry = _entry(_attention(config), "failure_notice_undelivered")
    last_run = _summary(_status(config, capsys))["last_run"]
    assert entry["identifiers"] == []
    assert entry["detail"] == {
        "failure_notice_error": last_run["failure_notice_error"],
        "run_id": last_run["run_id"],
    }
    assert entry["detail"]["failure_notice_error"] == "webhook unreachable: connection refused"
    assert _same_instant(entry["since"], last_run["finished_at"])


def test_separations_and_unplaced_files_match_status(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        placements = PlacementRepo(conn)
        placements.set_folder(SRC, "coll1", "gd-a.sbd", "Barton Hall [gd-a.sbd]", separated=True)
        placements.set_folder(SRC, "coll1", "gd-a.aud", "Barton Hall [gd-a.aud]", separated=True)
        placements.add_unplaced(SRC, "coll1", "Buffalo/notes.txt")
        conn.commit()

    answer = _attention(config)
    summary = _summary(_status(config, capsys))

    separations = _entry(answer, "separations")
    assert separations["since"] is None
    assert separations["identifiers"] == ["gd-a.aud", "gd-a.sbd"]
    assert separations["detail"] == {"separations": summary["separations"]}
    unplaced = _entry(answer, "unplaced_files")
    assert unplaced["since"] is None and unplaced["identifiers"] == []
    assert unplaced["detail"] == {"unplaced_files": summary["unplaced_files"]}
    # Entries keep the spec's kind order within a collection.
    assert _kinds(answer) == [("coll1", "separations"), ("coll1", "unplaced_files")]


# --- runs that ended unexpectedly ---------------------------------------------------


def _killed(conn: sqlite3.Connection, kind: ActivityKind, *, heartbeat: datetime,
            collection: str = "coll1", run: bool = True) -> tuple[int | None, int]:
    """An entry (and, for kinds that have one, an open run record) whose run stopped
    reporting at ``heartbeat``: as a SIGKILL leaves them."""
    run_id = None
    if run:
        run_id = RunRepo(conn).start(SRC, collection, heartbeat - timedelta(minutes=5),
                                     kind=RunKind(kind.value)).id
    activity_id = ActivityRepo(conn).create(
        SRC, collection, kind, run_id=run_id, started_by_serve=False,
        at=heartbeat - timedelta(minutes=5),
    )
    conn.execute("UPDATE run_activity SET last_heartbeat_at=? WHERE id=?",
                 (heartbeat.isoformat(), activity_id))
    return run_id, activity_id


def _ended(conn: sqlite3.Connection, kind: ActivityKind, result: str, *,
           at: datetime) -> None:
    repo = ActivityRepo(conn)
    activity_id = repo.create(SRC, "coll1", kind, run_id=None, started_by_serve=False,
                              at=at - timedelta(minutes=1))
    repo.end(activity_id, result, at)


def test_a_killed_sync_is_listed_until_the_next_good_sync(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots)
    heartbeat = datetime.now() - timedelta(seconds=30)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        run_id, activity_id = _killed(conn, ActivityKind.SYNC, heartbeat=heartbeat)
        conn.commit()

    entry = _entry(_attention(config), "ended_unexpectedly")
    assert _summary(_status(config, capsys))["last_run"]["end_reason"] == "stopped_reporting"
    assert entry["identifiers"] == []
    assert entry["detail"] == {
        "runs": [{"run_id": run_id, "activity_id": activity_id, "kind": "sync",
                  "finished_at": entry["since"]}],
    }
    assert datetime.fromisoformat(entry["since"]) == heartbeat.astimezone()

    _show("gd-a")
    assert run_sync(config) == 0  # closes the killed run, then ends `completed`
    assert _attention(config)["entries"] == []


def test_a_killed_sync_carries_its_unfinished_recovery_as_one_entry(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A SIGKILL also leaves the sync's outcome `in_progress`: status's recovery rides on
    the unexpected end it explains."""
    config = make_config(*tmp_roots)
    heartbeat = datetime.now() - timedelta(seconds=30)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        _killed(conn, ActivityKind.SYNC, heartbeat=heartbeat)
        OperationOutcomeRepo(conn).start(OperationType.SYNC, SRC, "coll1",
                                         heartbeat - timedelta(minutes=5),
                                         safe_next_action="rerun sync")
        conn.commit()

    answer = _attention(config)
    recovery = _summary(_status(config, capsys))["recovery"]
    assert _kinds(answer) == [("coll1", "ended_unexpectedly")]
    carried = answer["entries"][0]["detail"]["recovery"]
    assert carried["status"] == recovery["status"] == "in_progress"
    assert carried["safe_next_action"] == recovery["safe_next_action"] == "rerun sync"


def test_a_running_sync_is_not_a_recovery(tmp_roots: tuple[Path, Path]) -> None:
    """While a sync still reports, its outcome is `in_progress`: that is the run going,
    shown by the activity view, not something for the operator."""
    config = make_config(*tmp_roots)
    now = datetime.now()
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        _killed(conn, ActivityKind.CATCH_UP, heartbeat=now)  # still reporting
        OperationOutcomeRepo(conn).start(OperationType.SYNC, SRC, "coll1",
                                         now - timedelta(minutes=5))
        conn.commit()

    assert _attention(config)["entries"] == []


def test_a_recovery_of_another_operation_stays_its_own_entry(
    tmp_roots: tuple[Path, Path],
) -> None:
    """An unfinished restage does not explain a killed sync."""
    config = make_config(*tmp_roots)
    heartbeat = datetime.now() - timedelta(seconds=30)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        _killed(conn, ActivityKind.SYNC, heartbeat=heartbeat)
        OperationOutcomeRepo(conn).start(OperationType.RESTAGE, SRC, "coll1", heartbeat)
        conn.commit()

    answer = _attention(config)
    assert _kinds(answer) == [("coll1", "recovery"), ("coll1", "ended_unexpectedly")]
    assert "recovery" not in _entry(answer, "ended_unexpectedly")["detail"]


@pytest.mark.parametrize(
    ("kind", "result", "clears"),
    [
        (ActivityKind.SYNC, "completed", True),
        (ActivityKind.CATCH_UP, "completed_with_item_issues", True),
        (ActivityKind.SYNC, "failed_before_completion", False),
        (ActivityKind.SYNC, "stale_enumeration", False),
        (ActivityKind.VERIFY, "completed", False),
    ],
)
def test_only_a_good_run_of_the_same_kind_clears_it(
    tmp_roots: tuple[Path, Path], kind: ActivityKind, result: str, clears: bool
) -> None:
    config = make_config(*tmp_roots)
    now = datetime.now()
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        _killed(conn, ActivityKind.SYNC, heartbeat=now - timedelta(hours=2), run=False)
        ActivityRepo(conn).close_stale(now - timedelta(hours=1))  # as the next locked run
        _ended(conn, kind, result, at=now - timedelta(minutes=10))
        conn.commit()

    kinds = {e["kind"] for e in _attention(config)["entries"]}
    assert ("ended_unexpectedly" not in kinds) is clears


def test_a_killed_restage_is_listed_by_its_activity_entry(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    now = datetime.now()
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        _, activity_id = _killed(conn, ActivityKind.RESTAGE,
                                 heartbeat=now - timedelta(minutes=30), run=False)
        conn.commit()

    [run] = _entry(_attention(config), "ended_unexpectedly")["detail"]["runs"]
    assert (run["run_id"], run["activity_id"], run["kind"]) == (None, activity_id, "restage")

    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        _ended(conn, ActivityKind.RESTAGE, "completed", at=now - timedelta(minutes=1))
        conn.commit()
    assert _attention(config)["entries"] == []


def test_an_unexpected_end_older_than_thirty_days_is_dropped(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        _killed(conn, ActivityKind.VERIFY, heartbeat=datetime.now() - timedelta(days=31),
                run=False)
        conn.commit()

    assert _attention(config)["entries"] == []


def test_a_run_serve_closed_after_a_kill_is_listed_at_once(tmp_roots: tuple[Path, Path]) -> None:
    """serve's watcher closes a killed child's record before its heartbeat goes stale."""
    config = make_config(*tmp_roots)
    now = datetime.now()
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        run_id, activity_id = _killed(conn, ActivityKind.SYNC, heartbeat=now - timedelta(seconds=2))
        RunRepo(conn).close_abandoned(run_id, "run ended unexpectedly (killed by SIGKILL)", now)
        conn.commit()

    [run] = _entry(_attention(config), "ended_unexpectedly")["detail"]["runs"]
    assert (run["run_id"], run["activity_id"], run["kind"]) == (run_id, activity_id, "sync")


def test_a_killed_sync_older_than_thirty_days_is_dropped_even_while_still_latest(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A collection that has not run since: its run record is still the latest run."""
    config = make_config(*tmp_roots)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        _killed(conn, ActivityKind.SYNC, heartbeat=datetime.now() - timedelta(days=31))
        conn.commit()
    assert _attention(config)["entries"] == []

    # Its entry trimmed away, the open record alone is still older than 30 days.
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        conn.execute("DELETE FROM run_activity")
        conn.commit()
    assert _attention(config)["entries"] == []


def test_an_open_run_with_no_entry_matches_status(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A run record whose entry was never written: status falls back to its start."""
    config = make_config(*tmp_roots)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        started = datetime.now() - timedelta(minutes=2)
        run_id = RunRepo(conn).start(SRC, "coll1", started).id
        conn.commit()

    entry = _entry(_attention(config), "ended_unexpectedly")
    last_run = _summary(_status(config, capsys))["last_run"]
    assert last_run["end_reason"] == "stopped_reporting"
    # Its start is its last sign of life, as status and the run history read it.
    assert _same_instant(entry["since"], last_run["started_at"])
    assert entry["detail"]["runs"] == [
        {"run_id": run_id, "activity_id": None, "kind": "sync", "finished_at": entry["since"]}
    ]


def test_a_pre_v10_database_answers_without_migrating(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        conn.execute("DROP TABLE run_activity")
        conn.execute("UPDATE schema_version SET version = 9")
        conn.commit()

    assert _attention(config)["entries"] == []
    with closing(sqlite3.connect(config.state_db)) as conn:  # type: ignore[arg-type]
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 9


# --- the batch's user-level path -----------------------------------------------------


def test_four_problems_are_listed_by_collection_and_each_leaves_once_resolved(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """§spec:needs-attention user-level test path: a failed show, a blocked handoff, a
    skipped tick, and a stray library file, each named by collection and show and
    matching status; resolving each removes its entry."""
    archive, library = tmp_roots
    blocked = _two_collections(archive, library, on_complete=WebhookHandoff(webhook=DOWN))
    hooks = _Webhooks()

    def sync(config: Config, collection: str) -> int:
        with patch("shakedown.notify.httpx.post", side_effect=hooks):
            return run_sync(config, source_filter=SRC, collection_filter=collection)

    _show("gd-a")
    _show("gd-bad")
    # coll2: its handoff target is down.
    assert sync(blocked, "coll2") == 0
    # coll1: gd-bad fails; a file I put in gd-a's library folder is found once gd-a's
    # files change upstream.
    FakePlugin.lookup_faults["gd-bad"] = [RuntimeError("source said no")]
    assert sync(blocked, "coll1") == 1
    stray = library / SRC / "coll1" / "gd-a" / "my-notes.txt"
    stray.write_text("mine")
    _show("gd-a", ("gd-a-remaster.flac", b"new", "Flac"))
    assert sync(blocked, "coll1") == 1
    # coll2's tick is skipped behind another run. Last: a finishing coll1 sync would
    # catch it up.
    with writer_lock(blocked):
        assert sync(blocked, "coll2") == 1

    answer = _attention(blocked)
    status = _status(blocked, capsys)
    # The failed show's sync also leaves status a `recovery`: it rides on failed_shows.
    assert _kinds(answer) == [
        ("coll1", "failed_shows"), ("coll1", "unplaced_files"),
        ("coll2", "rescheduled"), ("coll2", "handoff_pending"),
    ]
    one, two = _summary(status, "coll1"), _summary(status, "coll2")
    failed = _entry(answer, "failed_shows")
    assert failed["identifiers"] == ["gd-bad"]
    assert one["counts"]["failed"] == 1
    assert failed["detail"]["recovery"]["affected_item"] == "gd-bad"
    assert _entry(answer, "unplaced_files")["detail"]["unplaced_files"] == (
        one["unplaced_files"]
    ) == [str(stray)]
    assert _same_instant(_entry(answer, "rescheduled", "coll2")["since"],
                         two["rescheduled"]["since"])
    handoff = _entry(answer, "handoff_pending", "coll2")
    assert handoff["identifiers"] == two["handoff_pending"]["identifiers"] == ["gd-a", "gd-bad"]

    FakePlugin.lookup_faults.clear()  # the source serves gd-bad again
    assert sync(blocked, "coll2") == 0  # its handoff target is still down
    assert ("coll2", "rescheduled") not in _kinds(_attention(blocked))
    assert ("coll2", "handoff_pending") in _kinds(_attention(blocked))

    assert sync(blocked, "coll1") == 0
    assert {k for c, k in _kinds(_attention(blocked)) if c == "coll1"} == {"unplaced_files"}

    stray.unlink()
    assert sync(blocked, "coll1") == 0
    assert _kinds(_attention(blocked)) == [("coll2", "handoff_pending")]

    fixed = _two_collections(archive, library, on_complete=WebhookHandoff(webhook=GOOD))
    assert sync(fixed, "coll2") == 0
    assert _attention(fixed)["entries"] == []


# --- library health -----------------------------------------------------------------


def test_health_carries_status_library_facts(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots)
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        items = ItemRepo(conn)
        items.upsert(Item(SRC, "coll1", "gd-r", ItemStatus.UNAVAILABLE,
                          restriction_reason="darked"))
        items.upsert(Item(SRC, "coll1", "gd-gone", ItemStatus.DISAPPEARED,
                          disappeared_at=datetime(2026, 11, 1, 4, 0)))
        items.upsert(Item(SRC, "coll1", "gd-shn", ItemStatus.COMPLETE,
                          not_playable_reason="only in SHN; archive.org offers no playable copy"))
        conn.commit()

    answer = _attention(config)
    summary = _summary(_status(config, capsys))

    assert answer["entries"] == []  # facts, not to-dos
    [health] = answer["health"]
    assert set(health) == HEALTH_FIELDS
    for key in ("restricted", "removed_upstream", "drift_files", "not_playable"):
        assert health[key] == summary[key]
    assert health["removed_upstream"] == [{"identifier": "gd-gone", "removed_on": "2026-11-01"}]


def test_a_show_kept_as_an_unplayable_original_and_a_playable_copy_is_listed(
    tmp_roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(*tmp_roots)
    _show("gd-shn-mp3", ("t01.shn", b"lossless", "Shorten"), ("t01.mp3", b"mp3", "VBR MP3"))
    _show("gd-flac", ("t01.flac", b"flac", "Flac"), ("t01.mp3", b"mp3", "VBR MP3"))
    _show("gd-shn-only", ("t01.shn", b"only", "Shorten"))
    assert run_sync(config) == 0

    [health] = _attention(config)["health"]

    assert health["playable_copies"] == ["gd-shn-mp3"]
    assert health["not_playable"] == _summary(_status(config, capsys))["not_playable"]
    assert [r["identifier"] for r in health["not_playable"]] == ["gd-shn-only"]


# --- serving ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_answer_is_shared_until_its_interval_passes(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    clock = _Clock()
    app = _app(config, clock)
    first = await _get(app)
    assert (await _get(app))["generated_at"] == first["generated_at"]

    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        ItemRepo(conn).upsert(Item(SRC, "coll1", "gd-x", ItemStatus.FAILED))
        conn.commit()
    assert (await _get(app))["entries"] == []  # still the shared answer
    clock.now += VIEW_TTL_S
    assert _kinds(await _get(app)) == [("coll1", "failed_shows")]


@pytest.mark.asyncio
async def test_answering_never_writes(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)
    _show("gd-a")
    assert run_sync(config) == 0
    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        RunRepo(conn).start(SRC, "coll1", datetime.now() - timedelta(hours=1))
        conn.commit()
        before = list(conn.iterdump())

    await _get(_app(config))

    with closing(connect(config.state_db)) as conn:  # type: ignore[arg-type]
        assert list(conn.iterdump()) == before


@pytest.mark.asyncio
async def test_only_get_is_served(tmp_roots: tuple[Path, Path]) -> None:
    app = _app(make_config(*tmp_roots))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.post("/api/attention")).status_code == 405
