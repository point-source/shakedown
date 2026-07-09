"""Source plugin contract. PRD §7."""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Any

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import Manifest


@dataclass(frozen=True)
class ItemDescriptor:
    """Lightweight descriptor returned by discover().

    Carries enough to:
    - identify the item uniquely (identifier),
    - decide whether we have it (manifest comparison via .manifest),
    - render staging templates (.metadata),
    - flag restricted items without a fetch attempt (.is_restricted/.restriction_reason).
    """
    identifier: str
    manifest: Manifest
    metadata: dict[str, Any] = field(default_factory=dict)
    is_restricted: bool = False
    restriction_reason: str | None = None
    # source-provided change signal for incremental discovery (§spec:incremental-discovery);
    # a token that moves whenever the item's contents change. None when the source exposes
    # no cheap signal.
    change_signal: str | None = None


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
    """
    success: bool
    bytes_downloaded: int
    error: str | None = None
    retriable: bool = False
    retry_after: float | None = None


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

    def __init__(self, source_config: SourceConfig) -> None:
        self.source_config = source_config

    @abstractmethod
    def discover(self, collection: CollectionConfig) -> Iterator[ItemDescriptor]:
        """Enumerate items currently matching the collection's query."""

    def enumerate_items(self, collection: CollectionConfig) -> Iterator[str] | None:
        """Cheaply list item identifiers so the core can fan `describe_item()` across
        the shared per-source budget (SPEC §spec:discovery-pipeline). Return None
        (the default) to signal this source has no cheap enumerate/describe split;
        the core then drains `discover()` serially. Raising signals an enumeration
        failure (the core marks the collection stale), exactly like `discover()`.
        """
        return None

    def enumerate_with_signals(
        self, collection: CollectionConfig
    ) -> Iterator[tuple[str, str | None]] | None:
        """Cheap enumeration paired with each item's source change signal
        (§spec:incremental-discovery): the same single search as enumerate_items,
        but also yielding the change signal per item so the core can skip the
        per-item metadata fetch when the stored signal is unchanged. Return None
        (the default) when the source has no cheap change signal; the core then
        always resolves the full descriptor via describe_item(). Raising signals an
        enumeration failure, exactly like enumerate_items()."""
        return None

    def describe_item(
        self, identifier: str, collection: CollectionConfig
    ) -> ItemDescriptor | None:
        """Resolve one enumerated identifier to a descriptor — the expensive per-item
        metadata fetch. Only called when `enumerate_items()` returns an iterator.
        Return None to skip the item (a transient fault already handled). Sources that
        do not support the enumerate/describe split need not implement this.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch(
        self,
        item: ItemDescriptor,
        dest_dir: Path,
        format_filters: list[str],
        exclude_filters: list[str],
    ) -> FetchResult:
        """Download files matching filters into dest_dir, verifying source checksums.

        `dest_dir` is a fresh, core-owned temporary directory. The plugin only
        writes the item's files there and reports the outcome; it MUST NOT touch
        the final archive location. The core performs the atomic rename of
        `dest_dir` into the archive on success and sweeps it on failure, so no
        plugin can commit partial state to the archive (SPEC.md §spec:sync-workflow).

        Implementations must be idempotent: re-running on a populated dest_dir
        should verify and skip matching files (the IA library does this natively).
        """

    @abstractmethod
    def verify(self, item: ItemDescriptor, archive_path: Path) -> VerifyResult:
        """Cheap re-check: every expected file present on disk."""

    # -- readiness probes (§spec:setup-readiness-validation) ----------------
    # Both are strictly non-mutating: they read from the source but never download a
    # full item, write to the archive/library, or leave state behind. `shakedown
    # validate` calls them once per configured source/collection before a real mirror.

    def check_credentials(self) -> ProbeResult:
        """Readiness probe for required credential environment variables.

        Default: a source with no ``auth`` block needs no credentials (pass); otherwise
        every configured credential env var must be present and non-empty. Acceptance —
        whether the source *honors* the credentials — is exercised by
        :meth:`check_reachable`, whose enumeration runs under the authenticated session,
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

        Default: pull only the first page of enumeration — a bounded, read-only touch
        that never downloads item files. An empty-but-reachable collection passes; a
        source that raises (network down, bad query, rejected credentials) fails with
        the source's own error text. Reraising is caught here and turned into a
        failing :class:`ProbeResult`, never propagated.
        """
        try:
            ids = self.enumerate_items(collection)
            if ids is None:
                ids = (d.identifier for d in self.discover(collection))
            # Touch at most the first identifier: enough to force a real request without
            # draining the whole (possibly huge) enumeration.
            list(islice(ids, 1))
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
