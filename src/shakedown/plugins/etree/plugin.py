"""Source plugin for the etree Live Music Archive on archive.org.

This plugin talks to archive.org's public HTTP/JSON API directly (via ``httpx``),
which is a distinct integration path from the official ``internetarchive`` Python
package. It uses three endpoints:

- ``GET /advancedsearch.php`` to enumerate item identifiers for a query.
- ``GET /metadata/<identifier>`` for an item's metadata and file list.
- ``GET /download/<identifier>/<name>`` for a file's bytes.

Everything hard about durability (temp dirs, atomic promotion, retries/backoff,
completeness guard) lives in the core; this plugin is a thin adapter that
implements ``discover``, ``fetch`` and ``verify``.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import Manifest, ManifestFile
from shakedown.plugins.base import (
    FetchResult,
    ItemDescriptor,
    SourcePlugin,
    VerifyResult,
)
from shakedown.plugins.registry import register

_BASE_URL = "https://archive.org"
_PAGE_SIZE = 100
_TIMEOUT = 60.0
# Hard ceiling on enumeration pages, so a source that reports an inflated
# numFound while always returning a non-empty page cannot loop forever.
_MAX_PAGES = 10_000


def _keep(name: str, fmt: str, formats: list[str], excludes: list[str]) -> bool:
    """Filter one file: excludes (fnmatch globs) first, then a format/ext keep-list.

    Matches the semantics documented in docs/plugins.md so that every plugin
    filters identically.
    """
    if any(fnmatch.fnmatch(name, pat) for pat in excludes):
        return False
    if not formats:
        return True
    fmt = (fmt or "").lower()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return any(f.lower() == fmt or f.lower() == ext for f in formats)


def _coerce_size(raw: Any) -> int | None:
    """archive.org reports file sizes as strings; coerce to int or None."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_contained(base: Path, name: str) -> bool:
    """True iff joining ``name`` onto ``base`` stays within ``base``.

    Manifest file names come from the remote source and are joined onto the
    core-owned temp dir before writing; reject any that would escape it via a
    ``..`` component, an absolute path, or a separator trick. Lexical
    (``normpath``) check — it never touches the filesystem.
    """
    base_n = os.path.normpath(base)
    target = os.path.normpath(base / name)
    # Must land strictly *inside* base: a name that normalizes back to base
    # itself (empty, ".", trailing slash) is not a real file and is rejected.
    return target.startswith(base_n + os.sep)


def _sanitize_identifier(identifier: str) -> str:
    """Reduce an upstream id to a single safe path segment.

    The core uses the identifier as an archive subdirectory name and rejects any
    that would escape the archive tree, so collapse separators here.
    """
    seg = identifier.replace("/", "_").replace("\\", "_")
    seg = seg.strip().strip(".")
    return seg or identifier


def _first(meta: dict[str, Any], key: str) -> Any:
    """archive.org metadata values may be a scalar or a list; return the first."""
    value = meta.get(key)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _is_restricted(meta: dict[str, Any]) -> tuple[bool, str | None]:
    """Detect an access-restricted / dark item from archive.org metadata flags."""
    if str(_first(meta, "access-restricted-item")).lower() == "true":
        return True, "access-restricted-item"
    if str(_first(meta, "access-restricted")).lower() == "true":
        return True, "access-restricted"
    curation = _first(meta, "curation")
    if curation and "dark" in str(curation).lower():
        return True, "dark"
    return False, None


def _classify(exc: httpx.HTTPError) -> tuple[bool, float | None, str]:
    """Classify an httpx error into (retriable, retry_after, message).

    Transient: 429, 5xx, connection reset, timeout. Permanent: other 4xx.
    Honors a numeric ``Retry-After`` when present.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retriable = status == 429 or 500 <= status < 600
        retry_after: float | None = None
        raw = exc.response.headers.get("Retry-After")
        if raw is not None:
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                retry_after = None
        return retriable, retry_after, f"HTTP {status}"
    if isinstance(exc, httpx.TimeoutException):
        return True, None, f"timeout: {exc}"
    if isinstance(exc, httpx.TransportError):
        # Connection reset and similar transport-level faults are transient.
        return True, None, f"transport error: {exc}"
    return False, None, str(exc)


@register
class EtreePlugin(SourcePlugin):
    """Talks to the etree Live Music Archive via archive.org's public JSON API."""

    type_name = "etree"
    #: HTTP client, a plain instance attribute so tests may substitute one backed
    #: by httpx.MockTransport.
    _client: httpx.Client
    template_fields = (
        "identifier",
        "title",
        "creator",
        "date",
        "year",
        "venue",
        "coverage",
    )

    def __init__(self, source_config: SourceConfig) -> None:
        super().__init__(source_config)
        # Plain instance attribute so a test can swap in an httpx.MockTransport
        # client. The core constructs one plugin instance per source.
        self._client = httpx.Client(
            base_url=_BASE_URL,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )

    # -- discover -----------------------------------------------------------

    def discover(self, collection: CollectionConfig) -> Iterator[ItemDescriptor]:
        for identifier in self._enumerate(collection.query):
            doc = self._metadata(identifier)
            meta = doc.get("metadata", {}) or {}
            files_raw = doc.get("files", []) or []

            files = tuple(
                ManifestFile(
                    name=f["name"],
                    size=_coerce_size(f.get("size")),
                    md5=f.get("md5"),
                )
                for f in files_raw
                if "name" in f
                and _keep(
                    f["name"],
                    f.get("format", ""),
                    collection.format_filters,
                    collection.exclude_filters,
                )
            )

            date = _first(meta, "date")
            year = str(date)[:4] if date else None
            metadata: dict[str, Any] = {
                "identifier": identifier,
                "title": _first(meta, "title"),
                "creator": _first(meta, "creator"),
                "date": date,
                "year": year,
                "venue": _first(meta, "venue"),
                "coverage": _first(meta, "coverage"),
            }

            restricted, reason = _is_restricted(meta)
            yield ItemDescriptor(
                identifier=_sanitize_identifier(identifier),
                manifest=Manifest(files=files),
                metadata=metadata,
                is_restricted=restricted,
                restriction_reason=reason,
            )

    def _enumerate(self, query: str) -> Iterator[str]:
        """Yield every matching identifier, paginating advancedsearch.php.

        Raises on any enumeration failure so the core flags the collection stale
        and retains existing items, rather than treating an error as "everything
        disappeared".
        """
        page = 1
        seen = 0
        while True:
            resp = self._client.get(
                "/advancedsearch.php",
                params={
                    "q": query,
                    "fl[]": "identifier",
                    "rows": _PAGE_SIZE,
                    "page": page,
                    "output": "json",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            response = body.get("response", {}) or {}
            num_found = int(response.get("numFound", 0) or 0)
            docs = response.get("docs", []) or []
            if not docs:
                break
            for doc in docs:
                identifier = doc.get("identifier")
                if identifier:
                    yield identifier
                    seen += 1
            if seen >= num_found:
                break
            page += 1
            if page > _MAX_PAGES:
                # Bound a source that reports an inflated numFound while never
                # returning an empty page.
                break

    def _metadata(self, identifier: str) -> dict[str, Any]:
        resp = self._client.get(f"/metadata/{identifier}")
        resp.raise_for_status()
        return resp.json()

    # -- fetch --------------------------------------------------------------

    def fetch(
        self,
        item: ItemDescriptor,
        dest_dir: Path,
        format_filters: list[str],
        exclude_filters: list[str],
    ) -> FetchResult:
        if item.is_restricted:
            # Fail fast, permanently: a restricted item will never download.
            return FetchResult(
                success=False,
                bytes_downloaded=0,
                error=f"restricted: {item.restriction_reason}",
                retriable=False,
            )

        total = 0
        for mf in item.manifest.files:
            # Trust boundary: the file name comes from the remote source. Refuse
            # to write anything that would escape the item directory. The core
            # enforces this too, but guarding here keeps the plugin safe on its
            # own and fails the item loudly rather than writing out of tree.
            if not _is_contained(dest_dir, mf.name):
                return FetchResult(
                    success=False,
                    bytes_downloaded=total,
                    error=f"unsafe file name escapes item directory: {mf.name!r}",
                    retriable=False,
                )

            out = dest_dir / mf.name
            out.parent.mkdir(parents=True, exist_ok=True)

            # Idempotent: a file already present with the expected size is kept.
            if out.is_file() and (mf.size is None or out.stat().st_size == mf.size):
                total += out.stat().st_size
                continue

            # Stream to disk while hashing, so a multi-GB file never lands in
            # memory in full (etree FLAC sets are large and fetches run
            # concurrently).
            hasher = hashlib.md5()
            written = 0
            try:
                with self._client.stream(
                    "GET", f"/download/{item.identifier}/{mf.name}"
                ) as resp:
                    resp.raise_for_status()
                    with out.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
                            hasher.update(chunk)
                            written += len(chunk)
            except httpx.HTTPError as exc:
                retriable, retry_after, message = _classify(exc)
                return FetchResult(
                    success=False,
                    bytes_downloaded=total,
                    error=f"{mf.name}: {message}",
                    retriable=retriable,
                    retry_after=retry_after,
                )

            if mf.md5 is not None and hasher.hexdigest() != mf.md5:
                # A checksum mismatch is a transient fault worth another attempt.
                # Drop the corrupt partial so it can't be mistaken for complete.
                out.unlink(missing_ok=True)
                return FetchResult(
                    success=False,
                    bytes_downloaded=total,
                    error=f"checksum mismatch for {mf.name}",
                    retriable=True,
                )

            total += written

        return FetchResult(success=True, bytes_downloaded=total)

    # -- verify -------------------------------------------------------------

    def verify(self, item: ItemDescriptor, archive_path: Path) -> VerifyResult:
        missing = [
            f.name
            for f in item.manifest.files
            if not (archive_path / f.name).is_file()
        ]
        return VerifyResult(ok=not missing, missing_files=missing)
