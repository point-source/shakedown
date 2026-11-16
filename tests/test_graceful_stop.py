"""Stopping a writing run on request (§spec:graceful-stop).

The first SIGTERM or SIGINT makes a writing command start no new work, abandon
downloads at their next chunk, record each run it cut off as `stopped`, release the
writer lock, and exit 143 (SIGTERM) or 130 (SIGINT). The next run continues and ends
where an uninterrupted run would have.

In-process tests ask for the stop through `stopping.request`, the handler's own
first step, from inside a fake download (as a real plugin's chunk loop sees it).
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from shakedown import stopping
from shakedown.config import Config, WebhookHandoff
from shakedown.db import connect
from shakedown.lock import run_exclusive
from shakedown.models import ItemStatus, OperationStatus, OperationType
from shakedown.state import (
    ItemRepo,
    OperationOutcomeRepo,
    PendingHandoffRepo,
    RescheduledRepo,
    RunRepo,
)
from shakedown.status import collection_summaries
from shakedown.sync import run_sync
from shakedown.trigger import READY_FD_ENV
from tests.conftest import make_config, write_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin
from tests.serve_child import SPEC_ENV

REPO_ROOT = Path(__file__).resolve().parent.parent
SHOWS = ("gd-a", "gd-b", "gd-c")


@pytest.fixture(autouse=True)
def _clear_stop() -> Iterator[None]:
    stopping.reset()
    yield
    stopping.reset()


def _add_shows(*identifiers: str) -> None:
    for identifier in identifiers:
        FakePlugin.items[identifier] = FakeItem(
            identifier=identifier,
            files=[
                FakeFile(name="t1.flac", content=f"{identifier}-one".encode()),
                FakeFile(name="t2.flac", content=f"{identifier}-two".encode()),
            ],
            metadata={"title": identifier},
        )


def _config(tmp_roots: tuple[Path, Path], **kwargs: Any) -> Config:
    config = make_config(*tmp_roots, **kwargs)
    # One download at a time, so "the second download" is a fixed point in the run.
    config.max_concurrent_downloads = 1
    return config


def _stop_on_fetch(monkeypatch: pytest.MonkeyPatch, nth: int, signum: int = signal.SIGTERM) -> None:
    """The ``nth`` download writes one file and half of the next, then sees the stop
    at its next chunk, as a real plugin's download loop does."""
    real = FakePlugin.fetch
    calls = {"n": 0}

    def fetch(self, identifier, files, dest_dir):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] != nth:
            return real(self, identifier, files, dest_dir)
        content = {f.name: f.content for f in self.items[identifier].files}
        dest_dir.mkdir(parents=True, exist_ok=True)
        first, second = files[0], files[1]
        (dest_dir / first.name).write_bytes(content[first.name])
        (dest_dir / second.name).write_bytes(content[second.name][:3])  # torn
        stopping.request(signum)
        stopping.check()
        raise AssertionError("unreachable")

    monkeypatch.setattr(FakePlugin, "fetch", fetch)


def _db(config: Config) -> sqlite3.Connection:
    return connect(config.state_db)  # type: ignore[arg-type]


def _items(config: Config) -> dict[str, str]:
    return {
        item.identifier: item.status.value
        for item in ItemRepo(_db(config)).list_for_collection("fake-src", "coll1")
    }


def _hidden_dirs(config: Config) -> list[str]:
    root = config.archive_root / "fake-src" / "coll1"
    return sorted(p.name for p in root.glob(".*")) if root.is_dir() else []


def _library(config: Config) -> list[str]:
    root = config.library_root
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _activity_results(config: Config) -> list[tuple[str, str | None]]:
    conn = _db(config)
    return [
        (row["state"], row["result"])
        for row in conn.execute("SELECT state, result FROM run_activity ORDER BY id")
    ]


# --- the handler -------------------------------------------------------------------


def test_a_writing_command_handles_sigterm_and_sigint_while_it_runs(
    tmp_roots: tuple[Path, Path],
) -> None:
    config = make_config(*tmp_roots)
    seen: dict[int, Any] = {}

    def command() -> int:
        seen.update({s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)})
        return 0

    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    assert run_exclusive(config, command) == 0
    assert seen[signal.SIGTERM] not in (signal.SIG_DFL, signal.SIG_IGN, before[signal.SIGTERM])
    assert seen[signal.SIGINT] is seen[signal.SIGTERM]
    # The previous handlers are back once the command ends.
    assert {s: signal.getsignal(s) for s in before} == before


@pytest.mark.parametrize(
    ("module", "entry", "inner", "args"),
    [
        ("shakedown.sync", "run_sync", "_run_sync", {}),
        ("shakedown.sync", "refetch_item", "_refetch_item", {"identifier": "gd-a"}),
        ("shakedown.restage", "run_restage", "_run_restage", {}),
        ("shakedown.reconcile", "run_reconcile", "_run_reconcile", {}),
        (
            "shakedown.verify", "run_verify", "_run_verify",
            {"source_filter": None, "collection_filter": None, "deep": True,
             "reconform": True, "list_drift": False, "assume_yes": True},
        ),
    ],
)
def test_every_writing_command_installs_the_handler(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    module: str, entry: str, inner: str, args: dict[str, Any],
) -> None:
    import importlib

    mod = importlib.import_module(module)
    seen: list[Any] = []
    monkeypatch.setattr(mod, inner, lambda *a, **k: seen.append(signal.getsignal(signal.SIGTERM)) or 0)
    getattr(mod, entry)(make_config(*tmp_roots), **args)
    assert seen and seen[0] not in (signal.SIG_DFL, signal.SIG_IGN)


def test_a_stopped_command_exits_with_the_signal_code(tmp_roots: tuple[Path, Path]) -> None:
    config = make_config(*tmp_roots)

    def stop(signum: int) -> int:
        stopping.request(signum)
        return 0

    assert run_exclusive(config, lambda: stop(signal.SIGTERM)) == 143
    assert run_exclusive(config, lambda: stop(signal.SIGINT)) == 130


# --- a stopped sync ----------------------------------------------------------------


def test_a_stopped_sync_records_what_it_finished_and_continues_next_run(
    tmp_roots: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_shows(*SHOWS)
    config = _config(tmp_roots)
    _stop_on_fetch(monkeypatch, nth=2)

    assert run_sync(config) == 143

    items = _items(config)
    # One show finished before the stop and is recorded; the torn one is not, and
    # the third was never started.
    assert list(items.values()) == [ItemStatus.COMPLETE.value]
    torn = next(i for i in SHOWS if i not in items and (
        config.archive_root / "fake-src" / "coll1" / f".tmp-{i}").is_dir())
    # The torn download keeps its temp folder, so the next run fetches only the rest.
    assert _hidden_dirs(config) == [f".tmp-{torn}"]
    assert not (config.archive_root / "fake-src" / "coll1" / torn).exists()

    conn = _db(config)
    run = RunRepo(conn).latest("fake-src", "coll1")
    assert run is not None and run.finished_at is not None
    outcome = OperationOutcomeRepo(conn).latest(OperationType.SYNC, "fake-src", "coll1")
    assert outcome is not None and outcome.status == OperationStatus.STOPPED
    assert _activity_results(config) == [("ended", "stopped")]

    # `status`: the last run says it was stopped; no recovery line for a deliberate stop.
    (summary,) = collection_summaries(config, conn)
    assert summary["recovery"] is None
    assert summary["last_run"]["result"] == "stopped"

    # The next sync continues and ends exactly where an uninterrupted one does.
    monkeypatch.undo()
    stopping.reset()
    assert run_sync(config) == 0
    assert set(_items(config).values()) == {ItemStatus.COMPLETE.value}
    assert _hidden_dirs(config) == []
    # The torn show reused its finished file: only the torn one was fetched again.
    assert FakePlugin.fetched_files[torn][-1:] == ["t2.flac"]

    fresh = _config((tmp_path / "a2", tmp_path / "l2"))
    fresh.archive_root.mkdir()
    fresh.library_root.mkdir()
    assert run_sync(fresh) == 0
    assert _items(fresh) == _items(config)
    assert _library(fresh) == _library(config)


def test_sigint_stop_exits_130(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_shows(*SHOWS)
    config = _config(tmp_roots)
    _stop_on_fetch(monkeypatch, nth=1, signum=signal.SIGINT)
    assert run_sync(config) == 130


def test_a_stop_during_lookups_starts_no_download(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_shows(*SHOWS)
    config = _config(tmp_roots)
    real = FakePlugin.lookup_item

    def lookup_item(self, identifier, timeout):  # type: ignore[no-untyped-def]
        stopping.request()
        return real(self, identifier, timeout)

    monkeypatch.setattr(FakePlugin, "lookup_item", lookup_item)
    assert run_sync(config) == 143
    assert FakePlugin.fetch_count == {}
    assert _items(config) == {}
    assert _activity_results(config) == [("ended", "stopped")]


def test_a_stopped_sync_prunes_nothing(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_shows("gd-old")
    config = _config(tmp_roots, prune_disappeared=True)
    assert run_sync(config) == 0
    del FakePlugin.items["gd-old"]
    _add_shows("gd-new")
    _stop_on_fetch(monkeypatch, nth=1)
    assert run_sync(config) == 143
    assert _items(config) == {"gd-old": ItemStatus.COMPLETE.value}
    assert (config.archive_root / "fake-src" / "coll1" / "gd-old").is_dir()


def test_a_stopped_sync_keeps_its_rescheduled_entry_and_catches_nothing_up(
    tmp_roots: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_shows(*SHOWS)
    config = _config(tmp_roots)
    config_path = write_config(tmp_path / "cfg.yaml", config)
    since = datetime(2026, 11, 1, 3, 0)
    conn = _db(config)
    RescheduledRepo(conn).record("fake-src", "coll1", since)
    conn.commit()
    _stop_on_fetch(monkeypatch, nth=1)

    assert run_sync(config, config_path=config_path) == 143

    assert RescheduledRepo(_db(config)).get("fake-src", "coll1") == since
    # No catch-up ran after the stop: one run record only.
    rows = _db(config).execute("SELECT kind FROM runs").fetchall()
    assert [r["kind"] for r in rows] == ["sync"]


def test_a_stopped_catch_up_keeps_its_entry(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shakedown import sync

    _add_shows(*SHOWS)
    config = _config(tmp_roots)
    since = datetime(2026, 11, 1, 3, 0)
    conn = _db(config)
    RescheduledRepo(conn).record("fake-src", "coll1", since)
    conn.commit()
    _stop_on_fetch(monkeypatch, nth=1)
    scope = sync._scope(config, None, None)

    code = run_exclusive(
        config,
        lambda: sync._run_sync(
            config, scope, False, False, catch_up={("fake-src", "coll1"): since}
        ),
    )

    assert code == 143
    assert RescheduledRepo(_db(config)).get("fake-src", "coll1") == since
    assert _activity_results(config) == [("ended", "stopped")]


def test_a_stopped_sync_sends_no_notice_and_keeps_its_shows_for_the_next_handoff(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_shows(*SHOWS)
    config = _config(tmp_roots, on_complete=WebhookHandoff(webhook="http://x.example/hook"))
    sent: list[Any] = []
    monkeypatch.setattr("shakedown.notify.httpx.post", lambda *a, **k: sent.append(a))
    _stop_on_fetch(monkeypatch, nth=2)

    assert run_sync(config) == 143

    assert sent == []
    pending = PendingHandoffRepo(_db(config)).list("fake-src", "coll1")
    assert [p.identifier for p in pending] == list(_items(config))


def test_a_stopped_tag_pass_leaves_the_rest_for_the_next_run(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shakedown import tagging

    _add_shows(*SHOWS)
    config = _config(tmp_roots)
    assert run_sync(config) == 0
    config.sources[0].collections[0].fill_tags = True
    calls: list[Path] = []

    def fill_file(path, *a, **k):  # type: ignore[no-untyped-def]
        calls.append(path)
        stopping.request()
        return real(path, *a, **k)

    real = tagging.fill_file
    monkeypatch.setattr(tagging, "fill_file", fill_file)
    assert run_sync(config) == 143
    # The file in hand was finished; no further file was opened.
    assert len(calls) == 1
    assert _activity_results(config)[-1] == ("ended", "stopped")


# --- other writing commands --------------------------------------------------------


def test_a_stopped_restage_records_stopped(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shakedown.placement import LibraryPlacement
    from shakedown.restage import run_restage

    _add_shows(*SHOWS)
    config = _config(tmp_roots)
    assert run_sync(config) == 0
    real = LibraryPlacement._link_show
    linked: list[str] = []

    def link_show(self, item, folder):  # type: ignore[no-untyped-def]
        linked.append(item.identifier)
        stopping.request()
        return real(self, item, folder)

    monkeypatch.setattr(LibraryPlacement, "_link_show", link_show)
    assert run_restage(config) == 143
    assert len(linked) == 1  # the show in hand was linked, then the pass stopped
    assert _activity_results(config)[-1] == ("ended", "stopped")


def test_a_stopped_reconcile_writes_nothing(
    tmp_roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shakedown.reconcile import run_reconcile

    _add_shows(*SHOWS)
    config = _config(tmp_roots)
    assert run_sync(config) == 0
    before = _items(config)
    del FakePlugin.items["gd-a"]  # a complete reconcile would mark it disappeared
    real = FakePlugin.lookup_item

    def lookup_item(self, identifier, timeout):  # type: ignore[no-untyped-def]
        stopping.request()
        return real(self, identifier, timeout)

    monkeypatch.setattr(FakePlugin, "lookup_item", lookup_item)
    assert run_reconcile(config) == 143
    assert _items(config) == before
    assert _activity_results(config)[-1] == ("ended", "stopped")


# --- real signals, in a separate process -------------------------------------------


def _spawn(tmp_path: Path, config: Config, args: list[str]) -> subprocess.Popen[bytes]:
    """A fake-source sync whose downloads wait at a gate that never opens, started and
    holding the lock once this returns."""
    write_config(tmp_path / "cfg.yaml", config)
    spec = tmp_path / "fake-spec.json"
    spec.write_text(json.dumps({
        "items": {i: {"t1.flac": f"{i}-1", "t2.flac": f"{i}-2"} for i in SHOWS},
        "gate": str(tmp_path / "never"),
    }))
    r, w = os.pipe()
    env = {**os.environ, READY_FD_ENV: str(w), SPEC_ENV: str(spec)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.serve_child", "--config", str(tmp_path / "cfg.yaml"),
         *args],
        pass_fds=(w,), env=env, cwd=REPO_ROOT, stderr=subprocess.PIPE,
    )
    os.close(w)
    with os.fdopen(r, "rb") as reader:
        assert json.loads(reader.readline())["state"] == "started"
    return proc


def _wait_for_download(config: Config) -> None:
    root = config.archive_root / "fake-src" / "coll1"
    deadline = time.monotonic() + 30
    while not any(root.glob(".tmp-*")):
        assert time.monotonic() < deadline, "no download started"
        time.sleep(0.05)


@pytest.mark.parametrize(("signum", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_a_signal_stops_a_running_sync_promptly(
    tmp_roots: tuple[Path, Path], tmp_path: Path, signum: int, code: int,
) -> None:
    config = _config(tmp_roots)
    proc = _spawn(tmp_path, config, ["sync"])
    _wait_for_download(config)
    sent = time.monotonic()
    proc.send_signal(signum)
    assert proc.wait(timeout=20) == code
    assert time.monotonic() - sent < 10  # within Docker's default stop timeout
    assert _activity_results(config) == [("ended", "stopped")]
    run = RunRepo(_db(config)).latest("fake-src", "coll1")
    assert run is not None and run.finished_at is not None


def test_a_second_signal_exits_at_once(
    tmp_roots: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_roots)
    # The download in hand ignores the stop, so only the second signal can end it.
    monkeypatch.setenv("SHAKEDOWN_FAKE_IGNORE_STOP", "1")
    proc = _spawn(tmp_path, config, ["sync"])
    _wait_for_download(config)
    proc.send_signal(signal.SIGTERM)
    time.sleep(0.5)
    assert proc.poll() is None  # still finishing the download in hand
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=10) == 143
