"""Library handoff and failure notifications (SPEC §spec:handoff, §spec:durable-handoff).

Two versioned, once-per-(collection, run) payloads over the same envelope:

- ``sync.complete`` fires a collection's ``on_complete`` webhook/exec with the shows
  staged and not yet delivered — the batch handoff that tells the library tool new
  shows are ready.
- ``sync.failed`` fires the global ``notifications.on_failure`` webhook when a
  run fails, carrying the run's ``errors`` array.

A handoff is a trigger with a bounded wait: a webhook answered 2xx, or a command
that exits 0, within five minutes delivers the notice. Anything else leaves it
undelivered, and the reason is returned to the caller (which keeps the shows
pending and shows them in ``status``). Delivery never raises and never aborts the
sync — the mirror's integrity does not depend on a listener being up. The
URL/command may embed the ``{source}``, ``{collection}``, and ``{staging_root}``
template fields. Payloads carry paths and counts only, never credentials.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from shakedown.config import (
    CollectionConfig,
    ExecHandoff,
    Handoff,
    NotificationsConfig,
    WebhookHandoff,
)

log = logging.getLogger(__name__)

# Bumped only on a breaking change to the payload shape so downstream
# integrations can rely on the contract (SPEC §spec:handoff).
# v2 (§spec:durable-handoff): `staged` means "staged and not yet delivered", so it can
# hold shows from earlier runs.
PAYLOAD_VERSION = 2

# A handoff is a trigger with a bounded wait (§spec:durable-handoff): shakedown waits up
# to five minutes for a webhook's answer or a command's exit. Fixed, not configurable;
# tests shrink it by patching this module attribute.
HANDOFF_WAIT_SECONDS = 300.0
NO_ANSWER = "no answer within 5 minutes"
_ERROR_TAIL_LINES = 3
_ERROR_TAIL_CHARS = 300

# Event name and marker for the readiness handoff test (§spec:setup-readiness-validation).
# A receiver keys on `event == VALIDATION_EVENT` / `test is True` to recognize a marked
# validation probe and NOT trigger a production import or notification.
VALIDATION_EVENT = "validate.handoff_test"


def _envelope(
    event: str, source: str, collection: str, staging_root: str,
    run: dict[str, Any], **extra: Any,
) -> dict[str, Any]:
    """Shared versioned envelope for both payloads, so the version/event contract
    lives in one place and can't drift between sync.complete and sync.failed."""
    return {
        "payload_version": PAYLOAD_VERSION,
        "event": event,
        "source": source,
        "collection": collection,
        "staging_root": staging_root,
        "run": run,
        **extra,
    }


@dataclass
class SyncCompletePayload:
    """Batch ``sync.complete`` body: one per (collection, run) that staged items."""
    source: str
    collection: str
    staging_root: str
    run: dict[str, Any]
    staged: list[dict[str, str]] = field(default_factory=list)

    def to_body(self) -> dict[str, Any]:
        return _envelope(
            "sync.complete", self.source, self.collection, self.staging_root,
            self.run, staged=self.staged,
        )


@dataclass
class SyncFailedPayload:
    """``sync.failed`` body: same envelope as complete, plus an ``errors`` array."""
    source: str
    collection: str
    staging_root: str
    run: dict[str, Any]
    errors: list[str] = field(default_factory=list)

    def to_body(self) -> dict[str, Any]:
        return _envelope(
            "sync.failed", self.source, self.collection, self.staging_root,
            self.run, errors=self.errors,
        )


def fire_complete(collection: CollectionConfig, payload: SyncCompletePayload) -> str | None:
    """Fire a collection's ``on_complete`` handoff. Returns why the notice was not
    delivered, or ``None`` when it was (or there is no handoff).

    Never raises: any delivery fault — a malformed URL, an unparseable/empty exec
    command, a down or silent listener — comes back as the reason, so a handoff can
    never abort the sync (SPEC §spec:durable-handoff)."""
    handoff = collection.on_complete
    if handoff is None:
        return None
    try:
        return _dispatch(handoff, payload.to_body())
    except Exception as e:
        msg = f"handoff delivery failed: {e}"
        log.warning(msg)
        return msg


def fire_failure(
    notifications: NotificationsConfig | None, payload: SyncFailedPayload
) -> str | None:
    """Fire the global ``notifications.on_failure`` webhook. Returns why it was not
    delivered, or ``None`` when it was (or none is configured). Not queued: a late
    alert about a superseded run is noise (SPEC §spec:durable-handoff).

    Best-effort with the same never-raise contract as :func:`fire_complete`."""
    if notifications is None or notifications.on_failure is None:
        return None
    url = notifications.on_failure.webhook
    if not url:
        return None
    try:
        body = payload.to_body()
        return _post(_expand(url, body), body)
    except Exception as e:
        msg = f"failure notification delivery failed: {e}"
        log.warning(msg)
        return msg


def _validation_body(source: str, collection: str, staging_root: str) -> dict[str, Any]:
    """Marked readiness-test payload (§spec:setup-readiness-validation).

    Same envelope as a real handoff so the receiver's parser exercises the true path,
    but tagged with ``event=validate.handoff_test`` and ``test=True`` so a correct
    receiver recognizes it as a probe and does not import music or notify anyone."""
    body = _envelope(
        VALIDATION_EVENT, source, collection, staging_root, run={}, staged=[]
    )
    body["test"] = True
    return body


def inspect_handoff(
    handoff: Handoff, *, source: str, collection: str
) -> tuple[bool, str | None, str | None]:
    """Non-mutating default handoff readiness check (§spec:setup-readiness-validation).

    Returns ``(ok, consequence, action)``. Does NOT send a webhook or run a command —
    a normal readiness check must never trigger a duplicate import or arbitrary side
    effect. Webhook URLs are parsed and required to be absolute http(s); exec commands
    are parsed and their program is required to be present and executable on PATH.
    """
    if isinstance(handoff, WebhookHandoff):
        url = _expand(
            handoff.webhook,
            {"source": source, "collection": collection, "staging_root": "<validation>"},
        )
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return (
                False,
                f"handoff webhook URL {handoff.webhook!r} is not an absolute http(s) URL, "
                f"so the completed-collection handoff cannot be delivered",
                "set on_complete.webhook to a full http(s) URL",
            )
        return (True, None, None)

    if isinstance(handoff, ExecHandoff):
        cmd = _expand(
            handoff.exec,
            {"source": source, "collection": collection, "staging_root": "<validation>"},
        )
        try:
            argv = shlex.split(cmd)
        except ValueError as e:
            return (
                False,
                f"handoff command {handoff.exec!r} could not be parsed: {e}",
                "fix the quoting in on_complete.exec",
            )
        if not argv:
            return (
                False,
                f"handoff command {handoff.exec!r} is empty",
                "set on_complete.exec to a runnable command",
            )
        if shutil.which(argv[0]) is None:
            return (
                False,
                f"handoff command program {argv[0]!r} was not found or is not executable, "
                f"so the completed-collection handoff cannot run",
                f"install {argv[0]!r} or correct the path in on_complete.exec",
            )
        return (True, None, None)

    return (True, None, None)  # pragma: no cover - Handoff is a closed union


def send_handoff_test(
    handoff: Handoff, *, source: str, collection: str
) -> str | None:
    """Live (mutating) handoff readiness test for ``validate --live-handoff``.

    Sends the marked validation webhook or runs the configured command with the marked
    test payload on stdin, and returns a delivery-error string or ``None`` on success.
    This is the explicit, auditable opt-in: it exercises the real handoff path, so it
    is protected by the same bearer-token posture as ad-hoc sync when reached over the
    control plane (§spec:serve)."""
    body = _validation_body(source, collection, "<validation>")
    try:
        return _dispatch(handoff, body)
    except Exception as e:  # mirror fire_complete's never-raise contract
        msg = f"live handoff test failed: {e}"
        log.warning(msg)
        return msg


def _dispatch(handoff: Handoff, body: dict[str, Any]) -> str | None:
    if isinstance(handoff, WebhookHandoff):
        return _post(_expand(handoff.webhook, body), body)
    return _run(_expand(handoff.exec, body), body)


def _post(url: str, body: dict[str, Any]) -> str | None:
    """POST the body; delivered only on a 2xx answer within the wait. The reason
    never names the URL: `GET /status` is open, and a URL can carry a token."""
    try:
        r = httpx.post(url, json=body, timeout=HANDOFF_WAIT_SECONDS)
    except httpx.TimeoutException:
        return NO_ANSWER
    except httpx.HTTPError as e:
        return f"webhook unreachable: {e}"
    if not r.is_success:
        return f"webhook answered {r.status_code}"
    return None


def _run(cmd: str, body: dict[str, Any]) -> str | None:
    """Run the command with the body on stdin; delivered only when it exits 0 within
    the wait. Past the wait the command and everything it started are stopped."""
    try:
        argv = shlex.split(cmd)
        if not argv:
            return "command could not start: the command is empty"
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,  # its own process group, so a stop reaches children
        )
    except (ValueError, OSError) as e:
        return f"command could not start: {e}"
    try:
        _, stderr = proc.communicate(json.dumps(body).encode(), timeout=HANDOFF_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        _stop(proc)
        return NO_ANSWER
    if proc.returncode != 0:
        return f"command exited with status {proc.returncode}: {_error_tail(stderr)}"
    return None


def _stop(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError):  # already gone
        os.killpg(proc.pid, signal.SIGKILL)
    proc.communicate()


def _error_tail(stderr: bytes) -> str:
    """The last lines of a command's error output, on one line and readable length."""
    lines = [
        line.strip()
        for line in stderr.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    return " / ".join(lines[-_ERROR_TAIL_LINES:])[:_ERROR_TAIL_CHARS]


def _expand(template: str, body: dict[str, Any]) -> str:
    """Substitute ``{source}``/``{collection}``/``{staging_root}`` placeholders in
    the URL or command from the payload's top-level fields."""
    fields = {
        "source": body.get("source", ""),
        "collection": body.get("collection", ""),
        "staging_root": body.get("staging_root", ""),
    }
    try:
        return template.format(**fields)
    except (KeyError, IndexError):
        return template
