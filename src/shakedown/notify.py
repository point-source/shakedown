"""Library handoff notifications (SPEC §spec:handoff).

``sync.complete`` fires a collection's ``on_complete`` webhook/exec once per
(collection, run) after the run staged at least one item — the batch handoff that
flows new shows into the library tool.

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

from shakedown.config import CollectionConfig, Handoff, WebhookHandoff

log = logging.getLogger(__name__)

# Bumped only on a breaking change to the payload shape so downstream
# integrations can rely on the contract (SPEC §spec:handoff).
PAYLOAD_VERSION = 1


@dataclass
class SyncCompletePayload:
    """Batch ``sync.complete`` body: one per (collection, run) that staged items."""
    source: str
    collection: str
    staging_root: str
    run: dict[str, Any]
    staged: list[dict[str, str]] = field(default_factory=list)

    def to_body(self) -> dict[str, Any]:
        return {
            "payload_version": PAYLOAD_VERSION,
            "event": "sync.complete",
            "source": self.source,
            "collection": self.collection,
            "staging_root": self.staging_root,
            "run": self.run,
            "staged": self.staged,
        }


def fire_complete(collection: CollectionConfig, payload: SyncCompletePayload) -> str | None:
    """Fire a collection's ``on_complete`` handoff. Returns a delivery-error string
    (for the caller to record in run errors) or ``None`` on success/no-op."""
    handoff = collection.on_complete
    if handoff is None:
        return None
    return _dispatch(handoff, payload.to_body())


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
