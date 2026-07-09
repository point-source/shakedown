"""Opt-in release validation gate."""
from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from shakedown.config import CollectionConfig, Config, ConfigError, ExecHandoff, SourceConfig, load
from shakedown.models import Manifest, ManifestFile
from shakedown.plugins import registry
from shakedown.plugins.base import FetchResult, ItemDescriptor, SourcePlugin, VerifyResult
from shakedown.reconcile import run_reconcile
from shakedown.restage import run_restage
from shakedown.status import print_status
from shakedown.sync import run_sync
from shakedown.validate import run_validate


@dataclass(frozen=True)
class ScenarioResult:
    workflow: str
    passed: bool
    detail: str


@dataclass
class _ReleaseFakeFile:
    name: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)

    @property
    def md5(self) -> str:
        return hashlib.md5(self.content).hexdigest()


@dataclass
class _ReleaseFakeItem:
    identifier: str
    files: list[_ReleaseFakeFile] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class _ReleaseFakePlugin(SourcePlugin):
    type_name = "release-fake"
    template_fields = ("identifier", "year")
    per_item_unique_fields = ("identifier",)

    items: ClassVar[dict[str, _ReleaseFakeItem]] = {}
    fail_enumerate: ClassVar[bool] = False
    fail_fetch_once: ClassVar[bool] = False

    def enumerate_items(self, collection: CollectionConfig):
        if self.fail_enumerate:
            raise ConnectionError("release fake source unavailable")
        yield from self.items

    def describe_item(self, identifier: str, collection: CollectionConfig) -> ItemDescriptor | None:
        item = self.items.get(identifier)
        if item is None:
            return None
        return _descriptor(item)

    def discover(self, collection: CollectionConfig):
        for identifier in self.enumerate_items(collection):
            desc = self.describe_item(identifier, collection)
            if desc is not None:
                yield desc

    def fetch(
        self,
        item: ItemDescriptor,
        dest_dir: Path,
        format_filters: list[str],
        exclude_filters: list[str],
    ) -> FetchResult:
        if type(self).fail_fetch_once:
            type(self).fail_fetch_once = False
            raise RuntimeError("forced release validation fetch failure")
        src = self.items[item.identifier]
        dest_dir.mkdir(parents=True, exist_ok=True)
        total = 0
        for file in src.files:
            (dest_dir / file.name).write_bytes(file.content)
            total += file.size
        return FetchResult(success=True, bytes_downloaded=total)

    def verify(self, item: ItemDescriptor, archive_path: Path) -> VerifyResult:
        missing = [f.name for f in item.manifest.files if not (archive_path / f.name).is_file()]
        return VerifyResult(ok=not missing, missing_files=missing)


def run_release_validation(*, deterministic: bool, real_source: bool) -> int:
    """Run the requested release gate portions and print a workflow summary."""
    registry.register(_ReleaseFakePlugin)
    results: list[ScenarioResult] = []
    if deterministic:
        results.extend(_run_deterministic_scenarios())
    if real_source:
        results.append(_run_real_source_scenario())

    print("Release validation summary")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status} {result.workflow}: {result.detail}")

    return 0 if results and all(r.passed for r in results) else 1


def _run_deterministic_scenarios() -> list[ScenarioResult]:
    return [
        _run_quietly(_scenario_setup_readiness),
        _run_quietly(_scenario_unsafe_config_rejection),
        _run_quietly(_scenario_handoff_failure),
        _run_quietly(_scenario_partial_failure_recovery),
        _run_quietly(_scenario_sync_to_library_staging),
    ]


def _run_quietly(func: Callable[[], ScenarioResult]) -> ScenarioResult:
    buf = StringIO()
    disabled_level = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with redirect_stdout(buf), redirect_stderr(buf):
            return func()
    except Exception as e:
        return ScenarioResult(func.__name__.removeprefix("_scenario_"), False, str(e))
    finally:
        logging.disable(disabled_level)


def _scenario_setup_readiness() -> ScenarioResult:
    with TemporaryDirectory() as tmp:
        config = _config(Path(tmp), "ready")
        rc = run_validate(config)
        return ScenarioResult(
            "setup readiness",
            rc == 0,
            "valid throwaway deployment passed readiness" if rc == 0 else "validate failed",
        )


def _scenario_unsafe_config_rejection() -> ScenarioResult:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = root / "archive"
        library = root / "library"
        cfg = root / "shakedown.yaml"
        cfg.write_text(f"""
archive_root: {archive}
library_root: {library}
sources:
  - name: release-fake-src
    type: release-fake
    collections:
      - name: unsafe
        query: '*'
        library_layout: constant-folder
""")
        try:
            load(cfg)
        except ConfigError as e:
            return ScenarioResult(
                "unsafe config rejection",
                "library_layout" in str(e),
                "unsafe layout rejected before sync",
            )
        return ScenarioResult("unsafe config rejection", False, "unsafe layout loaded cleanly")


def _scenario_handoff_failure() -> ScenarioResult:
    with TemporaryDirectory() as tmp:
        config = _config(Path(tmp), "handoff")
        config.sources[0].collections[0].on_complete = ExecHandoff(
            exec="/definitely/missing/shakedown-release-import"
        )
        rc = run_validate(config)
        status_text = _status_text(config)
        return ScenarioResult(
            "handoff failure",
            rc == 1 and "RECOVERY: validate failed" in status_text and "handoff" in status_text,
            "handoff readiness failure reported through status",
        )


def _scenario_partial_failure_recovery() -> ScenarioResult:
    checks: list[bool] = []
    details: list[str] = []
    with TemporaryDirectory() as tmp:
        root = Path(tmp)

        sync_config = _config(root / "sync", "sync-recovery")
        _ReleaseFakePlugin.fail_fetch_once = True
        failed = run_sync(sync_config)
        sync_failed_status = _status_text(sync_config)
        retried = run_sync(sync_config)
        sync_recovered_status = _status_text(sync_config)
        checks.append(
            failed == 1
            and "RECOVERY: sync failed" in sync_failed_status
            and retried == 0
            and "RECOVERY:" not in sync_recovered_status
        )
        details.append("sync")

        restage_config = _config(root / "restage", "restage-recovery", same_file_names=True)
        assert run_sync(restage_config) == 0
        colliding = restage_config.model_copy(deep=True)
        colliding.sources[0].collections[0].library_layout = "{year}"
        restage_failed = run_restage(colliding)
        restage_failed_status = _status_text(colliding)
        healthy = restage_config.model_copy(deep=True)
        healthy.sources[0].collections[0].library_layout = "{identifier}"
        restage_retried = run_restage(healthy)
        restage_recovered_status = _status_text(healthy)
        checks.append(
            restage_failed == 1
            and "RECOVERY: restage failed" in restage_failed_status
            and restage_retried == 0
            and "RECOVERY:" not in restage_recovered_status
        )
        details.append("restage")

        reconcile_config = _config(root / "reconcile", "reconcile-recovery")
        assert run_sync(reconcile_config) == 0
        assert reconcile_config.state_db is not None
        reconcile_config.state_db.unlink()
        _ReleaseFakePlugin.fail_enumerate = True
        reconcile_failed = run_reconcile(reconcile_config)
        reconcile_failed_status = _status_text(reconcile_config)
        _ReleaseFakePlugin.fail_enumerate = False
        reconcile_retried = run_reconcile(reconcile_config)
        reconcile_recovered_status = _status_text(reconcile_config)
        checks.append(
            reconcile_failed == 1
            and "RECOVERY: reconcile failed" in reconcile_failed_status
            and reconcile_retried == 0
            and "RECOVERY:" not in reconcile_recovered_status
        )
        details.append("reconcile")

        validate_config = _config(root / "validate", "validate-recovery")
        _ReleaseFakePlugin.fail_enumerate = True
        validate_failed = run_validate(validate_config)
        validate_failed_status = _status_text(validate_config)
        _ReleaseFakePlugin.fail_enumerate = False
        validate_retried = run_validate(validate_config)
        validate_recovered_status = _status_text(validate_config)
        checks.append(
            validate_failed == 1
            and "RECOVERY: validate failed" in validate_failed_status
            and validate_retried == 0
            and "RECOVERY:" not in validate_recovered_status
        )
        details.append("validation")

    _ReleaseFakePlugin.fail_enumerate = False
    _ReleaseFakePlugin.fail_fetch_once = False
    return ScenarioResult(
        "partial-failure recovery",
        all(checks),
        f"retry warnings exercised for {', '.join(details)}",
    )


def _scenario_sync_to_library_staging() -> ScenarioResult:
    with TemporaryDirectory() as tmp:
        config = _config(Path(tmp), "staging")
        rc = run_sync(config)
        archive = config.archive_root / "release-fake-src" / "staging" / "show-a" / "track.flac"
        library = config.library_root / "release-fake-src" / "staging" / "show-a" / "track.flac"
        hardlinked = archive.exists() and library.exists() and archive.stat().st_ino == library.stat().st_ino
        return ScenarioResult(
            "sync-to-library staging",
            rc == 0 and hardlinked,
            "ordinary sync created archive file and library hardlink",
        )


def _run_real_source_scenario() -> ScenarioResult:
    cmd = [sys.executable, "-m", "pytest", "-q", "-m", "network", "tests/test_e2e_real_source.py"]
    completed = subprocess.run(cmd, text=True, capture_output=True)
    output = (completed.stdout + completed.stderr).strip().splitlines()
    detail = output[-1] if output else "network check exited without output"
    return ScenarioResult("real-source IA seam", completed.returncode == 0, detail)


def _config(root: Path, collection_name: str, *, same_file_names: bool = False) -> Config:
    _reset_fake()
    archive = root / "archive"
    library = root / "library"
    archive.mkdir(parents=True, exist_ok=True)
    library.mkdir(parents=True, exist_ok=True)
    first_name = "track.flac"
    second_name = "track.flac" if same_file_names else "track-2.flac"
    _ReleaseFakePlugin.items["show-a"] = _ReleaseFakeItem(
        "show-a",
        [_ReleaseFakeFile(first_name, b"audio-a")],
        {"identifier": "show-a", "year": "1977"},
    )
    _ReleaseFakePlugin.items["show-b"] = _ReleaseFakeItem(
        "show-b",
        [_ReleaseFakeFile(second_name, b"audio-b")],
        {"identifier": "show-b", "year": "1977"},
    )
    return Config(
        archive_root=archive,
        library_root=library,
        max_concurrent_downloads=1,
        max_concurrent_collections=1,
        sources=[
            SourceConfig(
                name="release-fake-src",
                type="release-fake",
                collections=[CollectionConfig(name=collection_name, query="*")],
            )
        ],
    )


def _descriptor(item: _ReleaseFakeItem) -> ItemDescriptor:
    files = tuple(ManifestFile(f.name, f.size, f.md5) for f in item.files)
    return ItemDescriptor(
        identifier=item.identifier,
        manifest=Manifest(files=files),
        metadata={**item.metadata, "identifier": item.identifier},
    )


def _reset_fake() -> None:
    _ReleaseFakePlugin.items.clear()
    _ReleaseFakePlugin.fail_enumerate = False
    _ReleaseFakePlugin.fail_fetch_once = False


def _status_text(config: Config) -> str:
    buf = StringIO()
    with redirect_stdout(buf):
        print_status(config, as_json=False)
    return buf.getvalue()
