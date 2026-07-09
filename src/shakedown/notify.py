"""Library handoff and failure notifications (SPEC §spec:handoff).

Two versioned, once-per-(collection, run) payloads over the same envelope:

- ``sync.complete`` fires a collection's ``on_complete`` webhook/exec after a
  run staged at least one item — the batch handoff that flows new shows into the
  library tool.
- ``sync.failed`` fires the global ``notifications.on_failure`` webhook when a
  run fails, carrying the run's ``errors`` array.

Delivery is best-effort: a failure is logged and returned to the caller (which
records it in the run's errors, visible in ``status``) but never aborts the sync
— the mirror's integrity does not depend on a listener being up. The URL/command
may embed the ``{source}``, ``{collection}``, and ``{staging_root}`` template
fields. Payloads carry paths and counts only, never credentials.
"""
from __future__ import annotations

import json
import logging
import shlex
import shutil
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
PAYLOAD_VERSION = 1

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
    """Fire a collection's ``on_complete`` handoff. Returns a delivery-error string
    (for the caller to record in run errors) or ``None`` on success/no-op.

    Best-effort: any delivery fault — a malformed URL, an unparseable/empty exec
    command, a down listener — is turned into a returned error string and never
    raised, so a handoff can never abort the sync (SPEC §spec:handoff)."""
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
    """Fire the global ``notifications.on_failure`` webhook. Returns a
    delivery-error string (for the caller to record) or ``None`` on success/no-op.

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
    cmd = _expand(handoff.exec, body)
    try:
        completed = subprocess.run(
            shlex.split(cmd),
            check=False,
            capture_output=True,
            timeout=30,
            input=json.dumps(body).encode(),
        )
    except (subprocess.SubprocessError, OSError) as e:
        msg = f"handoff exec {cmd} failed: {e}"
        log.warning(msg)
        return msg
    if completed.returncode != 0:
        msg = f"handoff exec {cmd} exited {completed.returncode}"
        log.warning(msg)
        return msg
    return None


def _post(url: str, body: dict[str, Any]) -> str | None:
    try:
        r = httpx.post(url, json=body, timeout=10.0)
    except httpx.HTTPError as e:
        msg = f"handoff webhook {url} failed: {e}"
        log.warning(msg)
        return msg
    if r.status_code >= 400:
        msg = f"handoff webhook {url} returned {r.status_code}"
        log.warning(msg)
        return msg
    return None


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
