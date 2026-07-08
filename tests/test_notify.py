"""Library handoff + failure notifications (SPEC §spec:handoff).

Covers the versioned once-per-(collection, run) ``sync.complete`` batch payload,
the ``sync.failed`` notification, template-field expansion, and the best-effort
delivery contract (a down listener is recorded in run errors but never fails the
sync).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx

from shakedown.config import (
    ExecHandoff,
    FailureNotification,
    NotificationsConfig,
    WebhookHandoff,
)
from shakedown.db import connect
from shakedown.notify import (
    PAYLOAD_VERSION,
    SyncCompletePayload,
    SyncFailedPayload,
    fire_complete,
    fire_failure,
)
from shakedown.state import RunRepo
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _complete(**overrides) -> SyncCompletePayload:
    base = {
        "source": "fake-src",
        "collection": "coll1",
        "staging_root": "/data/library/fake-src/coll1",
        "run": {
            "started_at": "2026-07-08T00:00:00",
            "finished_at": "2026-07-08T00:01:00",
            "items_new": 1,
            "items_updated": 0,
            "items_failed": 0,
            "bytes_downloaded": 5,
        },
        "staged": [
            {
                "identifier": "gd-x",
                "archive_path": "/data/archive/fake-src/coll1/gd-x",
                "staging_path": "/data/library/fake-src/coll1/gd-x",
            }
        ],
    }
    base.update(overrides)
    return SyncCompletePayload(**base)


def _make_collection(handoff):
    cfg = make_config(Path("/tmp/a"), Path("/tmp/b"))
    cfg.sources[0].collections[0].on_complete = handoff
    return cfg.sources[0].collections[0]


# --- sync.complete batch payload -------------------------------------------


def test_complete_webhook_posts_versioned_batch_body() -> None:
    """Body must carry the documented versioned batch contract."""
    coll = _make_collection(WebhookHandoff(webhook="http://beets:8337/import"))
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200)

    with patch("shakedown.notify.httpx.post", side_effect=fake_post):
        assert fire_complete(coll, _complete()) is None

    assert captured["url"] == "http://beets:8337/import"
    body = captured["json"]
    assert body["payload_version"] == PAYLOAD_VERSION == 1
    assert body["event"] == "sync.complete"
    assert body["source"] == "fake-src"
    assert body["collection"] == "coll1"
    assert body["staging_root"] == "/data/library/fake-src/coll1"
    assert body["run"]["items_new"] == 1
    assert isinstance(body["staged"], list)
    assert body["staged"][0]["identifier"] == "gd-x"


def test_complete_webhook_template_expansion() -> None:
    """The URL may reference {source}/{collection}/{staging_root} template fields."""
    coll = _make_collection(
        WebhookHandoff(webhook="http://beets:8337/import?src={source}&path={staging_root}")
    )
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        return httpx.Response(200)

    with patch("shakedown.notify.httpx.post", side_effect=fake_post):
        fire_complete(coll, _complete())

    assert "src=fake-src" in captured["url"]
    assert "path=/data/library/fake-src/coll1" in captured["url"]


def test_complete_webhook_network_failure_returns_error() -> None:
    """A broken webhook must not raise, and must report the delivery error."""
    coll = _make_collection(WebhookHandoff(webhook="http://nope.example/import"))

    def boom(*args, **kwargs):
        raise httpx.ConnectError("nobody home")

    with patch("shakedown.notify.httpx.post", side_effect=boom):
        err = fire_complete(coll, _complete())  # must not raise

    assert err is not None
    assert "failed" in err


def test_complete_webhook_4xx_returns_error() -> None:
    coll = _make_collection(WebhookHandoff(webhook="http://beets:8337/import"))
    with patch("shakedown.notify.httpx.post", return_value=httpx.Response(500)):
        err = fire_complete(coll, _complete())
    assert err is not None
    assert "500" in err


def test_complete_exec_runs_command_with_payload_on_stdin() -> None:
    coll = _make_collection(ExecHandoff(exec="/usr/local/bin/import {staging_root}"))
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["input"] = kwargs.get("input")
        from subprocess import CompletedProcess

        return CompletedProcess(args, 0)

    with patch("shakedown.notify.subprocess.run", side_effect=fake_run):
        assert fire_complete(coll, _complete()) is None

    assert captured["args"] == ["/usr/local/bin/import", "/data/library/fake-src/coll1"]
    body = json.loads(captured["input"])
    assert body["event"] == "sync.complete"
    assert body["staged"][0]["identifier"] == "gd-x"


def test_no_handoff_is_a_noop() -> None:
    coll = _make_collection(None)
    assert fire_complete(coll, _complete()) is None


def test_complete_exec_unparseable_command_returns_error() -> None:
    """An unbalanced-quote exec command must be reported, never raised."""
    coll = _make_collection(ExecHandoff(exec='beet import "unterminated'))
    err = fire_complete(coll, _complete())  # shlex.split would raise ValueError
    assert err is not None


def test_complete_exec_empty_command_returns_error() -> None:
    """A command that expands to nothing must be reported, never raised."""
    coll = _make_collection(ExecHandoff(exec="   "))
    err = fire_complete(coll, _complete())  # subprocess.run([]) would raise IndexError
    assert err is not None


def test_complete_webhook_invalid_url_returns_error() -> None:
    """A malformed URL (control char) raises httpx.InvalidURL, not HTTPError — the
    best-effort net must still turn it into a returned error, never a raise."""
    coll = _make_collection(WebhookHandoff(webhook="http://beets:8337/\x00import"))
    err = fire_complete(coll, _complete())
    assert err is not None


# --- sync.failed notification -----------------------------------------------


def test_failure_webhook_posts_errors() -> None:
    notifications = NotificationsConfig(
        on_failure=FailureNotification(webhook="http://ntfy/shakedown?src={source}")
    )
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200)

    payload = SyncFailedPayload(
        source="fake-src",
        collection="coll1",
        staging_root="/data/library/fake-src/coll1",
        run={"started_at": "t0", "finished_at": "t1", "items_new": 0,
             "items_updated": 0, "items_failed": 1, "bytes_downloaded": 0},
        errors=["source enumeration failed: boom"],
    )
    with patch("shakedown.notify.httpx.post", side_effect=fake_post):
        assert fire_failure(notifications, payload) is None

    assert "src=fake-src" in captured["url"]
    body = captured["json"]
    assert body["payload_version"] == 1
    assert body["event"] == "sync.failed"
    assert body["errors"] == ["source enumeration failed: boom"]


def test_failure_noop_when_unconfigured() -> None:
    payload = SyncFailedPayload(
        source="s", collection="c", staging_root="/l", run={}, errors=["x"]
    )
    assert fire_failure(None, payload) is None
    assert fire_failure(NotificationsConfig(on_failure=None), payload) is None
    assert fire_failure(
        NotificationsConfig(on_failure=FailureNotification(webhook=None)), payload
    ) is None


# --- vertical slice: sync fires notifications through the CLI path ----------


def test_sync_fires_one_complete_per_run(tmp_roots: tuple[Path, Path]) -> None:
    """Exactly one POST per (collection, run), carrying the batch contract."""
    archive, library = tmp_roots
    config = make_config(
        archive, library,
        on_complete=WebhookHandoff(webhook="http://beets:8337/import?path={staging_root}"),
    )
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"audio")]
    )
    bodies: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        bodies.append(json)
        return httpx.Response(200)

    with patch("shakedown.notify.httpx.post", side_effect=fake_post):
        assert run_sync(config) == 0

    assert len(bodies) == 1
    body = bodies[0]
    assert body["payload_version"] == 1
    assert body["event"] == "sync.complete"
    assert body["run"]["items_new"] == 1
    assert len(body["staged"]) == 1
    assert body["staged"][0]["identifier"] == "gd-x"

    # Second sync (idempotent) stages nothing new — the handoff must not re-fire.
    with patch("shakedown.notify.httpx.post", side_effect=fake_post):
        assert run_sync(config) == 0
    assert len(bodies) == 1


def test_sync_fires_failure_on_unreachable_source(tmp_roots: tuple[Path, Path]) -> None:
    """A failed run (unreachable source) fires sync.failed with errors."""
    archive, library = tmp_roots
    config = make_config(archive, library)
    config.notifications = NotificationsConfig(
        on_failure=FailureNotification(webhook="http://ntfy/shakedown")
    )
    bodies: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        bodies.append(json)
        return httpx.Response(200)

    with (
        patch.object(FakePlugin, "discover", side_effect=RuntimeError("source unreachable")),
        patch("shakedown.notify.httpx.post", side_effect=fake_post),
    ):
        assert run_sync(config) == 1  # a failed run exits non-zero

    assert len(bodies) == 1
    body = bodies[0]
    assert body["event"] == "sync.failed"
    assert body["errors"]
    assert any("source enumeration failed" in e for e in body["errors"])


def test_down_listener_recorded_in_run_errors_and_does_not_fail_sync(
    tmp_roots: tuple[Path, Path],
) -> None:
    """A down handoff listener is logged in run errors (visible in status) but
    never fails the sync (SPEC §spec:failure-behavior)."""
    archive, library = tmp_roots
    config = make_config(
        archive, library, on_complete=WebhookHandoff(webhook="http://beets:8337/import")
    )
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"audio")]
    )

    def boom(*args, **kwargs):
        raise httpx.ConnectError("nobody home")

    with patch("shakedown.notify.httpx.post", side_effect=boom):
        assert run_sync(config) == 0  # delivery failure never fails the sync

    conn = connect(config.state_db)  # type: ignore[arg-type]
    run = RunRepo(conn).latest("fake-src", "coll1")
    assert run is not None
    assert any("failed" in e for e in run.errors)
