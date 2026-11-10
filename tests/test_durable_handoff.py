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


# --- the shipped recipe works when followed literally -------------------------

REPO = Path(__file__).resolve().parent.parent


def test_example_config_ships_no_active_handoff() -> None:
    """The listening path needs no handoff; every example target is a placeholder
    (commented out), never a receiver missing from the example stack."""
    from shakedown.config import load

    config = load(REPO / "shakedown.example.yaml")
    assert all(c.on_complete is None for s in config.sources for c in s.collections)
    assert config.notifications is None or config.notifications.on_failure is None
    assert "beet" not in (REPO / "shakedown.example.yaml").read_text().lower()


def test_example_compose_runs_navidrome_on_the_library_tree() -> None:
    import yaml

    compose = yaml.safe_load((REPO / "docker-compose.example.yaml").read_text())
    navidrome = compose["services"]["navidrome"]
    assert "/share/data/library:/music:ro" in navidrome["volumes"]
    assert "beets" not in compose["services"]


# --- status: text, --json, GET /status ---------------------------------------


def _status(cfg: Path, *args: str) -> str:
    from click.testing import CliRunner

    from shakedown.cli import main

    result = CliRunner().invoke(main, ["--config", str(cfg), "status", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_status_shows_an_undelivered_handoff_until_a_later_run_delivers_it(
    tmp_path: Path, tmp_roots: tuple[Path, Path],
) -> None:
    """§spec:durable-handoff user-level path, against a real receiver: an unreachable
    target leaves the notice pending in status with its reason and exit 0; fixing the
    target and syncing with nothing new delivers one notice and clears status."""
    import json as jsonlib

    from tests.conftest import write_config
    from tests.test_notify import _Receiver

    archive, library = tmp_roots
    closed = _Receiver()
    unreachable = closed.url
    closed.close()  # nothing listens there any more
    _add_show("gd-a")
    _add_show("gd-b")
    config = _config(archive, library, unreachable)
    cfg = write_config(tmp_path / "shakedown.yaml", config)
    assert run_sync(config) == 0

    conn = connect(config.state_db)  # type: ignore[arg-type]
    since = min(p.since for p in PendingHandoffRepo(conn).list("fake-src", "coll1"))
    text = _status(cfg)
    assert f"  handoff not delivered: 2 shows since {since.isoformat()}\n" in text
    assert "    last reason: webhook unreachable: " in text
    assert unreachable not in text  # the target is in the config, not in status

    summary = jsonlib.loads(_status(cfg, "--json"))[0]
    pending = summary["handoff_pending"]
    assert pending["shows"] == 2
    assert pending["since"] == since.isoformat()
    assert pending["last_reason"].startswith("webhook unreachable: ")
    assert pending["identifiers"] == ["gd-a", "gd-b"]

    receiver = _Receiver()
    try:
        config = _config(archive, library, receiver.url)
        cfg = write_config(tmp_path / "shakedown.yaml", config)
        assert run_sync(config) == 0
        assert len(receiver.bodies) == 1
        assert receiver.bodies[0]["payload_version"] == 2
        assert _staged_ids(receiver.bodies[0]) == ["gd-a", "gd-b"]
    finally:
        receiver.close()

    assert "handoff not delivered" not in _status(cfg)
    assert jsonlib.loads(_status(cfg, "--json"))[0]["handoff_pending"] is None


def test_status_counts_one_show_in_the_singular(
    tmp_path: Path, tmp_roots: tuple[Path, Path],
) -> None:
    from tests.conftest import write_config

    archive, library = tmp_roots
    _add_show("gd-a")
    config = _config(archive, library, DOWN)
    assert _sync(config, _Webhooks()) == 0

    text = _status(write_config(tmp_path / "shakedown.yaml", config))
    assert "  handoff not delivered: 1 show since " in text


def test_status_hides_pending_shows_once_the_handoff_is_removed(
    tmp_path: Path, tmp_roots: tuple[Path, Path],
) -> None:
    """Removing on_complete drops the list: status says so before the next run."""
    import json as jsonlib

    from tests.conftest import write_config

    archive, library = tmp_roots
    _add_show("gd-a")
    assert _sync(_config(archive, library, DOWN), _Webhooks()) == 0

    cfg = write_config(tmp_path / "shakedown.yaml", _config(archive, library, None))
    assert "handoff not delivered" not in _status(cfg)
    assert jsonlib.loads(_status(cfg, "--json"))[0]["handoff_pending"] is None
