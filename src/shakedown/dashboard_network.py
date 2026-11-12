"""`GET /api/network` and the `network` part of `GET /api/activity`: per-source
network activity (§spec:network-activity)."""
from __future__ import annotations

from typing import Any

from shakedown.dashboard import iso
from shakedown.network import NetworkEvent


def event_answer(event: NetworkEvent) -> dict[str, Any]:
    """A network event as the API answers it; absent values are null."""
    return {
        "at": iso(event.at),
        "kind": event.kind.value,
        "http_status": event.http_status,
        "collection": event.collection,
        "identifier": event.identifier,
        "wait_seconds": event.wait_seconds,
        "detail": event.detail,
    }
