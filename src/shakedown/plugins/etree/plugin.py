"""Source plugin for the etree Live Music Archive on archive.org.

This plugin talks to archive.org's public HTTP/JSON API directly (via ``httpx``),
which is a distinct integration path from the official ``internetarchive`` Python
package. It uses three endpoints:

- ``GET /advancedsearch.php`` to enumerate item identifiers for a query.
- ``GET /metadata/<identifier>`` for an item's metadata and file list.
- ``GET /download/<identifier>/<name>`` for a file's bytes.

Everything hard (temp dirs, atomic promotion, retries/backoff, lookup isolation,
file selection, completeness guard) lives in the core; this plugin is a thin
adapter that implements ``list_items``, ``lookup_item``, ``fetch`` and ``verify``.
"""
from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import ManifestFile
from shakedown.plugins.base import (
    FetchResult,
    ItemLookup,
    ListedItem,
    SourcePlugin,
    VerifyResult,
    http_status,
    retry_after_seconds,
    reusable_leftover,
)
from shakedown.plugins.registry import register
from shakedown.selection import DOWNLOAD_REFUSED_REASON

_BASE_URL = "https://archive.org"
_PAGE_SIZE = 100
_TIMEOUT = 60.0
# Hard ceiling on enumeration pages, so a source that reports an inflated
# numFound while always returning a non-empty page cannot loop forever.
_MAX_PAGES = 10_000


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


def _first(meta: dict[str, Any], key: str) -> Any:
    """archive.org metadata values may be a scalar or a list; return the first."""
    value = meta.get(key)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _restriction_reason(meta: dict[str, Any]) -> str | None:
    """Why the whole item is access-restricted or dark, or None.

    ``access-restricted-item`` is not an item restriction: archive.org sets it on
    stream-only shows whose MP3s stay public, and marks each refused file
    ``private`` — the per-file verdict the core's selection honours."""
    if str(_first(meta, "access-restricted")).lower() == "true":
        return "access-restricted"
    curation = _first(meta, "curation")
    if curation and "dark" in str(curation).lower():
        return "dark"
    return None


def _classify(exc: httpx.HTTPError) -> tuple[bool, float | None, str]:
    """Classify an httpx error into (retriable, retry_after, message).

    Transient: 429, 5xx, connection reset, timeout. Permanent: other 4xx.
    Honors a numeric ``Retry-After`` when present.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retriable = status == 429 or 500 <= status < 600
        return retriable, retry_after_seconds(exc), f"HTTP {status}"
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
    upstream_host = "archive.org"
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
    # `identifier` is the etree recording's archive.org identifier — unique per item
    # (§spec:layout-collision-safety); the other fields (date, venue, ...) repeat across
    # the multiple recordings of a single show.
    per_item_unique_fields = ("identifier",)

    def __init__(self, source_config: SourceConfig) -> None:
        super().__init__(source_config)
        # Plain instance attribute so a test can swap in an httpx.MockTransport
        # client. The core constructs one plugin instance per source.
        self._client = httpx.Client(
            base_url=_BASE_URL,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )

    # -- list / look up ------------------------------------------------------

    def list_items(self, collection: CollectionConfig) -> Iterator[ListedItem]:
        for identifier in self._enumerate(collection.query):
            yield ListedItem(identifier)

    def lookup_item(self, identifier: str, timeout: float) -> ItemLookup:
        resp = self._client.get(f"/metadata/{quote(identifier)}", timeout=timeout)
        resp.raise_for_status()
        doc = resp.json()
        meta = doc.get("metadata", {}) or {}
        date = _first(meta, "date")
        return ItemLookup(
            metadata={
                "identifier": identifier,
                "title": _first(meta, "title"),
                "creator": _first(meta, "creator"),
                "date": date,
                "year": str(date)[:4] if date else None,
                "venue": _first(meta, "venue"),
                "coverage": _first(meta, "coverage"),
            },
            files=[
                {
                    "name": f.get("name"),
                    "size": f.get("size"),
                    "md5": f.get("md5"),
                    "format": f.get("format"),
                    "refused": str(f.get("private")).lower() == "true",
                    "derived_from": f.get("original"),
                    "bitrate": f.get("bitrate"),
                }
                for f in doc.get("files", []) or []
            ],
            restriction_reason=_restriction_reason(meta),
        )

    def _enumerate(self, query: str) -> Iterator[str]:
        """Yield every matching identifier, paginating advancedsearch.php.

        Raises on any enumeration failure so the core flags the collection stale
        and retains existing items, rather than treating an error as "everything
        disappeared". A listing that ends before the search's own ``numFound`` (an
        early empty page, or the page ceiling) is such a failure: the unlisted
        shows are still upstream, and reading the short list as complete would
        mark them disappeared or prune them.
        """
        page = 1
        seen: set[str] = set()
        num_found = 0
        while True:
            resp = self._client.get(
                "/advancedsearch.php",
                params={
                    "q": query,
                    "fl[]": "identifier",
                    # A stable order keeps paging from skipping or repeating items.
                    "sort[]": "identifier asc",
                    "rows": _PAGE_SIZE,
                    "page": page,
                    "output": "json",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            response = body.get("response", {}) or {}
            # Keep the largest total seen: a total that falls mid-listing (an item
            # removed upstream) must not lower the completeness bar.
            num_found = max(num_found, int(response.get("numFound", 0) or 0))
            docs = response.get("docs", []) or []
            if not docs:
                break
            for doc in docs:
                identifier = doc.get("identifier")
                # Count unique ids: a repeated row must not stand in for a skipped one.
                if identifier and identifier not in seen:
                    seen.add(identifier)
                    yield identifier
            if len(seen) >= num_found:
                return
            page += 1
            if page > _MAX_PAGES:
                # Bound a source that reports an inflated numFound while never
                # returning an empty page.
                break
        if len(seen) < num_found:
            raise RuntimeError(
                f"etree search for {query!r} ended early: "
                f"listed {len(seen)} of {num_found} items"
            )

    # -- fetch --------------------------------------------------------------

    def fetch(
        self, identifier: str, files: Sequence[ManifestFile], dest_dir: Path
    ) -> FetchResult:
        total = 0
        for mf in files:
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

            # A file an earlier attempt left behind is kept only once it matches the
            # manifest's size and md5; one we cannot check is fetched again.
            if reusable_leftover(out, mf):
                total += out.stat().st_size
                continue

            # Stream to disk while hashing, so a multi-GB file never lands in
            # memory in full (etree FLAC sets are large and fetches run
            # concurrently).
            hasher = hashlib.md5()
            written = 0
            try:
                with self._client.stream(
                    "GET", f"/download/{quote(identifier)}/{quote(mf.name)}"
                ) as resp:
                    resp.raise_for_status()
                    with out.open("wb") as fh:
                        for chunk in resp.iter_bytes():
                            fh.write(chunk)
                            hasher.update(chunk)
                            written += len(chunk)
            except httpx.HTTPError as exc:
                if http_status(exc) == 403:
                    # archive.org serves some shows stream-only with no restriction
                    # flag in their metadata; the refusal is the only signal.
                    return FetchResult(
                        success=False,
                        bytes_downloaded=total,
                        error=DOWNLOAD_REFUSED_REASON,
                        unavailable=True,
                    )
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

    def verify(self, files: Sequence[ManifestFile], archive_path: Path) -> VerifyResult:
        missing = [f.name for f in files if not (archive_path / f.name).is_file()]
        return VerifyResult(ok=not missing, missing_files=missing)
