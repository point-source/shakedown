"""on_complete handoff: webhook POST or local exec. Resolves PRD §14 still-open."""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from shakedown.config import CollectionConfig, ExecHandoff, WebhookHandoff

log = logging.getLogger(__name__)


@dataclass
class HandoffPayload:
    """Stable contract for the on_complete webhook body / exec env."""
    event: str  # "item_complete"
    source: str
    collection: str
    item_identifier: str
    archive_path: str
    staging_path: str
    source_metadata_excerpt: dict[str, Any]


def fire(collection: CollectionConfig, payload: HandoffPayload) -> None:
    """Best-effort handoff. Failures log but don't abort the sync."""
    handoff = collection.on_complete
    if handoff is None:
        return
    body = asdict(payload)
    if isinstance(handoff, WebhookHandoff):
        url = _expand(handoff.webhook, body)
        try:
            r = httpx.post(url, json=body, timeout=10.0)
            if r.status_code >= 400:
                log.warning("handoff webhook %s returned %s", url, r.status_code)
        except httpx.HTTPError as e:
            log.warning("handoff webhook %s failed: %s", url, e)
    elif isinstance(handoff, ExecHandoff):
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
            log.warning("handoff exec %s failed: %s", cmd, e)


def _expand(template: str, body: dict[str, Any]) -> str:
    """Substitute {field} placeholders in the URL/cmd from payload fields."""
    try:
        return template.format(**body)
    except (KeyError, IndexError):
        return template
