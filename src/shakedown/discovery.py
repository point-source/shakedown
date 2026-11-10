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
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import requests

from shakedown.config import Config, SourceConfig
from shakedown.plugins.base import ItemLookup, SourcePlugin

# The per-item lookup envelope, matched to archive.org's real latency
# (SPEC §spec:slow-metadata): a slow, 5xx, or 429 answer costs a bounded wait.
LOOKUP_TIMEOUT = 30.0  # seconds, handed to the plugin for its single request
LOOKUP_ATTEMPTS = 3
_LOOKUP_BACKOFF_BASE_SECONDS = 1.0
_LOOKUP_MAX_BACKOFF_SECONDS = 30.0


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
class SourceFile:
    """One file the source lists for an item, after validation."""
    name: str
    size: int | None
    md5: str | None
    format: str | None = None
    refused: bool = False  # the source will not serve this file
    derived_from: str | None = None  # the original file, for a derivative
    bitrate: int | None = None  # kbps, when the source lists it


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


def _status(exc: BaseException) -> int | None:
    """HTTP status of a requests- or httpx-style error (both carry .response)."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


_TIMEOUTS = (requests.Timeout, httpx.TimeoutException, TimeoutError)
_CONNECTION_ERRORS = (requests.ConnectionError, httpx.TransportError, ConnectionError)


def _describe_fault(exc: BaseException) -> tuple[bool, str]:
    """(transient, plain-language description) for a fault a lookup raised.

    Transient: 429, 5xx, timeouts, connection errors (SPEC §spec:slow-metadata).
    Everything else fails at once.
    """
    status = _status(exc)
    if status is not None:
        return status == 429 or 500 <= status < 600, f"HTTP {status}"
    # requests.Timeout subclasses requests.ConnectionError: check timeouts first.
    if isinstance(exc, _TIMEOUTS):
        return True, "timed out"
    if isinstance(exc, _CONNECTION_ERRORS):
        return True, "connection error"
    return False, f"{type(exc).__name__}: {exc}"


def _retry_after(exc: BaseException) -> float | None:
    """A numeric `Retry-After` from the error's response, if any."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _backoff_seconds(attempt: int, exc: BaseException) -> float:
    """Wait before the next attempt: the source's `Retry-After` when given, else an
    exponential backoff — either way capped. The header is upstream-controlled and
    must never pin a worker for an attacker-chosen time."""
    retry_after = _retry_after(exc)
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
    *,
    sleep: Callable[[float], None] = time.sleep,
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
            sleep(_backoff_seconds(attempt, e))
    raise AssertionError("unreachable: LOOKUP_ATTEMPTS must be >= 1")
