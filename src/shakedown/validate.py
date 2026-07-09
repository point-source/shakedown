"""Setup readiness validation before a large mirror begins."""
from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from urllib.parse import urlparse

import click

from shakedown.config import CollectionConfig, Config, ExecHandoff, SourceConfig, WebhookHandoff
from shakedown.plugins import registry
from shakedown.recovery import clear_issue, record_issue


def run_validate(
    config: Config,
    *,
    source_filter: str | None = None,
    collection_filter: str | None = None,
    live_handoff: bool = False,
) -> int:
    """Preflight configured paths, source reachability, credentials, and handoff."""
    failures = 0
    for source in config.sources:
        if source_filter and source.name != source_filter:
            continue
        for collection in source.collections:
            if collection_filter and collection.name != collection_filter:
                continue
            result = _validate_collection(config, source, collection, live_handoff)
            if result is None:
                clear_issue(config, source=source.name, collection=collection.name, operation="validate")
                click.echo(f"PASS {source.name}/{collection.name} setup readiness")
            else:
                failures += 1
                phase, message, next_action = result
                record_issue(
                    config,
                    source=source.name,
                    collection=collection.name,
                    operation="validate",
                    phase=phase,
                    message=message,
                    next_action=next_action,
                )
                click.echo(
                    f"FAIL {source.name}/{collection.name} setup readiness: "
                    f"{phase}: {message}; next: {next_action}"
                )
    return 0 if failures == 0 else 1


def _validate_collection(
    config: Config,
    source: SourceConfig,
    collection: CollectionConfig,
    live_handoff: bool,
) -> tuple[str, str, str] | None:
    for label, path in (
        ("archive path", config.archive_root),
        ("library path", config.library_root),
        ("state path", config.state_db.parent if config.state_db else config.archive_root),
    ):
        failure = _probe_writable(label, path)
        if failure is not None:
            return failure

    failure = _check_auth_env(source)
    if failure is not None:
        return failure

    failure = _check_source_access(source, collection)
    if failure is not None:
        return failure

    failure = _check_handoff(collection, live_handoff)
    if failure is not None:
        return failure

    return None


def _probe_writable(label: str, path: Path) -> tuple[str, str, str] | None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".shakedown-validate-write"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        return (label, f"{path} is not writable: {e}", "fix configuration then rerun")
    return None


def _check_auth_env(source: SourceConfig) -> tuple[str, str, str] | None:
    if source.auth is None:
        return None
    for env_name in (source.auth.email_env, source.auth.password_env):
        if env_name and not os.environ.get(env_name):
            return (
                "credentials",
                f"environment variable {env_name} is not set",
                "fix configuration then rerun",
            )
    return None


def _check_source_access(
    source: SourceConfig, collection: CollectionConfig
) -> tuple[str, str, str] | None:
    try:
        plugin = registry.for_source(source)
        identifiers = plugin.enumerate_items(collection)
        if identifiers is None:
            stream = plugin.discover(collection)
            next(stream, None)
        else:
            next(iter(identifiers), None)
    except Exception as e:
        return ("source access", str(e), "fix configuration then rerun")
    return None


def _check_handoff(
    collection: CollectionConfig, live_handoff: bool
) -> tuple[str, str, str] | None:
    handoff = collection.on_complete
    if handoff is None:
        return None
    if isinstance(handoff, WebhookHandoff):
        parsed = urlparse(handoff.webhook)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return (
                "handoff",
                f"webhook URL {handoff.webhook!r} is not a reachable HTTP(S) target",
                "fix configuration then rerun",
            )
        if live_handoff:
            return None
        return None
    if isinstance(handoff, ExecHandoff):
        try:
            argv = shlex.split(handoff.exec)
        except ValueError as e:
            return ("handoff", f"exec command is invalid: {e}", "fix configuration then rerun")
        if not argv:
            return ("handoff", "exec command is empty", "fix configuration then rerun")
        expanded = os.path.expandvars(argv[0])
        command = shutil.which(expanded) if not Path(expanded).is_absolute() else expanded
        if command is None or not Path(command).exists():
            return (
                "handoff",
                f"exec command {argv[0]!r} was not found",
                "fix configuration then rerun",
            )
    return None
