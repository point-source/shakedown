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
import subprocess
from dataclasses import dataclass, field
from typing import Any

import httpx

from shakedown.config import (
    CollectionConfig,
    Handoff,
    NotificationsConfig,
    WebhookHandoff,
)

log = logging.getLogger(__name__)

# Bumped only on a breaking change to the payload shape so downstream
# integrations can rely on the contract (SPEC §spec:handoff).
PAYLOAD_VERSION = 1


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
    (for the caller to record in run errors) or ``None`` on success/no-op."""
    handoff = collection.on_complete
    if handoff is None:
        return None
    return _dispatch(handoff, payload.to_body())


def fire_failure(
    notifications: NotificationsConfig | None, payload: SyncFailedPayload
) -> str | None:
    """Fire the global ``notifications.on_failure`` webhook. Returns a
    delivery-error string (for the caller to record) or ``None`` on success/no-op."""
    if notifications is None or notifications.on_failure is None:
        return None
    url = notifications.on_failure.webhook
    if not url:
        return None
    body = payload.to_body()
    return _post(_expand(url, body), body)


def _dispatch(handoff: Handoff, body: dict[str, Any]) -> str | None:
    if isinstance(handoff, WebhookHandoff):
        return _post(_expand(handoff.webhook, body), body)
    cmd = _expand(handoff.exec, body)
    try:
        subprocess.run(
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
