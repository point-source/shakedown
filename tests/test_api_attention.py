"""`GET /api/attention` (§spec:needs-attention): one entry per collection per kind of
thing that needs the operator, and per-collection library health, each from the same
computation `status` uses."""
from __future__ import annotations

import asyncio
import json
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

pytest.importorskip("fastapi")

from shakedown.config import CollectionConfig, Config, SourceConfig, WebhookHandoff
from shakedown.dashboard import VIEW_TTL_S
from shakedown.db import connect
from shakedown.lock import writer_lock
from shakedown.models import Item, ItemStatus, OperationStatus, OperationType
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
    entry = _entry(answer, "failed_shows")
    assert set(entry) == ENTRY_FIELDS
    assert entry == {
        "kind": "failed_shows", "source": SRC, "collection": "coll1", "since": None,
        "identifiers": ["gd-bad"], "detail": {},
    }
    assert len(entry["identifiers"]) == _summary(_status(config, capsys))["counts"]["failed"]

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
