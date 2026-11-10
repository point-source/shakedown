"""Durable handoff (SPEC §spec:durable-handoff, §req:success-criteria #21).

A notice is either delivered or its shows stay pending and are resent, merged into
the collection's next notice, at their current library paths. An undelivered notice
never changes the run's outcome or exit code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from shakedown.config import (
    Config,
    FailureNotification,
    NotificationsConfig,
    WebhookHandoff,
)
from shakedown.db import connect
from shakedown.state import ItemRepo, PendingHandoffRepo, RunRepo
from shakedown.sync import run_sync, send_collection_handoff
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin

GOOD = "http://receiver.example/hook"
DOWN = "http://down.example/hook"


class _Webhooks:
    """Stands in for httpx.post: URLs in `down` cannot be reached; others answer 200."""

    def __init__(self, down: set[str] | None = None) -> None:
        self.down = down if down is not None else {DOWN}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, json: dict[str, Any] | None = None, timeout: Any = None):
        assert json is not None
        self.calls.append((url, json))
        if url in self.down:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200)

    def bodies(self, event: str = "sync.complete") -> list[dict[str, Any]]:
        return [body for _, body in self.calls if body["event"] == event]


def _add_show(identifier: str) -> None:
    FakePlugin.items[identifier] = FakeItem(
        identifier=identifier, files=[FakeFile(name=f"{identifier}.flac", content=b"audio")]
    )


def _config(archive: Path, library: Path, url: str | None, **kwargs: Any) -> Config:
    handoff = WebhookHandoff(webhook=url) if url else None
    return make_config(archive, library, on_complete=handoff, **kwargs)


def _sync(config: Config, hooks: _Webhooks, **kwargs: Any) -> int:
    with patch("shakedown.notify.httpx.post", side_effect=hooks):
        return run_sync(config, **kwargs)


def _pending(config: Config) -> list[str]:
    conn = connect(config.state_db)  # type: ignore[arg-type]
    return [p.identifier for p in PendingHandoffRepo(conn).list("fake-src", "coll1")]


def _staged_ids(body: dict[str, Any]) -> list[str]:
    return sorted(entry["identifier"] for entry in body["staged"])


def test_undelivered_notice_keeps_shows_pending_and_the_run_succeeds(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = _config(archive, library, DOWN)
    _add_show("gd-a")
    _add_show("gd-b")

    assert _sync(config, _Webhooks()) == 0  # undelivered never fails the run

    assert _pending(config) == ["gd-a", "gd-b"]
    conn = connect(config.state_db)  # type: ignore[arg-type]
    pending = PendingHandoffRepo(conn).list("fake-src", "coll1")
    assert {p.last_reason for p in pending} == {"webhook unreachable: connection refused"}
    run = RunRepo(conn).latest("fake-src", "coll1")
    assert run is not None and run.items_failed == 0


def test_fixed_target_receives_pending_shows_on_a_run_with_nothing_new(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    _add_show("gd-a")
    _add_show("gd-b")
    assert _sync(_config(archive, library, DOWN), _Webhooks()) == 0

    config = _config(archive, library, GOOD)  # operator fixes the target
    hooks = _Webhooks()
    assert _sync(config, hooks) == 0

    bodies = hooks.bodies()
    assert len(bodies) == 1  # one notice, not one per failed run
    body = bodies[0]
    assert body["payload_version"] == 2
    assert body["run"]["items_new"] == 0
    assert _staged_ids(body) == ["gd-a", "gd-b"]
    for entry in body["staged"]:
        assert entry["staging_path"] == str(library / "fake-src" / "coll1" / entry["identifier"])
        assert entry["archive_path"] == str(archive / "fake-src" / "coll1" / entry["identifier"])
    assert _pending(config) == []

    # Delivered: the next run has nothing to send.
    assert _sync(config, hooks) == 0
    assert len(hooks.bodies()) == 1


def test_new_and_pending_shows_go_out_in_one_notice(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    _add_show("gd-a")
    assert _sync(_config(archive, library, DOWN), _Webhooks()) == 0

    _add_show("gd-b")
    hooks = _Webhooks()
    assert _sync(_config(archive, library, GOOD), hooks) == 0

    (body,) = hooks.bodies()
    assert _staged_ids(body) == ["gd-a", "gd-b"]


def test_a_second_failure_keeps_the_first_time_and_adds_new_shows(
    tmp_roots: tuple[Path, Path],
) -> None:
    archive, library = tmp_roots
    config = _config(archive, library, DOWN)
    _add_show("gd-a")
    assert _sync(config, _Webhooks()) == 0
    conn = connect(config.state_db)  # type: ignore[arg-type]
    (first,) = PendingHandoffRepo(conn).list("fake-src", "coll1")

    _add_show("gd-b")
    hooks = _Webhooks()
    assert _sync(config, hooks) == 0

    (body,) = hooks.bodies()
    assert _staged_ids(body) == ["gd-a", "gd-b"]  # the pending show was resent
    pending = {p.identifier: p for p in PendingHandoffRepo(conn).list("fake-src", "coll1")}
    assert set(pending) == {"gd-a", "gd-b"}
    assert pending["gd-a"].since == first.since


def test_resent_show_carries_its_current_library_path(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    _add_show("gd-a")
    assert _sync(_config(archive, library, DOWN), _Webhooks()) == 0

    # The layout changes before the notice is delivered: the show moves.
    config = _config(archive, library, GOOD, library_layout="shows/{identifier}")
    hooks = _Webhooks()
    assert _sync(config, hooks) == 0

    (body,) = hooks.bodies()
    assert body["staged"][0]["staging_path"] == str(
        library / "fake-src" / "coll1" / "shows" / "gd-a"
    )


def test_pruned_show_leaves_the_pending_list(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    _add_show("gd-a")
    _add_show("gd-b")
    assert _sync(_config(archive, library, DOWN, prune_disappeared=True), _Webhooks()) == 0

    del FakePlugin.items["gd-a"]  # taken down upstream, then pruned
    config = _config(archive, library, GOOD, prune_disappeared=True)
    hooks = _Webhooks()
    assert _sync(config, hooks) == 0

    (body,) = hooks.bodies()
    assert _staged_ids(body) == ["gd-b"]
    assert _pending(config) == []


def test_removing_the_handoff_drops_the_pending_list(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    _add_show("gd-a")
    assert _sync(_config(archive, library, DOWN), _Webhooks()) == 0

    config = _config(archive, library, None)
    assert _sync(config, _Webhooks()) == 0
    assert _pending(config) == []

    # Adding a handoff back later starts from an empty list.
    hooks = _Webhooks()
    assert _sync(_config(archive, library, GOOD), hooks) == 0
    assert hooks.bodies() == []


def test_changed_target_receives_the_pending_shows(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    _add_show("gd-a")
    assert _sync(_config(archive, library, DOWN), _Webhooks()) == 0

    hooks = _Webhooks()
    assert _sync(_config(archive, library, GOOD), hooks) == 0

    assert [url for url, _ in hooks.calls] == [GOOD]


def test_stale_run_still_resends_pending_shows(tmp_roots: tuple[Path, Path]) -> None:
    """Every run of the collection resends, even one whose enumeration failed."""
    archive, library = tmp_roots
    _add_show("gd-a")
    assert _sync(_config(archive, library, DOWN), _Webhooks()) == 0

    config = _config(archive, library, GOOD)
    hooks = _Webhooks()
    with patch.object(FakePlugin, "enumerate_items", side_effect=RuntimeError("down")):
        assert _sync(config, hooks) == 1  # stale: fails for its own reason

    (body,) = hooks.bodies()
    assert _staged_ids(body) == ["gd-a"]
    assert _pending(config) == []


def test_dry_run_sends_nothing(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    _add_show("gd-a")
    assert _sync(_config(archive, library, DOWN), _Webhooks()) == 0

    config = _config(archive, library, GOOD)
    hooks = _Webhooks()
    assert _sync(config, hooks, dry_run=True) == 0

    assert hooks.calls == []
    assert _pending(config) == ["gd-a"]


def test_undelivered_failure_notice_is_not_queued(tmp_roots: tuple[Path, Path]) -> None:
    archive, library = tmp_roots
    config = _config(archive, library, GOOD)
    config.notifications = NotificationsConfig(on_failure=FailureNotification(webhook=DOWN))
    _add_show("gd-a")
    hooks = _Webhooks()
    assert _sync(config, hooks) == 0  # delivers gd-a's notice

    with patch.object(FakePlugin, "enumerate_items", side_effect=RuntimeError("down")):
        assert _sync(config, hooks) == 1

    assert _pending(config) == []
    conn = connect(config.state_db)  # type: ignore[arg-type]
    run = RunRepo(conn).latest("fake-src", "coll1")
    assert run is not None
    assert "failure notice not delivered: webhook unreachable: connection refused" in run.errors

    # The next run does not resend the failure alert.
    assert _sync(config, hooks) == 0
    assert len(hooks.bodies("sync.failed")) == 1


def test_pending_show_missing_from_the_library_stays_pending(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A show that still has its archive copy but no library folder this pass (its
    path was refused, or the layout failed to render) is kept for a later notice."""
    archive, library = tmp_roots
    config = _config(archive, library, DOWN)
    _add_show("gd-a")
    assert _sync(config, _Webhooks()) == 0

    config.sources[0].collections[0].on_complete = WebhookHandoff(webhook=GOOD)
    conn = connect(config.state_db)  # type: ignore[arg-type]
    hooks = _Webhooks()
    with patch("shakedown.notify.httpx.post", side_effect=hooks):
        reason = send_collection_handoff(
            config, ItemRepo(conn), "fake-src", config.sources[0].collections[0],
            staged=[], folder_of={}, run={},
        )

    assert reason is None
    assert hooks.calls == []
    assert _pending(config) == ["gd-a"]
