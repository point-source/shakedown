"""Handoff dispatch: webhook + exec. Resolves PRD §14 still-open."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx

from shakedown.config import ExecHandoff, WebhookHandoff
from shakedown.notify import HandoffPayload, fire
from shakedown.sync import run_sync
from tests.conftest import make_config
from tests.fake_plugin import FakeFile, FakeItem, FakePlugin


def _payload(**overrides) -> HandoffPayload:
    base = {
        "event": "item_complete",
        "source": "fake-src",
        "collection": "coll1",
        "item_identifier": "gd-x",
        "archive_path": "/data/archive/fake-src/coll1/gd-x",
        "staging_path": "/data/library/fake-src/coll1/gd-x",
        "source_metadata_excerpt": {"date": "1977-05-08", "venue": "Cornell"},
    }
    base.update(overrides)
    return HandoffPayload(**base)


def _make_collection(handoff):
    cfg = make_config(Path("/tmp/a"), Path("/tmp/b"))
    cfg.sources[0].collections[0].on_complete = handoff
    return cfg.sources[0].collections[0]


def test_webhook_posts_json_body() -> None:
    """Payload must contain the documented contract fields."""
    coll = _make_collection(WebhookHandoff(webhook="http://beets:8337/import"))
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(200)

    with patch("shakedown.notify.httpx.post", side_effect=fake_post):
        fire(coll, _payload())

    assert captured["url"] == "http://beets:8337/import"
    body = captured["json"]
    assert body["event"] == "item_complete"
    assert body["source"] == "fake-src"
    assert body["collection"] == "coll1"
    assert body["item_identifier"] == "gd-x"
    assert body["archive_path"].endswith("gd-x")
    assert body["staging_path"].endswith("gd-x")
    assert body["source_metadata_excerpt"]["venue"] == "Cornell"


def test_webhook_url_template_expansion() -> None:
    """on_complete.webhook may reference {staging_path} or other payload fields."""
    coll = _make_collection(
        WebhookHandoff(webhook="http://beets:8337/import?path={staging_path}&id={item_identifier}")
    )
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        return httpx.Response(200)

    with patch("shakedown.notify.httpx.post", side_effect=fake_post):
        fire(coll, _payload())

    assert "path=/data/library/fake-src/coll1/gd-x" in captured["url"]
    assert "id=gd-x" in captured["url"]


def test_webhook_network_failure_does_not_raise() -> None:
    """A broken webhook must not abort the sync (PRD: best-effort handoff)."""
    coll = _make_collection(WebhookHandoff(webhook="http://nope.example/import"))

    def boom(*args, **kwargs):
        raise httpx.ConnectError("nobody home")

    with patch("shakedown.notify.httpx.post", side_effect=boom):
        fire(coll, _payload())  # must not raise


def test_webhook_4xx_logged_but_does_not_raise() -> None:
    coll = _make_collection(WebhookHandoff(webhook="http://beets:8337/import"))
    with patch("shakedown.notify.httpx.post", return_value=httpx.Response(500)):
        fire(coll, _payload())  # logged warning, no raise


def test_exec_runs_command_with_payload_on_stdin() -> None:
    coll = _make_collection(ExecHandoff(exec="/usr/local/bin/notify-beets {item_identifier}"))
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["input"] = kwargs.get("input")
        from subprocess import CompletedProcess
        return CompletedProcess(args, 0)

    with patch("shakedown.notify.subprocess.run", side_effect=fake_run):
        fire(coll, _payload())

    assert captured["args"] == ["/usr/local/bin/notify-beets", "gd-x"]
    body = json.loads(captured["input"])
    assert body["item_identifier"] == "gd-x"
    assert body["staging_path"].endswith("gd-x")


def test_no_handoff_is_a_noop() -> None:
    coll = _make_collection(None)
    fire(coll, _payload())  # must not raise, must not crash on missing handoff


def test_handoff_fires_during_sync(tmp_roots: tuple[Path, Path]) -> None:
    """Sync must call the handoff exactly once per newly-completed item."""
    archive, library = tmp_roots
    config = make_config(
        archive, library,
        on_complete=WebhookHandoff(webhook="http://beets:8337/import?path={staging_path}"),
    )
    FakePlugin.items["gd-x"] = FakeItem(
        identifier="gd-x", files=[FakeFile(name="x.flac", content=b"audio")]
    )
    calls: list[str] = []

    def fake_post(url, json=None, timeout=None):
        calls.append(url)
        return httpx.Response(200)

    with patch("shakedown.notify.httpx.post", side_effect=fake_post):
        assert run_sync(config) == 0

    assert len(calls) == 1
    assert "path=" in calls[0] and "gd-x" in calls[0]

    # Second sync (idempotent) must not re-fire the handoff.
    with patch("shakedown.notify.httpx.post", side_effect=fake_post):
        assert run_sync(config) == 0
    assert len(calls) == 1
