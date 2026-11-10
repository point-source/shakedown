"""Internet Archive source plugin. Wraps the official `internetarchive` package."""
from __future__ import annotations

import fnmatch
import logging
import os
import time
from collections.abc import Iterator
from dataclasses import replace
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import internetarchive as ia
from internetarchive.auth import S3Auth
from urllib3 import Retry

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import Manifest, ManifestFile
from shakedown.plugins.base import (
    FetchResult,
    ItemDescriptor,
    SourcePlugin,
    VerifyResult,
    downloaded_file_mismatch,
    reusable_leftover,
)
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

# File downloads: one request per file per attempt; the core retries the fetch
# (SPEC §spec:failure-behavior). The read timeout bounds a stalled transfer.
_DOWNLOAD_TIMEOUT = (_METADATA_CONNECT_TIMEOUT, _METADATA_READ_TIMEOUT)
_DOWNLOAD_CHUNK = 1024 * 1024

# The search has no shakedown retry loop around it, so the session's own retry is
# its one layer: bounded, and honoring a Retry-After only up to the metadata cap.
_SEARCH_RETRIES = 3


def _search_retry() -> Retry:
    return Retry(
        total=_SEARCH_RETRIES,
        connect=_SEARCH_RETRIES,
        read=_SEARCH_RETRIES,
        redirect=False,
        allowed_methods=["GET", "POST"],
        status_forcelist=[429, 500, 502, 503, 504],
        backoff_factor=_METADATA_BACKOFF_BASE_SECONDS,
        backoff_max=_METADATA_MAX_BACKOFF_SECONDS,
        respect_retry_after_header=True,
        retry_after_max=int(_METADATA_MAX_BACKOFF_SECONDS),
    )


# Recorded as the item's restriction_reason when archive.org refuses a download.
DOWNLOAD_REFUSED_REASON = "download refused by archive.org (stream-only or restricted)"
# Recorded on a show mirrored from the collection's fallback formats instead.
FALLBACK_REASON = "lossless refused by archive.org; MP3 copy mirrored instead"


def _metadata_backoff_seconds(attempt: int) -> float:
    """Exponential backoff (seconds) for the Nth (1-based) metadata retry, capped."""
    return min(_METADATA_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), _METADATA_MAX_BACKOFF_SECONDS)


def _get_ia_session(source_config: SourceConfig) -> ia.ArchiveSession:
    """Build an ArchiveSession honoring optional auth from env vars (PRD §11), with the
    urllib3 retries on its archive.org adapter turned off."""
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


def _is_private(file_meta: dict[str, Any]) -> bool:
    """archive.org's per-file verdict: a `private` file answers 403 (e.g. the lossless
    originals of a stream-only show, whose MP3s stay public)."""
    return str(file_meta.get("private")).lower() == "true"


def _build_manifest(
    files: list[dict[str, Any]],
    format_filters: list[str],
    exclude_filters: list[str],
) -> Manifest:
    """Manifest holds only the files we care to fetch and archive.org will serve —
    that's what 'do I have it?' compares."""
    entries: list[ManifestFile] = []
    for f in files:
        if _is_private(f) or not _file_matches_filters(f, format_filters, exclude_filters):
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
    upstream_host = "archive.org"
    template_fields = ("identifier", "title", "creator", "date", "year", "venue", "coverage")
    # `identifier` is the item's IA identifier — globally unique, the composite key
    # (§spec:layout-collision-safety); no other surfaced field is guaranteed distinct.
    per_item_unique_fields = ("identifier",)

    def __init__(self, source_config: SourceConfig) -> None:
        super().__init__(source_config)
        # Exactly one retry layer per request (SPEC §spec:failure-behavior). The
        # library remounts its own retries on the session every search, so searches
        # get a session of their own. Metadata lookups and downloads go through
        # `_session` as plain requests, retried only by shakedown.
        self._session = _get_ia_session(source_config)
        self._search_session = _get_ia_session(source_config)

    def enumerate_with_signals(
        self, collection: CollectionConfig
    ) -> Iterator[tuple[str, str | None]]:
        log.info("IA discover: query=%r", collection.query)
        search = self._search_session.search_items(
            collection.query,
            fields=["identifier", "oai_updatedate", "item_size"],
            max_retries=_search_retry(),
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
        item = self._get_metadata_with_retries(identifier)
        if item is None:
            return None
        files: list[dict[str, Any]] = list(item.get("files") or [])
        metadata: dict[str, Any] = dict(item.get("metadata") or {})
        manifest = _build_manifest(files, collection.format_filters, collection.exclude_filters)
        restricted, reason = _detect_restriction(metadata)
        if not restricted and not manifest.files and any(
            _is_private(f)
            and _file_matches_filters(f, collection.format_filters, collection.exclude_filters)
            for f in files
        ):
            # Every wanted file is one archive.org refuses to serve: mirror the
            # collection's fallback formats if it opted in and they are served.
            fallback = (
                _build_manifest(
                    files, collection.fallback_format_filters, collection.exclude_filters
                )
                if collection.fallback_format_filters
                else manifest
            )
            if fallback.files:
                manifest, reason = fallback, FALLBACK_REASON
            else:
                restricted, reason = True, DOWNLOAD_REFUSED_REASON
        metadata = _normalize_template_fields(identifier, metadata)
        return ItemDescriptor(
            identifier=identifier,
            manifest=manifest,
            metadata=metadata,
            is_restricted=restricted,
            restriction_reason=reason,
        )

    def _auth(self) -> S3Auth | None:
        if self._session.access_key and self._session.secret_key:
            return S3Auth(self._session.access_key, self._session.secret_key)
        return None

    def _url(self, path: str) -> str:
        return f"{self._session.protocol}//{self._session.host}/{path}"

    def _get_metadata(self, identifier: str) -> dict[str, Any]:
        """One Metadata API request. Unlike the library's get_item, an HTTP error keeps
        its response, so the retry below sees the status and any Retry-After."""
        response = self._session.get(
            self._url(f"metadata/{quote(identifier)}"),
            timeout=_METADATA_TIMEOUT,
            auth=self._auth(),
        )
        response.raise_for_status()
        return response.json()

    def _get_metadata_with_retries(self, identifier: str) -> dict[str, Any] | None:
        """The item's metadata with a bounded timeout and the fetch path's retry/back-off.

        Returns the Metadata API response, or None if it can't be resolved within the
        envelope.
        """
        for attempt in range(1, _MAX_METADATA_ATTEMPTS + 1):
            try:
                return self._get_metadata(identifier)
            except Exception as e:
                if not _is_retriable(e) or attempt == _MAX_METADATA_ATTEMPTS:
                    log.warning("IA metadata lookup failed for %s: %s", identifier, e)
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
                    "IA metadata attempt %d/%d for %s failed (%s); retrying in %.1fs",
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

        The plugin only writes bytes and checks each file's size and md5 against the
        manifest; the core owns the temp-dir lifecycle and the atomic rename into the
        archive (SPEC.md §spec:sync-workflow).
        """
        if item.is_restricted:
            return FetchResult(
                success=False,
                bytes_downloaded=0,
                error=f"restricted: {item.restriction_reason}",
            )

        if not item.manifest.files:
            # Zero files upstream (darked item, glitch, filter matches nothing) is not
            # an empty success: the core would promote an empty folder over the copy.
            return FetchResult(
                success=False, bytes_downloaded=0, error="source lists no files to download"
            )

        dest_dir.mkdir(parents=True, exist_ok=True)
        # Files an earlier attempt (or a killed run) left in dest_dir are kept once
        # they match the manifest's size and md5; download only the rest. Any other
        # leftover is removed first so the library never resumes it blindly.
        to_fetch = [
            mf for mf in item.manifest.files if not reusable_leftover(dest_dir / mf.name, mf)
        ]
        for mf in to_fetch:
            (dest_dir / mf.name).unlink(missing_ok=True)

        try:
            for mf in to_fetch:
                self._download_file(item.identifier, mf.name, dest_dir / mf.name)
        except Exception as e:
            if _http_status(e) == 403:
                # archive.org serves some shows stream-only (often soundboards) with no
                # restriction flag in their metadata; the refusal is the only signal.
                log.info("IA download refused for %s: %s", item.identifier, e)
                return FetchResult(
                    success=False,
                    bytes_downloaded=0,
                    error=DOWNLOAD_REFUSED_REASON,
                    unavailable=True,
                )
            log.warning("IA download failed for %s: %s", item.identifier, e)
            return FetchResult(
                success=False,
                bytes_downloaded=0,
                error=str(e),
                retriable=_is_retriable(e),
                retry_after=_retry_after_seconds(e),
            )

        # Every manifest file is now on disk: reused above or just written.
        bytes_downloaded = sum((dest_dir / mf.name).stat().st_size for mf in item.manifest.files)

        # Check every downloaded file against the manifest (reused leftovers were
        # checked above). A corrupt file is removed and the fetch retried.
        for mf in to_fetch:
            mismatch = downloaded_file_mismatch(dest_dir / mf.name, mf)
            if mismatch is not None:
                (dest_dir / mf.name).unlink()
                return FetchResult(
                    success=False,
                    bytes_downloaded=bytes_downloaded,
                    error=mismatch,
                    retriable=True,
                )

        return FetchResult(success=True, bytes_downloaded=bytes_downloaded)

    def _download_file(self, identifier: str, name: str, path: Path) -> None:
        """Stream one file straight to its place in the temp dir, so every finished
        file sits where the next attempt looks for it, even if the run is killed.

        One plain request: the library's own download retries at least twice under
        any setting and remounts retries on the session, so it is not used here.
        """
        with self._session.get(
            self._url(f"download/{quote(identifier)}/{quote(name)}"),
            params={"cnt": "0"},  # don't count mirroring as views, like the library
            stream=True,
            timeout=_DOWNLOAD_TIMEOUT,
            auth=self._auth(),
        ) as response:
            response.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=_DOWNLOAD_CHUNK):
                    fh.write(chunk)
            last_modified = response.headers.get("Last-Modified")
        if last_modified:
            # Keep the upstream timestamp, as the library did.
            try:
                mtime = parsedate_to_datetime(last_modified).timestamp()
            except (TypeError, ValueError):
                return
            os.utime(path, (mtime, mtime))

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
    # A file cut off mid-stream ("Connection broken: IncompleteRead(...)",
    # "Response ended prematurely").
    "connection broken",
    "ended prematurely",
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
