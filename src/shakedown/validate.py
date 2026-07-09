"""Setup readiness validation: `shakedown validate` (§spec:setup-readiness-validation).

A dedicated preflight an operator runs *before* the first large mirror. It reuses the
same startup configuration contract as every command, then continues into setup-specific
probes — path writability, same-filesystem archive/library, source/collection
reachability, credential presence/acceptance, layout collision-risk, and handoff
readiness — and reports one grouped pass/fail result per source and collection. Each
failing check names the affected setting/source/collection, the plain-language
consequence, and the user action needed to continue.

Crucially it never downloads a full collection and never leaves archive or library items
behind, so a broken setup cannot look like a clean no-op. A pass means "this deployment is
ready to attempt a real sync," not "the collection has been mirrored." Validation failures
are recorded through the durable operation-outcome model (§spec:recoverable-operation-reporting)
so `status` can explain a failed validation and the safe next action.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import click

from shakedown import config as config_module
from shakedown import notify
from shakedown.config import Config
from shakedown.db import connect, transaction
from shakedown.filesystem import FilesystemError, check_writable, ensure_same_filesystem
from shakedown.models import OperationStatus, OperationType
from shakedown.plugins import registry
from shakedown.plugins.base import ProbeResult
from shakedown.state import OperationOutcomeRepo
from shakedown.utils.templates import fields_in

log = logging.getLogger(__name__)


@dataclass
class Check:
    """One readiness check. On failure, ``consequence`` and ``action`` are the
    plain-language "what breaks" / "what to do" surfaced verbatim in the output."""
    name: str
    ok: bool
    target: str | None = None
    consequence: str | None = None
    action: str | None = None

    @classmethod
    def from_probe(cls, name: str, target: str, probe: ProbeResult) -> Check:
        """Lift a plugin :class:`ProbeResult` into a named readiness check."""
        return cls(
            name=name,
            ok=probe.ok,
            target=target,
            consequence=probe.consequence,
            action=probe.action,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "target": self.target,
            "consequence": self.consequence,
            "action": self.action,
        }


@dataclass
class Group:
    """A group of checks. ``collection is None`` marks a source-wide group; both None
    marks the deployment-level configuration group."""
    source: str | None
    collection: str | None
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def label(self) -> str:
        if self.source is None:
            return "configuration"
        if self.collection is None:
            return self.source
        return f"{self.source}/{self.collection}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "collection": self.collection,
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class Report:
    groups: list[Group]

    @property
    def ok(self) -> bool:
        return all(g.ok for g in self.groups)

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ok, "groups": [g.to_dict() for g in self.groups]}


def _config_group(config: Config) -> Group:
    """Deployment-level checks: archive/library/state writability + same filesystem."""
    checks: list[Check] = []
    state_dir = config.state_db.parent if config.state_db else config.archive_root

    for name, path in (
        ("archive path writable", config.archive_root),
        ("library path writable", config.library_root),
        ("state path writable", state_dir),
    ):
        try:
            check_writable(path)
            checks.append(Check(name=name, ok=True, target=str(path)))
        except FilesystemError as e:
            checks.append(
                Check(
                    name=name,
                    ok=False,
                    target=str(path),
                    consequence=f"{e}; syncing will fail before writing anything",
                    action="fix the volume mount / permissions before syncing",
                )
            )

    try:
        ensure_same_filesystem(config.archive_root, config.library_root)
        checks.append(
            Check(name="archive and library share a filesystem", ok=True)
        )
    except (FilesystemError, OSError) as e:
        checks.append(
            Check(
                name="archive and library share a filesystem",
                ok=False,
                target=f"{config.archive_root} / {config.library_root}",
                consequence=f"{e}",
                action="move archive_root and library_root under the same volume",
            )
        )

    return Group(source=None, collection=None, checks=checks)


def _layout_check(source_type: str, collection: config_module.CollectionConfig) -> Check:
    """Elevate the existing collision-risk guard (§spec:layout-collision-safety) to a
    readiness failure: a non-passthrough layout with no per-item-unique field will drop
    the loser of any two items sharing its fields. Config load only *warns* about this;
    validate treats it as a fail so an unsafe template exits non-zero before a mirror.

    Hard layout faults (unknown field, no field at all) are already rejected at config
    load, so they never reach here — a config with one would fail the startup contract.
    """
    name = "library layout is collision-safe"
    layout = collection.library_layout
    if layout == "passthrough":
        return Check(name=name, ok=True, target=collection.name)
    plugin_cls = registry.plugin_class(source_type)
    unique = set(plugin_cls.per_item_unique_fields)
    referenced = fields_in(layout)
    if referenced and not (referenced & unique):
        return Check(
            name=name,
            ok=False,
            target=collection.name,
            consequence=(
                f"library_layout {layout!r} references no per-item-unique field "
                f"{sorted(unique) or '(none declared by plugin)'}; two items sharing "
                f"these fields collide at staging and the loser is dropped from the library"
            ),
            action="add a unique component such as {identifier} to the layout leaf",
        )
    return Check(name=name, ok=True, target=collection.name)


def _handoff_check(
    collection: config_module.CollectionConfig, source_name: str, *, live_handoff: bool
) -> Check | None:
    """Handoff readiness. Returns None when the collection configures no handoff.

    Default is non-mutating (parse/inspect only). ``--live-handoff`` performs the
    explicit, auditable live test: a marked webhook or the configured command with a
    marked test payload."""
    handoff = collection.on_complete
    if handoff is None:
        return None
    name = "handoff target ready"
    if not live_handoff:
        ok, consequence, action = notify.inspect_handoff(
            handoff, source=source_name, collection=collection.name
        )
        return Check(name=name, ok=ok, target=collection.name, consequence=consequence, action=action)

    # Live: explicit opt-in mutating test.
    name = "live handoff test"
    err = notify.send_handoff_test(handoff, source=source_name, collection=collection.name)
    if err is None:
        return Check(name=name, ok=True, target=collection.name)
    return Check(
        name=name,
        ok=False,
        target=collection.name,
        consequence=f"marked handoff test did not deliver: {err}",
        action="fix the handoff target so it accepts the marked validation payload",
    )


def validate_config(
    config: Config,
    *,
    source_filter: str | None = None,
    collection_filter: str | None = None,
    live_handoff: bool = False,
) -> Report:
    """Run every readiness probe on an already-loaded config and return the report.

    Records the per-collection outcome through the durable operation model so `status`
    surfaces a failed validation (best-effort: a state DB we cannot open never blocks
    the report itself)."""
    groups: list[Group] = [_config_group(config)]

    for source in config.sources:
        if source_filter and source.name != source_filter:
            continue

        src_checks: list[Check] = []
        plugin = None
        try:
            plugin = registry.for_source(source)
        except Exception as e:  # a broken plugin is a readiness failure, not a crash
            src_checks.append(
                Check(
                    name="source plugin loads",
                    ok=False,
                    target=source.name,
                    consequence=f"source {source.name!r} could not be initialized: {e}",
                    action="check the source type and configuration",
                )
            )

        if plugin is not None:
            src_checks.append(
                Check.from_probe(
                    "credentials present", source.name, plugin.check_credentials()
                )
            )
        groups.append(Group(source=source.name, collection=None, checks=src_checks))

        for collection in source.collections:
            if collection_filter and collection.name != collection_filter:
                continue
            checks: list[Check] = []

            if plugin is not None:
                checks.append(
                    Check.from_probe(
                        "source reachable",
                        f"{source.name}/{collection.name}",
                        plugin.check_reachable(collection),
                    )
                )
                checks.append(_layout_check(source.type, collection))

            handoff_check = _handoff_check(
                collection, source.name, live_handoff=live_handoff
            )
            if handoff_check is not None:
                checks.append(handoff_check)

            groups.append(
                Group(source=source.name, collection=collection.name, checks=checks)
            )

    report = Report(groups)
    _record_outcomes(config, report)
    return report


def _first_failure(group: Group) -> Check | None:
    return next((c for c in group.checks if not c.ok), None)


def _record_outcomes(config: Config, report: Report) -> None:
    """Persist a VALIDATE outcome per in-scope (source, collection) so `status` explains
    a failed validation and the safe next action. Deployment- and source-level failures
    are inherited by every collection they block. Best-effort: if the state DB cannot be
    opened (e.g. the archive path under validation is itself broken), we skip recording —
    the printed report and exit code remain the authoritative result."""
    try:
        conn = connect(config.state_db)  # type: ignore[arg-type]
    except Exception as e:  # best-effort: a broken state path never blocks the report
        log.warning("validate: could not open state DB to record outcomes: %s", e)
        return

    cfg_group = next((g for g in report.groups if g.source is None), None)
    cfg_fail = _first_failure(cfg_group) if cfg_group else None
    src_fail = {
        g.source: _first_failure(g)
        for g in report.groups
        if g.source is not None and g.collection is None and not g.ok
    }

    outcomes = OperationOutcomeRepo(conn)
    now = datetime.now()
    try:
        with transaction(conn):
            for g in report.groups:
                if g.source is None or g.collection is None:
                    continue
                fail = cfg_fail or src_fail.get(g.source) or _first_failure(g)
                outcome = outcomes.start(
                    OperationType.VALIDATE, g.source, g.collection, now, phase="validate"
                )
                if fail is None:
                    outcomes.finish(
                        outcome,
                        OperationStatus.COMPLETED,
                        now,
                        phase="validate",
                        completed_work={"validated": True},
                    )
                    outcomes.resolve_actionable(
                        OperationType.VALIDATE, g.source, g.collection, now
                    )
                else:
                    outcomes.finish(
                        outcome,
                        OperationStatus.FAILED_BEFORE_COMPLETION,
                        now,
                        phase=fail.name,
                        affected_path=fail.target if "path" in fail.name else None,
                        safe_next_action=fail.action or "fix configuration then rerun validate",
                        errors=[fail.consequence] if fail.consequence else [],
                    )
    except Exception as e:  # recording is best-effort, never fatal to validation
        log.warning("validate: could not record outcomes: %s", e)


def _emit(report: Report, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2, default=str))
        return
    for g in report.groups:
        click.echo(f"=== {g.label} ===")
        for c in g.checks:
            if c.ok:
                click.echo(f"  PASS {c.name}")
            else:
                click.echo(f"  FAIL {c.name}: {c.consequence}")
                if c.action:
                    click.echo(f"       → {c.action}")
        click.echo("")
    if report.ok:
        click.echo("READY: this deployment is ready to attempt a real sync.")
    else:
        click.echo("NOT READY: fix the failures above, then rerun `shakedown validate`.")


def run_validate(
    config_path: Any,
    *,
    source_filter: str | None = None,
    collection_filter: str | None = None,
    live_handoff: bool = False,
    as_json: bool = False,
) -> int:
    """CLI entry point. Loads config (surfacing a startup-contract failure as the first
    failed check, not a traceback), runs validation, prints the report, and returns a
    non-zero exit code on any failure."""
    try:
        config = config_module.load(config_path)
    except config_module.ConfigError as e:
        report = Report(
            [
                Group(
                    source=None,
                    collection=None,
                    checks=[
                        Check(
                            name="startup configuration valid",
                            ok=False,
                            target=str(config_path),
                            consequence=str(e),
                            action="fix shakedown.yaml, then rerun `shakedown validate`",
                        )
                    ],
                )
            ]
        )
        _emit(report, as_json=as_json)
        return 1

    report = validate_config(
        config,
        source_filter=source_filter,
        collection_filter=collection_filter,
        live_handoff=live_handoff,
    )
    _emit(report, as_json=as_json)
    return 0 if report.ok else 1
