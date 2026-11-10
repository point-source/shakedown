"""Core-owned discovery: the shared source budget and the per-item lookup.

A plugin answers single requests — list a collection, look up one item. The core
runs every lookup under the shared per-source budget with a bounded timeout and
retry envelope, validates the answer, and isolates a failed lookup to its own item
(SPEC §spec:per-item-isolation, §spec:source-budget, §spec:slow-metadata).
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import requests

from shakedown.config import CollectionConfig, Config, SourceConfig
from shakedown.models import Manifest, SourceFile
from shakedown.plugins.base import (
    ItemLookup,
    ListedItem,
    SourcePlugin,
    http_status,
    retry_after_seconds,
)
from shakedown.selection import select

# The per-item lookup envelope, matched to archive.org's real latency
# (SPEC §spec:slow-metadata): a slow, 5xx, or 429 answer costs a bounded wait.
LOOKUP_TIMEOUT = 30.0  # seconds, handed to the plugin for its single request
LOOKUP_ATTEMPTS = 3
_LOOKUP_BACKOFF_BASE_SECONDS = 1.0
_LOOKUP_MAX_BACKOFF_SECONDS = 30.0
_sleep = time.sleep  # looked up per call, so tests can skip real backoff


class SourceBudget:
    """Shared per-source concurrency ceiling (SPEC §spec:source-budget).

    One instance per upstream host, shared across every collection of the sources
    on that host and both their discovery and download workers, so the number of
    simultaneous connections Shakedown opens to the host never exceeds the
    configured budget — no matter how many collections run concurrently.
    """

    def __init__(self, size: int) -> None:
        self.size = size
        self._sem = threading.BoundedSemaphore(size)

    @contextmanager
    def slot(self) -> Iterator[None]:
        self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()


def _source_budget_size(config: Config, source: SourceConfig) -> int:
    return source.max_concurrent_requests or config.max_concurrent_downloads


def source_budgets(
    config: Config, work: Sequence[tuple[SourceConfig, SourcePlugin]]
) -> dict[str, SourceBudget]:
    """One shared concurrency budget per upstream host, keyed by source name.

    All collections of a source — and every source whose plugin names the same host
    (an `ia` and an `etree` source both reach archive.org) — draw on one bounded
    pool, so the ceiling on simultaneous connections to a host holds even with
    max_concurrent_collections > 1. A shared budget takes the smallest ceiling its
    sources set (SPEC §spec:source-budget).
    """
    groups: dict[str, list[SourceConfig]] = {}
    group_of: dict[str, str] = {}
    for source, plugin in work:
        if source.name in group_of:
            continue
        key = f"host:{plugin.upstream_host}" if plugin.upstream_host else f"source:{source.name}"
        groups.setdefault(key, []).append(source)
        group_of[source.name] = key
    shared = {
        key: SourceBudget(min(_source_budget_size(config, s) for s in sources))
        for key, sources in groups.items()
    }
    return {name: shared[key] for name, key in group_of.items()}


# --- the one description validator ----------------------------------------------


@dataclass(frozen=True)
class ItemDetails:
    """A validated lookup: what the source says about one item."""
    metadata: dict[str, Any]
    files: tuple[SourceFile, ...]
    restriction_reason: str | None = None


class MalformedLookup(ValueError):
    """The source's answer for an item is not a well-formed description."""


_MD5 = re.compile(r"[0-9a-fA-F]{32}")


def _size(raw: Any, name: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    raise MalformedLookup(f"source sent a malformed file list: size of {name!r} is not a number")


def _md5(raw: Any, name: str) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str) and _MD5.fullmatch(raw):
        return raw.lower()
    raise MalformedLookup(
        f"source sent a malformed file list: checksum of {name!r} is not an md5"
    )


def _optional_str(raw: Any, field: str, name: str) -> str | None:
    if raw is None or isinstance(raw, str):
        return raw or None
    raise MalformedLookup(f"source sent a malformed file list: {field} of {name!r} is not text")


def _bitrate(raw: Any) -> int | None:
    """Informational only: an unreadable bitrate is dropped, never a failure."""
    try:
        return int(float(raw)) if raw is not None else None
    except (TypeError, ValueError):
        return None


def validate_lookup(raw: object) -> ItemDetails:
    """THE definition of a well-formed item description (§spec:per-item-isolation).

    Rejects an answer that is not an ItemLookup, metadata that is not a mapping, a
    file list that is not a list, a file entry without a name, and a size or checksum
    that is present but not a number / not an md5. A missing size or checksum is
    allowed: archive.org lists its own index files without them.
    """
    if not isinstance(raw, ItemLookup):
        raise MalformedLookup("source sent a malformed answer: not an item lookup")
    if not isinstance(raw.metadata, Mapping):
        raise MalformedLookup("source sent a malformed answer: its metadata is not a mapping")
    if isinstance(raw.files, str | bytes) or not isinstance(raw.files, Sequence):
        raise MalformedLookup("source sent a malformed answer: its file list is not a list")
    files: list[SourceFile] = []
    for position, entry in enumerate(raw.files, start=1):
        if not isinstance(entry, Mapping):
            raise MalformedLookup(
                f"source sent a malformed file list: file entry {position} is not a file record"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise MalformedLookup(
                f"source sent a malformed file list: file entry {position} has no name"
            )
        files.append(
            SourceFile(
                name=name,
                size=_size(entry.get("size"), name),
                md5=_md5(entry.get("md5"), name),
                format=_optional_str(entry.get("format"), "format", name),
                refused=bool(entry.get("refused", False)),
                derived_from=_optional_str(entry.get("derived_from"), "original", name),
                bitrate=_bitrate(entry.get("bitrate")),
            )
        )
    reason = raw.restriction_reason
    return ItemDetails(
        metadata=dict(raw.metadata),
        files=tuple(files),
        restriction_reason=str(reason) if reason else None,
    )


# --- the lookup envelope ------------------------------------------------------------


@dataclass(frozen=True)
class LookupFailure:
    """A lookup that yielded no valid description, with a plain-language reason."""
    identifier: str
    reason: str


_TIMEOUTS = (requests.Timeout, httpx.TimeoutException, TimeoutError)
_CONNECTION_ERRORS = (requests.ConnectionError, httpx.TransportError, ConnectionError)


def _describe_fault(exc: BaseException) -> tuple[bool, str]:
    """(transient, plain-language description) for a fault a lookup raised.

    Transient: 429, 5xx, timeouts, connection errors (SPEC §spec:slow-metadata).
    Everything else fails at once.
    """
    status = http_status(exc)
    if status is not None:
        return status == 429 or 500 <= status < 600, f"HTTP {status}"
    # requests.Timeout subclasses requests.ConnectionError: check timeouts first.
    if isinstance(exc, _TIMEOUTS):
        return True, "timed out"
    if isinstance(exc, _CONNECTION_ERRORS):
        return True, "connection error"
    return False, f"{type(exc).__name__}: {exc}"


def _backoff_seconds(attempt: int, exc: BaseException) -> float:
    """Wait before the next attempt: the source's `Retry-After` when given, else an
    exponential backoff — either way capped. The header is upstream-controlled and
    must never pin a worker for an attacker-chosen time."""
    retry_after = retry_after_seconds(exc)
    delay = (
        _LOOKUP_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        if retry_after is None
        else retry_after
    )
    return min(delay, _LOOKUP_MAX_BACKOFF_SECONDS)


def lookup(
    plugin: SourcePlugin,
    identifier: str,
    budget: SourceBudget,
) -> ItemDetails | LookupFailure:
    """Look up one item under the core's envelope (SPEC §spec:per-item-isolation).

    Each attempt is one plugin request holding one budget slot; the slot is released
    before any backoff, since a sleeping worker holds no connection. A transient fault
    is retried up to LOOKUP_ATTEMPTS; a malformed answer or any other exception fails
    at once. Never raises: a failure comes back as a LookupFailure with its reason.
    """
    for attempt in range(1, LOOKUP_ATTEMPTS + 1):
        try:
            with budget.slot():
                raw = plugin.lookup_item(identifier, LOOKUP_TIMEOUT)
            return validate_lookup(raw)
        except MalformedLookup as e:
            return LookupFailure(identifier, str(e))
        except Exception as e:
            transient, said = _describe_fault(e)
            if not transient:
                return LookupFailure(identifier, f"lookup failed: {said}")
            if attempt == LOOKUP_ATTEMPTS:
                return LookupFailure(
                    identifier, f"lookup failed after {LOOKUP_ATTEMPTS} attempts: {said}"
                )
            _sleep(_backoff_seconds(attempt, e))
    raise AssertionError("unreachable: LOOKUP_ATTEMPTS must be >= 1")


# --- from a listing to item descriptions ------------------------------------------


@dataclass(frozen=True)
class ItemDescriptor:
    """What the core knows about one listed item after its lookup and file selection.

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


def describe(
    identifier: str,
    details: ItemDetails,
    collection: CollectionConfig,
    change_signal: str | None = None,
) -> ItemDescriptor:
    """Turn a validated lookup into a descriptor: choose the files to mirror."""
    chosen = select(details.files, details.restriction_reason, collection)
    return ItemDescriptor(
        identifier=identifier,
        manifest=chosen.manifest,
        metadata=details.metadata,
        is_restricted=chosen.is_restricted,
        restriction_reason=chosen.restriction_reason,
        change_signal=change_signal,
    )


def look_up_one(
    plugin: SourcePlugin,
    listed: ListedItem,
    collection: CollectionConfig,
    budget: SourceBudget,
) -> ItemDescriptor | LookupFailure:
    """Look up one listed item and describe it, or report why it could not be."""
    result = lookup(plugin, listed.identifier, budget)
    if isinstance(result, LookupFailure):
        return result
    return describe(listed.identifier, result, collection, listed.change_signal)


def stream_items(
    plugin: SourcePlugin,
    collection: CollectionConfig,
    budget: SourceBudget,
    enumerated: set[str],
    *,
    look_up: Callable[[ListedItem], bool] | None = None,
) -> Iterator[ItemDescriptor | LookupFailure]:
    """List the collection, then look its items up in parallel across the shared
    budget, yielding each description (or lookup failure) as it resolves
    (SPEC §spec:discovery-pipeline, §spec:per-item-isolation).

    The listing is taken in full first; a listing that raises propagates, and the
    caller marks the collection stale. Every listed identifier is added to
    ``enumerated`` before any lookup — that set, not the described one, is the prune
    signal (§spec:prune-safety), so an item whose lookup fails is never read as gone.

    ``look_up`` decides per listed item whether to look it up (default: all) — the
    incremental fast path skips unchanged items, single-item operations pick theirs.

    Lookups are submitted only as the consumer takes results, a few ahead of it, so a
    show's details live from its lookup until the consumer has handled them — never
    for the whole listing. Memory stays flat however large the collection.
    """
    listing = list(plugin.list_items(collection))
    enumerated.update(row.identifier for row in listing)
    todo = [row for row in listing if look_up is None or look_up(row)]
    if not todo:
        return
    workers = min(budget.size, len(todo))
    window = 2 * workers
    rows = iter(todo)
    pending: set[Future[ItemDescriptor | LookupFailure]] = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:

        def top_up() -> None:
            while len(pending) < window:
                row = next(rows, None)
                if row is None:
                    return
                pending.add(pool.submit(look_up_one, plugin, row, collection, budget))

        top_up()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                pending.discard(fut)
                yield fut.result()
                top_up()
