"""Source plugin contract. PRD §7."""
from __future__ import annotations

import hashlib
import math
import os
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import ManifestFile

_HASH_CHUNK = 1024 * 1024


def downloaded_file_mismatch(path: Path, expected: ManifestFile) -> str | None:
    """Check a just-downloaded file in the core-owned temp dir against the source's
    manifest entry: the size, then the md5, whichever the source reported. Returns a
    reason on mismatch, None when the file matches (or the source gave nothing to
    check against).

    This is the arrival check (SPEC §spec:sync-workflow, step 3). Use it only on files
    in the fetch temp dir — never on the archive, which library tools may retag in
    place (§spec:sync-identity).
    """
    size = path.stat().st_size
    if expected.size is not None and size != expected.size:
        return f"size mismatch for {expected.name}: expected {expected.size}, got {size}"
    if expected.md5 is not None:
        digest = hashlib.md5()
        with path.open("rb") as fh:
            while chunk := fh.read(_HASH_CHUNK):
                digest.update(chunk)
        if digest.hexdigest() != expected.md5.lower():
            return f"checksum mismatch for {expected.name}"
    return None


def http_status(exc: BaseException) -> int | None:
    """The HTTP status of a requests- or httpx-style error (both carry .response)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def retry_after_seconds(exc: BaseException) -> float | None:
    """The delta-seconds `Retry-After` an HTTP error response asked for, if any.

    The HTTP-date form, and any value that is not a finite, non-negative number of
    seconds, count as no header: the core then uses its own backoff.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        seconds = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def reusable_leftover(path: Path, expected: ManifestFile) -> bool:
    """True iff a file an earlier fetch attempt left in the temp dir can be kept: it
    exists and matches the manifest's size and md5. A file the manifest gives nothing
    to check against is never reused."""
    return (
        path.is_file()
        and (expected.size is not None or expected.md5 is not None)
        and downloaded_file_mismatch(path, expected) is None
    )


@dataclass(frozen=True)
class ListedItem:
    """One row of a collection listing (list_items).

    ``change_signal`` is the source's cheap token that moves whenever the item's
    contents change (§spec:incremental-discovery); None when the source has none.
    """
    identifier: str
    change_signal: str | None = None


@dataclass(frozen=True)
class ItemLookup:
    """A plugin's raw answer for one item (lookup_item). The core validates it.

    - ``metadata`` — the item's metadata, with the plugin's ``template_fields`` filled.
    - ``files`` — every file the source lists, each a mapping with these keys:
      ``name`` (str, required), ``size`` (bytes, int or digit string), ``md5`` (hex
      checksum), ``format`` (the source's format name), ``refused`` (True when the
      source will not serve the file), ``derived_from`` (the original file's name, or a
      list of names, when this file is a derivative), ``bitrate`` (kbps, when the
      source lists it), ``title`` and ``track`` (the file's track title and number,
      used to fill tags).
      Only ``name`` is required; pass the source's values through without checking
      them.
    - ``restriction_reason`` — set when the source restricts the whole item.
    """
    metadata: Mapping[str, Any]
    files: Sequence[Mapping[str, Any]]
    restriction_reason: str | None = None


@dataclass
class FetchResult:
    """Outcome of fetch() for a single item.

    On failure, plugins classify whether the fault is transient so the core can
    apply bounded retries with backoff (SPEC.md §spec:failure-behavior):
    - `retriable` — a checksum mismatch, truncated download, rate limit, or other
      transient fault worth another attempt. Permanent faults (restricted item,
      malformed metadata) leave it False so the core fails fast.
    - `retry_after` — seconds the source asked us to wait (e.g. an HTTP
      `Retry-After` on a 429). The core honors it in place of its own backoff.
    - `unavailable` — the source refuses to serve the item (e.g. an HTTP 403 on a
      stream-only item whose metadata carries no restriction flag). The core records
      the item `unavailable` with `error` as the reason, keeps any archive copy, and
      does not count it as a failure.
    """
    success: bool
    bytes_downloaded: int
    error: str | None = None
    retriable: bool = False
    retry_after: float | None = None
    unavailable: bool = False


@dataclass
class VerifyResult:
    """Outcome of cheap verify() for a single item.

    `ok` means: every file the manifest expects is present on disk.
    Plugins MUST NOT hash bytes here — that's `verify --deep`'s job.
    """
    ok: bool
    missing_files: list[str] = field(default_factory=list)


@dataclass
class ProbeResult:
    """Outcome of a non-mutating readiness probe (§spec:setup-readiness-validation).

    ``ok`` is the only thing validation branches on. ``consequence`` and ``action``
    are the plain-language "what breaks" and "what to do" strings surfaced verbatim in
    ``shakedown validate`` output — populated only on failure.
    """
    ok: bool
    consequence: str | None = None
    action: str | None = None


class SourcePlugin(ABC):
    """Abstract source. One instance per (source, plugin) pair."""

    #: Identifier used in `type:` config keys (e.g., "ia").
    type_name: str

    #: The host this plugin's requests go to. Sources whose plugins name the same
    #: host share one concurrency budget (SPEC §spec:source-budget). None: the
    #: source gets a budget of its own.
    upstream_host: str | None = None

    def __init__(self, source_config: SourceConfig) -> None:
        self.source_config = source_config

    @abstractmethod
    def list_items(self, collection: CollectionConfig) -> Iterator[ListedItem]:
        """List the collection: every item currently matching its query, with the
        source's change signal when it has one.

        Raising means the listing failed: the core marks the collection stale and
        prunes nothing. The listing must be complete or raise — a short list reads
        as items having left the collection.
        """

    @abstractmethod
    def lookup_item(self, identifier: str, timeout: float) -> ItemLookup:
        """Look up one item: its metadata, any item-level restriction, and every file
        the source lists for it.

        Make exactly one request, timing out after ``timeout`` seconds, and let any
        error propagate. The core owns retries, validation, file selection, and
        isolating a failed lookup to this item (SPEC §spec:per-item-isolation).
        """

    @abstractmethod
    def fetch(
        self, identifier: str, files: Sequence[ManifestFile], dest_dir: Path
    ) -> FetchResult:
        """Download the given files of one item into dest_dir, verifying source
        checksums.

        `dest_dir` is a fresh, core-owned temporary directory. The plugin only
        writes the item's files there and reports the outcome; it MUST NOT touch
        the final archive location. The core performs the atomic rename of
        `dest_dir` into the archive on success and sweeps it on failure, so no
        plugin can commit partial state to the archive (SPEC.md §spec:sync-workflow).

        `dest_dir` may hold files an earlier failed attempt left behind (the core
        keeps them so one late error doesn't re-download a whole show). Keep such a
        file only when `reusable_leftover` accepts it; fetch anything else.
        """

    @abstractmethod
    def verify(self, files: Sequence[ManifestFile], archive_path: Path) -> VerifyResult:
        """Cheap re-check: every given file present on disk."""

    # -- readiness probes (§spec:setup-readiness-validation) ----------------
    # Both are strictly non-mutating: they read from the source but never download a
    # full item, write to the archive/library, or leave state behind. `shakedown
    # validate` calls them once per configured source/collection before a real mirror.

    def check_credentials(self) -> ProbeResult:
        """Readiness probe for required credential environment variables.

        Default: a source with no ``auth`` block needs no credentials (pass); otherwise
        every configured credential env var must be present and non-empty. Acceptance —
        whether the source *honors* the credentials — is exercised by
        :meth:`check_reachable`, whose listing runs under the authenticated session,
        so a rejected key surfaces there with the source's own error text. Plugins that
        can cheaply confirm acceptance out-of-band may override.
        """
        auth = self.source_config.auth
        if auth is None:
            return ProbeResult(ok=True)
        missing = [
            name
            for name in (auth.email_env, auth.password_env)
            if name and not os.environ.get(name)
        ]
        if missing:
            return ProbeResult(
                ok=False,
                consequence=(
                    f"required credential environment variable(s) {missing} are not set, "
                    f"so authenticated access to source {self.source_config.name!r} will fail"
                ),
                action="set the credential environment variables before syncing",
            )
        return ProbeResult(ok=True)

    def check_reachable(self, collection: CollectionConfig) -> ProbeResult:
        """Readiness probe that the source (and this collection's query) can be reached.

        Default: pull only the first page of the listing — a bounded, read-only touch
        that never downloads item files. An empty-but-reachable collection passes; a
        source that raises (network down, bad query, rejected credentials) fails with
        the source's own error text. Reraising is caught here and turned into a
        failing :class:`ProbeResult`, never propagated.
        """
        try:
            # Touch at most the first row: enough to force a real request without
            # draining the whole (possibly huge) listing.
            list(islice(self.list_items(collection), 1))
            return ProbeResult(ok=True)
        except Exception as e:  # any source fault is a readiness failure, never a crash
            return ProbeResult(
                ok=False,
                consequence=(
                    f"source {self.source_config.name!r} could not be reached for "
                    f"collection {collection.name!r}: {e}"
                ),
                action="check source connectivity, credentials, and the collection query, then rerun",
            )

    # Optional: declared metadata fields available for library_layout templates.
    #: subclass may override to advertise template variables it surfaces.
    template_fields: tuple[str, ...] = ()

    # Subset of ``template_fields`` guaranteed to distinguish any two items in a
    # collection — the source's per-item identity fields. The config layer uses this
    # to guard against lossy `library_layout` templates that would render two items to
    # the same staging path (§spec:layout-collision-safety). Empty means the plugin
    # vouches for no field's uniqueness.
    per_item_unique_fields: tuple[str, ...] = ()
