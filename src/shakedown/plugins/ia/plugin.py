"""Internet Archive source plugin. Wraps the official `internetarchive` package."""
from __future__ import annotations

import fnmatch
import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import internetarchive as ia

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import Manifest, ManifestFile
from shakedown.plugins.base import FetchResult, ItemDescriptor, SourcePlugin, VerifyResult
from shakedown.plugins.registry import register

log = logging.getLogger(__name__)


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
    return ia.get_session(config=config) if config else ia.get_session()


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


@register
class IAPlugin(SourcePlugin):
    type_name = "ia"
    template_fields = ("identifier", "title", "creator", "date", "year", "venue", "coverage")

    def __init__(self, source_config: SourceConfig) -> None:
        super().__init__(source_config)
        self._session = _get_ia_session(source_config)

    def discover(self, collection: CollectionConfig) -> Iterator[ItemDescriptor]:
        """Enumerate items matching `collection.query` against IA's advanced search."""
        log.info("IA discover: query=%r", collection.query)
        search = self._session.search_items(collection.query, fields=["identifier"])
        for hit in search:
            identifier = hit["identifier"]
            try:
                item = self._session.get_item(identifier)
            except Exception as e:
                log.warning("IA get_item failed for %s: %s", identifier, e)
                continue

            files: list[dict[str, Any]] = list(item.files)
            metadata: dict[str, Any] = dict(item.metadata)
            manifest = _build_manifest(files, collection.format_filters, collection.exclude_filters)
            restricted, reason = _detect_restriction(metadata)
            metadata = _normalize_template_fields(identifier, metadata)

            yield ItemDescriptor(
                identifier=identifier,
                manifest=manifest,
                metadata=metadata,
                is_restricted=restricted,
                restriction_reason=reason,
            )

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

        files_written: list[Path] = []
        bytes_downloaded = 0
        missing: list[str] = []
        for mf in item.manifest.files:
            written = dest_dir / mf.name
            if not written.is_file():
                missing.append(mf.name)
                continue
            files_written.append(written)
            bytes_downloaded += written.stat().st_size

        if missing:
            # A truncated/partial download is a transient fault worth another attempt.
            return FetchResult(
                success=False,
                bytes_downloaded=bytes_downloaded,
                error=f"missing after fetch: {missing[:5]}",
                retriable=True,
            )

        return FetchResult(
            success=True, bytes_downloaded=bytes_downloaded, files_written=files_written
        )

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
