"""Library handoff + failure notifications (SPEC §spec:handoff, §spec:durable-handoff).

Covers the versioned once-per-(collection, run) ``sync.complete`` batch payload,
the ``sync.failed`` notification, template-field expansion, and the delivery
contract: a handoff is a trigger with a bounded wait — a 2xx answer or exit 0
delivers it; anything else, or no answer in time, leaves it undelivered with a
reason, and never fails the sync.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from shakedown import notify
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
    assert body["payload_version"] == PAYLOAD_VERSION == 2
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

    assert err == "webhook unreachable: nobody home"


def test_complete_webhook_4xx_returns_error() -> None:
    coll = _make_collection(WebhookHandoff(webhook="http://beets:8337/import"))
    with patch("shakedown.notify.httpx.post", return_value=httpx.Response(500)):
        err = fire_complete(coll, _complete())
    assert err is not None
    assert "500" in err


def test_complete_exec_runs_command_with_payload_on_stdin(tmp_path: Path) -> None:
    """The command runs with template fields expanded and the JSON body on stdin;
    exit 0 delivers the notice."""
    script = tmp_path / "receiver.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps("
        "{'argv': sys.argv[2:], 'body': json.load(sys.stdin)}))\n"
    )
    out = tmp_path / "seen.json"
    coll = _make_collection(
        ExecHandoff(exec=f"{sys.executable} {script} {out} {{staging_root}}")
    )

    assert fire_complete(coll, _complete()) is None

    seen = json.loads(out.read_text())
    assert seen["argv"] == ["/data/library/fake-src/coll1"]
    assert seen["body"]["event"] == "sync.complete"
    assert seen["body"]["staged"][0]["identifier"] == "gd-x"


def test_complete_exec_nonzero_exit_reports_status_and_last_error_lines() -> None:
    """§spec:durable-handoff: for exec, the reason carries the exit status and the
    last lines of the command's error output."""
    coll = _make_collection(ExecHandoff(
        exec="sh -c 'echo one >&2; echo two >&2; echo three >&2; echo four >&2; exit 2'"
    ))

    err = fire_complete(coll, _complete())

    assert err == "command exited with status 2: two / three / four"


def test_complete_exec_error_output_is_cut_to_a_readable_length() -> None:
    coll = _make_collection(ExecHandoff(
        exec=f"{sys.executable} -c \"import sys; sys.stderr.write('x' * 5000); sys.exit(1)\""
    ))

    err = fire_complete(coll, _complete())

    assert err is not None
    assert err.startswith("command exited with status 1: xxx")
    assert len(err) <= len("command exited with status 1: ") + 300


def test_complete_exec_that_cannot_start_is_undelivered() -> None:
    coll = _make_collection(ExecHandoff(exec="/nonexistent/import-show {staging_root}"))

    err = fire_complete(coll, _complete())

    assert err is not None
    assert err.startswith("command could not start: ")


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
    assert body["payload_version"] == 2
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
        assert json is not None
        bodies.append(json)
        return httpx.Response(200)

    with patch("shakedown.notify.httpx.post", side_effect=fake_post):
        assert run_sync(config) == 0

    assert len(bodies) == 1
    body = bodies[0]
    assert body["payload_version"] == 2
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
        assert json is not None
        bodies.append(json)
        return httpx.Response(200)

    with (
        patch.object(FakePlugin, "list_items", side_effect=RuntimeError("source unreachable")),
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
    assert "handoff not delivered: webhook unreachable: nobody home" in run.errors


# --- bounded wait against a real receiver (§spec:durable-handoff) -------------


class _Receiver:
    """A local webhook receiver: answers `status` after `delay` seconds, or never
    (until the test ends) when `delay` is None."""

    def __init__(self, status: int = 200, delay: float | None = 0.0) -> None:
        self.status = status
        self.delay = delay
        self.bodies: list[dict] = []
        self.release = threading.Event()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                receiver.bodies.append(json.loads(self.rfile.read(length)))
                if receiver.delay is None:
                    receiver.release.wait(30)
                    return
                time.sleep(receiver.delay)
                self.send_response(receiver.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver() -> Iterator[_Receiver]:
    r = _Receiver()
    yield r
    r.close()


@pytest.fixture
def short_wait(monkeypatch: pytest.MonkeyPatch) -> float:
    """Shrink the five-minute handoff wait for tests (injected, never config)."""
    monkeypatch.setattr(notify, "HANDOFF_WAIT_SECONDS", 0.5)
    return 0.5


def test_webhook_answering_after_a_while_is_delivered(receiver: _Receiver) -> None:
    receiver.delay = 1.0
    coll = _make_collection(WebhookHandoff(webhook=receiver.url))

    assert fire_complete(coll, _complete()) is None
    assert receiver.bodies[0]["event"] == "sync.complete"


def test_webhook_that_never_answers_is_undelivered_after_the_wait(
    receiver: _Receiver, short_wait: float
) -> None:
    receiver.delay = None
    coll = _make_collection(WebhookHandoff(webhook=receiver.url))

    started = time.monotonic()
    err = fire_complete(coll, _complete())

    assert err == "no answer within 5 minutes"
    assert time.monotonic() - started < short_wait + 5


@pytest.mark.parametrize("status", [302, 404, 503])
def test_webhook_answering_non_2xx_is_undelivered(receiver: _Receiver, status: int) -> None:
    """Only a 2xx answer delivers — a redirect is not an acknowledgement."""
    receiver.status = status
    coll = _make_collection(WebhookHandoff(webhook=receiver.url))

    assert fire_complete(coll, _complete()) == f"webhook answered {status}"


def test_webhook_at_a_closed_port_is_undelivered() -> None:
    receiver = _Receiver()
    url = receiver.url
    receiver.close()  # nothing listens there any more
    coll = _make_collection(WebhookHandoff(webhook=url))

    err = fire_complete(coll, _complete())

    assert err is not None
    assert err.startswith("webhook unreachable: ")


def test_reason_never_carries_the_webhook_url() -> None:
    """GET /status needs no token, and a webhook URL can carry one."""
    coll = _make_collection(WebhookHandoff(webhook="http://hook.example/x?token=s3cret"))

    def boom(*args, **kwargs):
        raise httpx.ConnectError("nobody home")

    with patch("shakedown.notify.httpx.post", side_effect=boom):
        err = fire_complete(coll, _complete())

    assert err is not None
    assert "s3cret" not in err


def test_exec_that_starts_work_in_the_background_and_returns_is_delivered(
    tmp_path: Path, short_wait: float
) -> None:
    """The documented pattern — start the import and return — delivers at once, and
    the work it started keeps running (§spec:durable-handoff: a trigger, not a wait)."""
    done = tmp_path / "import-finished"
    coll = _make_collection(ExecHandoff(exec=f"sh -c '(sleep 1; touch {done}) & exit 0'"))

    started = time.monotonic()
    err = fire_complete(coll, _complete())

    assert err is None
    assert time.monotonic() - started < short_wait
    deadline = time.monotonic() + 5
    while not done.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert done.exists(), "the background work must not be stopped"


def test_exec_that_never_exits_is_stopped_after_the_wait(short_wait: float) -> None:
    """Past the wait the command is stopped — including anything it started, so a
    shell wrapper's child holding the error output cannot hold the run."""
    coll = _make_collection(ExecHandoff(exec="sh -c 'sleep 60 & sleep 60'"))

    started = time.monotonic()
    err = fire_complete(coll, _complete())

    assert err == "no answer within 5 minutes"
    assert time.monotonic() - started < short_wait + 5
