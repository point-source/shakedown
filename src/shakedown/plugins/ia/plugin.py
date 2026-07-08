"""Internet Archive source plugin. Wraps the official `internetarchive` package."""
from __future__ import annotations

import fnmatch
import logging
import os
import shutil
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import internetarchive as ia

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import Manifest, ManifestFile
from shakedown.plugins.base import FetchResult, ItemDescriptor, SourcePlugin, VerifyResult
from shakedown.plugins.registry import register

log = logging.getLogger(__name__)


# Bounded metadata timeout + retry envelope for discovery-time faults, matched to
# archive.org's real latency (SPEC §spec:slow-metadata). Mirrors the fetch path's
# core-owned retry discipline (sync.py) so a slow/5xx/429 metadata response costs a
# bounded wait, not minutes of stacked retries, and never a spurious enumeration failure.
_METADATA_CONNECT_TIMEOUT = 10.0
_METADATA_READ_TIMEOUT = 30.0
_METADATA_TIMEOUT = (_METADATA_CONNECT_TIMEOUT, _METADATA_READ_TIMEOUT)
_MAX_METADATA_ATTEMPTS = 3
_METADATA_BACKOFF_BASE_SECONDS = 1.0
_METADATA_MAX_BACKOFF_SECONDS = 30.0


def _metadata_backoff_seconds(attempt: int) -> float:
    """Exponential backoff (seconds) for the Nth (1-based) metadata retry, capped."""
    return min(_METADATA_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _METADATA_MAX_BACKOFF_SECONDS)


def _get_ia_session(source_config: SourceConfig) -> ia.ArchiveSession:
    """Build an ArchiveSession honoring optional auth from env vars (PRD §11)."""
    config: dict[str, Any] = {}
    if source_config.auth:
        email = (
            os.environ.get(source_config.auth.email_env)
            if source_config.auth.email_env
            else None
        )
        password = (
            os.environ.get(source_config.auth.password_env)
            if source_config.auth.password_env
            else None
        )
        if email and password:
            config["s3"] = {"access": email, "secret": password}
            config["cookies"] = {"logged-in-user": email}
    # Disable the internetarchive library's own internal retries so they don't stack
    # under our bounded discovery/fetch retry loops (SPEC §spec:slow-metadata).
    http_adapter_kwargs = {"max_retries": 0}
    return (
        ia.get_session(config=config, http_adapter_kwargs=http_adapter_kwargs)
        if config
        else ia.get_session(http_adapter_kwargs=http_adapter_kwargs)
    )


def _file_matches_filters(
    file_meta: dict[str, Any],
    format_filters: list[str],
    exclude_filters: list[str],
) -> bool:
    """Apply config-declared format and exclude filters to one IA file record."""
    name = file_meta.get("name", "")
    if any(fnmatch.fnmatch(name, pat) for pat in exclude_filters):
        return False
    if not format_filters:
        return True
    fmt = (file_meta.get("format") or "").lower()
    name_ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return any(f.lower() == fmt or f.lower() == name_ext for f in format_filters)


def _detect_restriction(item_meta: dict[str, Any]) -> tuple[bool, str | None]:
    """Heuristic for "this item is stream-only / download-disabled" (PRD §6, §13)."""
    if item_meta.get("access-restricted") in (True, "true", "1"):
        return True, item_meta.get("access-restricted-item-reason") or "access-restricted"
    if str(item_meta.get("noindex")).lower() == "true":
        return True, "noindex (item hidden by uploader)"
    return False, None


def _build_manifest(
    files: list[dict[str, Any]],
    format_filters: list[str],
    exclude_filters: list[str],
) -> Manifest:
    """Manifest holds only the files we care to fetch — that's what 'do I have it?' compares."""
    entries: list[ManifestFile] = []
    for f in files:
        if not _file_matches_filters(f, format_filters, exclude_filters):
            continue
        size_raw = f.get("size")
        size = int(size_raw) if size_raw is not None else None
        entries.append(
            ManifestFile(name=f["name"], size=size, md5=f.get("md5"))
        )
    entries.sort(key=lambda e: e.name)
    return Manifest(files=tuple(entries))


def _combine_change_signal(hit: dict[str, Any]) -> str | None:
    """Combine IA's oai_updatedate and item_size into one change signal.

    oai_updatedate is the monotone component the correctness bound leans on: a
    re-derivation (new checksum, possibly identical byte size) bumps it, so it
    catches same-size content changes that item_size alone would miss. The
    signal therefore *requires* oai_updatedate — when the search did not expose
    it we return None so the core falls through to the full manifest comparison
    rather than trusting a size-only signal that is not monotone in the item's
    contents (§spec:incremental-discovery, correctness bound). When present,
    item_size is folded in as a second discriminator.
    """
    updated = hit.get("oai_updatedate")
    # IA returns multi-valued fields as lists; take the most recent.
    if isinstance(updated, list):
        updated = updated[-1] if updated else None
    if updated is None:
        return None
    return f"{updated}|{hit.get('item_size')}"


@register
class IAPlugin(SourcePlugin):
    type_name = "ia"
    template_fields = ("identifier", "title", "creator", "date", "year", "venue", "coverage")

    def __init__(self, source_config: SourceConfig) -> None:
        super().__init__(source_config)
        self._session = _get_ia_session(source_config)

    def enumerate_with_signals(
        self, collection: CollectionConfig
    ) -> Iterator[tuple[str, str | None]]:
        log.info("IA discover: query=%r", collection.query)
        search = self._session.search_items(
            collection.query, fields=["identifier", "oai_updatedate", "item_size"]
        )
        for hit in search:
            yield hit["identifier"], _combine_change_signal(hit)

    def enumerate_items(self, collection: CollectionConfig) -> Iterator[str]:
        for identifier, _signal in self.enumerate_with_signals(collection):
            yield identifier

    def discover(self, collection: CollectionConfig) -> Iterator[ItemDescriptor]:
        for identifier, signal in self.enumerate_with_signals(collection):
            desc = self.describe_item(identifier, collection)
            if desc is not None:
                yield replace(desc, change_signal=signal)

    def describe_item(
        self, identifier: str, collection: CollectionConfig
    ) -> ItemDescriptor | None:
        """Resolve one item's descriptor via a bounded, retrying metadata fetch.

        Carries a bounded read timeout and mirrors the fetch path's retry/back-off
        discipline (SPEC §spec:slow-metadata): a transient fault (slow, 5xx, 429,
        connection reset) is retried up to `_MAX_METADATA_ATTEMPTS` with exponential
        backoff, honoring a source-supplied `Retry-After`. A non-retriable fault, or
        exhausted retries, logs a warning and returns None (the item is skipped for
        this run — identical to the pre-change behavior of a failed get_item), so one
        bad item never fails the whole enumeration.
        """
        item = self._get_item_with_retries(identifier)
        if item is None:
            return None
        files: list[dict[str, Any]] = list(item.files)
        metadata: dict[str, Any] = dict(item.metadata)
        manifest = _build_manifest(files, collection.format_filters, collection.exclude_filters)
        restricted, reason = _detect_restriction(metadata)
        metadata = _normalize_template_fields(identifier, metadata)
        return ItemDescriptor(
            identifier=identifier,
            manifest=manifest,
            metadata=metadata,
            is_restricted=restricted,
            restriction_reason=reason,
        )

    def _get_item_with_retries(self, identifier: str):
        """get_item with a bounded timeout and the fetch path's retry/back-off.

        Returns the IA item, or None if it can't be resolved within the envelope.
        """
        for attempt in range(1, _MAX_METADATA_ATTEMPTS + 1):
            try:
                return self._session.get_item(
                    identifier, request_kwargs={"timeout": _METADATA_TIMEOUT}
                )
            except Exception as e:
                if not _is_retriable(e) or attempt == _MAX_METADATA_ATTEMPTS:
                    log.warning("IA get_item failed for %s: %s", identifier, e)
                    return None
                # Honor a source-supplied Retry-After, but clamp it to the backoff
                # cap: the header is upstream-controlled, and a describe worker holds
                # a shared SourceBudget slot while it sleeps — an unbounded value would
                # let a hostile/compromised endpoint pin slots and wedge the run.
                retry_after = _retry_after_seconds(e)
                delay = (
                    _metadata_backoff_seconds(attempt)
                    if retry_after is None
                    else min(retry_after, _METADATA_MAX_BACKOFF_SECONDS)
                )
                log.warning(
                    "IA get_item attempt %d/%d for %s failed (%s); retrying in %.1fs",
                    attempt, _MAX_METADATA_ATTEMPTS, identifier, e, delay,
                )
                time.sleep(delay)
        return None

    def fetch(
        self,
        item: ItemDescriptor,
        dest_dir: Path,
        format_filters: list[str],
        exclude_filters: list[str],
    ) -> FetchResult:
        """Download manifest files into dest_dir (a core-owned temp directory).

        The plugin only writes bytes and verifies IA's per-file checksums; the
        core owns the temp-dir lifecycle and the atomic rename into the archive
        (SPEC.md §spec:sync-workflow).
        """
        if item.is_restricted:
            return FetchResult(
                success=False,
                bytes_downloaded=0,
                error=f"restricted: {item.restriction_reason}",
            )

        target_files = [f.name for f in item.manifest.files]
        if not target_files:
            return FetchResult(success=True, bytes_downloaded=0)

        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            ia.download(
                item.identifier,
                files=target_files,  # type: ignore[arg-type]  # IA accepts filenames at runtime
                destdir=str(dest_dir),
                # IA places files under <destdir>/<identifier>/...; strip that prefix below.
                no_directory=False,
                ignore_existing=False,
                checksum=True,
                # Core owns retries/backoff (SPEC §spec:failure-behavior); don't
                # let the IA library retry underneath us.
                retries=0,
                archive_session=self._session,
            )
        except Exception as e:
            log.warning("IA download failed for %s: %s", item.identifier, e)
            return FetchResult(
                success=False,
                bytes_downloaded=0,
                error=str(e),
                retriable=_is_retriable(e),
                retry_after=_retry_after_seconds(e),
            )

        # IA's download writes to <dest_dir>/<identifier>/<filename>. Flatten that
        # subtree into dest_dir; the core promotes dest_dir into the archive.
        ia_subdir = dest_dir / item.identifier
        if ia_subdir.is_dir():
            for child in ia_subdir.iterdir():
                shutil.move(str(child), str(dest_dir / child.name))
            ia_subdir.rmdir()

        bytes_downloaded = 0
        missing: list[str] = []
        for mf in item.manifest.files:
            written = dest_dir / mf.name
            if not written.is_file():
                missing.append(mf.name)
                continue
            bytes_downloaded += written.stat().st_size

        if missing:
            # A truncated/partial download is a transient fault worth another attempt.
            return FetchResult(
                success=False,
                bytes_downloaded=bytes_downloaded,
                error=f"missing after fetch: {missing[:5]}",
                retriable=True,
            )

        return FetchResult(success=True, bytes_downloaded=bytes_downloaded)

    def verify(self, item: ItemDescriptor, archive_path: Path) -> VerifyResult:
        """Cheap existence-only verify. Hashing belongs in `verify --deep`."""
        missing = [f.name for f in item.manifest.files if not (archive_path / f.name).is_file()]
        return VerifyResult(ok=not missing, missing_files=missing)


_RETRIABLE_MARKERS = (
    "checksum",
    "rate limit",
    "too many requests",
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "429",
    "502",
    "503",
    "504",
)


def _http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status from a requests-style exception (has .response)."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    return getattr(response, "status_code", None)


def _is_retriable(exc: BaseException) -> bool:
    """Classify a download failure as a transient fault worth a bounded retry.

    Rate limits (429), server errors (5xx), connection resets/timeouts, and
    checksum mismatches are transient; anything else fails fast so a permanent
    fault isn't retried needlessly (SPEC §spec:failure-behavior).
    """
    status = _http_status(exc)
    if status is not None and (status == 429 or 500 <= status < 600):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _RETRIABLE_MARKERS)


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Extract a `Retry-After` delay (seconds) from an HTTP error response, if any.

    Only the delta-seconds form is honored; the HTTP-date form falls back to the
    core's exponential backoff.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _normalize_template_fields(identifier: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Surface a stable set of template-friendly fields alongside raw IA metadata."""
    out = dict(metadata)
    out.setdefault("identifier", identifier)
    date = metadata.get("date") or ""
    if date and "year" not in out:
        out["year"] = date.split("-", 1)[0] if "-" in date else date[:4]
    if "venue" not in out and "coverage" in out:
        out["venue"] = out["coverage"]
    return out
