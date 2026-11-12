"""Handoff readiness errors are recorded and shown by the unauthenticated dashboard
(§spec:dashboard "never secrets"): they name the key, never its value."""
from __future__ import annotations

from shakedown.config import ExecHandoff, WebhookHandoff
from shakedown.notify import inspect_handoff

TOKEN = "tok-9d1c"


def test_a_malformed_webhook_url_is_named_not_quoted() -> None:
    ok, consequence, _action = inspect_handoff(
        WebhookHandoff(webhook=f"hxxp://hooks.example/{TOKEN}"), source="s", collection="c"
    )
    assert not ok and consequence is not None
    assert "on_complete.webhook" in consequence and TOKEN not in consequence


def test_an_unparsable_exec_command_is_named_not_quoted() -> None:
    ok, consequence, _action = inspect_handoff(
        ExecHandoff(exec=f'curl -H "Authorization: {TOKEN}'), source="s", collection="c"
    )
    assert not ok and consequence is not None
    assert "on_complete.exec" in consequence and TOKEN not in consequence
