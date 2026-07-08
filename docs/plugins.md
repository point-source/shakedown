# Writing a source plugin

This is the authoritative guide for authoring a Shakedown source plugin. It is
self-contained: a competent Python developer should be able to write a complete,
correct plugin from this document alone, without reading any existing plugin's
source. It reflects the contract defined by `SPEC.md` §spec:source-plugins and
the sync workflow in §spec:sync-workflow.

## Overview

A source plugin teaches Shakedown how to talk to one kind of remote archive
(Internet Archive, an `etree`-style HTTP/JSON catalog, Phish.in, and so on). The
plugin is a thin adapter over that source. Everything hard about durability lives
in the core, not in your plugin.

Division of labor:

- **The plugin** does three things and declares its capabilities:
  - `discover` — enumerate the items in a collection and describe each one.
  - `fetch` — download one item's files into a directory the core hands it.
  - `verify` — cheaply confirm one item's files are present on disk.
  - Plus two class attributes: `type_name` (the config key) and
    `template_fields` (metadata keys it exposes to library layout templates).
- **The core** owns everything else:
  - the temporary-directory lifecycle for each fetch,
  - the atomic promotion of a completed fetch into the archive,
  - bounded retries with exponential backoff for transient faults,
  - a post-fetch completeness guard,
  - library staging (hardlinks), the state database, planning
    (change detection), and notifications.

You never write retry loops, you never touch the final archive directory, and
you never hash bytes for change detection. Those are core responsibilities with
strong invariants (see [Core-owned durability and backoff](#core-owned-durability-and-backoff)).
Your plugin stays small.

## The data types

Import these from `shakedown.plugins.base` and `shakedown.models`:

```python
from shakedown.plugins.base import (
    SourcePlugin,
    ItemDescriptor,
    FetchResult,
    VerifyResult,
)
from shakedown.models import Manifest, ManifestFile
```

### `ItemDescriptor`

`discover` yields one of these per item. It is a frozen dataclass:

```python
ItemDescriptor(
    identifier: str,
    manifest: Manifest,
    metadata: dict[str, Any] = {},
    is_restricted: bool = False,
    restriction_reason: str | None = None,
)
```

- `identifier` — a stable, unique id for the item within its source. The core
  uses it as the archive subdirectory name (`<archive_root>/<source>/<collection>/<identifier>/`),
  so it must be a single safe path segment: no `/`, no `..`, not absolute. The
  core rejects identifiers that would escape the archive tree and fails that
  item, so sanitize upstream ids into safe segments in `discover`.
- `manifest` — the source's reported file list for this item (see below). This
  is the identity used for change detection.
- `metadata` — a free-form dict of source metadata. Keys listed in
  `template_fields` become available to `library_layout` templates. Include
  whatever a user might reasonably want in their library path (title, date,
  venue, and so on).
- `is_restricted` — set `True` for items the source will not let you download
  (stream-only, access-restricted, region-locked). The core records these as
  `unavailable` and never calls `fetch` for them.
- `restriction_reason` — a short human-readable explanation, surfaced in
  `status`. Set it whenever `is_restricted` is `True`.

### `Manifest` and `ManifestFile`

```python
ManifestFile(name: str, size: int | None, md5: str | None)
Manifest(files: tuple[ManifestFile, ...])
```

- `ManifestFile.name` — the file's path relative to the item's directory. Use
  the same relative name the file will have on disk after `fetch`.
- `ManifestFile.size` — byte size if the source reports it, else `None`.
- `ManifestFile.md5` — the source-reported MD5 if available, else `None`.

The manifest is the whole game for change detection. Two manifests are equal
when their `(name, size, md5)` fingerprint sets are equal — `Manifest.__eq__`
compares `frozenset` fingerprints, order-independent. Shakedown answers "do I
already have this item, unchanged?" by comparing the manifest you return now
against the manifest it recorded at the last successful fetch. **It never hashes
the bytes on disk to make this decision.** Consequences for you:

- Build the manifest from the source's own reported metadata (its file list and
  checksums), not by reading local files.
- Populate `size` and `md5` whenever the source provides them. They sharpen
  change detection: if a file is re-mastered upstream and its size or md5
  changes, the fingerprint changes and the core re-fetches. If you leave both
  `None`, only a change in the file *set* (names added/removed) is detectable.
- Include only the files you intend to fetch — apply the collection's filters
  (below) when building the manifest, so the recorded manifest matches what
  actually lands on disk.
- Keeping the file ordering stable is good hygiene but not required for
  equality; the fingerprint is a set.

### `FetchResult`

`fetch` returns one of these. It is a mutable dataclass:

```python
FetchResult(
    success: bool,
    bytes_downloaded: int,
    error: str | None = None,
    retriable: bool = False,
    retry_after: float | None = None,
)
```

- `success` — `True` only if every manifest file was written to `dest_dir` and
  passed the source's checksum. Otherwise `False`.
- `bytes_downloaded` — total bytes written this call, for run stats. Report your
  best count even on failure.
- `error` — a short message when `success` is `False`; recorded and shown in
  `status`.
- `retriable` — on failure, `True` for a *transient* fault the core should retry
  (see fault classification under [`fetch`](#fetchitem-dest_dir-format_filters-exclude_filters)),
  `False` for a permanent one. Ignored when `success` is `True`.
- `retry_after` — seconds the source explicitly asked you to wait (e.g. an HTTP
  `Retry-After` delta-seconds value on a 429). When set, the core waits that
  long instead of its own backoff. Leave `None` to use core backoff.

### `VerifyResult`

`verify` returns one of these:

```python
VerifyResult(ok: bool, missing_files: list[str] = [])
```

- `ok` — `True` when every manifest file exists under the given path.
- `missing_files` — the manifest names that were absent (used in log/error
  messages). Empty when `ok` is `True`.

`verify` is existence-only: it checks that files are present, never their
contents. Byte-level hashing is `verify --deep`'s job and is owned by the core.

## The three methods

Your class subclasses `SourcePlugin`. The base `__init__` stores the source's
config as `self.source_config` (a `SourceConfig`); override it only if you need
to build a client/session, and call `super().__init__(source_config)`.

### `discover(collection)`

```python
def discover(self, collection: CollectionConfig) -> Iterator[ItemDescriptor]:
    ...
```

Enumerate every item currently matching `collection.query` and yield an
`ItemDescriptor` for each. `collection` is a `CollectionConfig` with these
fields you will use:

- `collection.query` — the source-specific query string (its meaning is entirely
  up to your source; e.g. a search expression or a collection id).
- `collection.format_filters` — `list[str]`, keep-only file formats/extensions.
- `collection.exclude_filters` — `list[str]`, fnmatch glob patterns on filename
  to drop.

For each item:

1. Resolve its file list from the source.
2. Apply the filters (below) and build a `Manifest` from the surviving files.
3. Collect metadata for templates.
4. Determine whether the item is restricted; if so set `is_restricted=True` and
   a `restriction_reason`, and do not worry about its files — the core will not
   fetch it.
5. `yield ItemDescriptor(...)`.

`discover` is a generator; yield items lazily so large collections stream.

#### Filter semantics

Apply both filters when deciding which files enter the manifest. Implement them
exactly as follows so behavior matches every other plugin:

- **`exclude_filters`** are fnmatch glob patterns matched against the file's
  name. If the name matches *any* exclude pattern, drop the file. Apply excludes
  first.
- **`format_filters`**, when the list is *non-empty*, keep a file only if its
  source-reported format **or** its filename extension matches one of the
  entries (case-insensitive). When the list is *empty*, keep every file (that
  survived excludes).

Reference implementation for one file:

```python
import fnmatch

def _keep(name: str, fmt: str, format_filters: list[str], exclude_filters: list[str]) -> bool:
    if any(fnmatch.fnmatch(name, pat) for pat in exclude_filters):
        return False
    if not format_filters:
        return True
    fmt = (fmt or "").lower()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return any(f.lower() == fmt or f.lower() == ext for f in format_filters)
```

If your source reports no distinct "format" field, pass the extension for both,
or just compare against the extension.

#### When enumeration fails

If you cannot enumerate the collection (the source is unreachable, the query
endpoint errors), **raise an exception**. Do not return a partial or empty
iterator. The core catches the exception, flags the collection *stale* in
`status`, and retains every existing item untouched — a failed enumeration is
not evidence that any item disappeared (§spec:failure-behavior). Returning an
empty list, by contrast, would look like "every item disappeared" and could
trigger pruning. Raise.

### `fetch(item, dest_dir, format_filters, exclude_filters)`

```python
def fetch(
    self,
    item: ItemDescriptor,
    dest_dir: Path,
    format_filters: list[str],
    exclude_filters: list[str],
) -> FetchResult:
    ...
```

Download `item`'s manifest files into `dest_dir`, verifying source-provided
checksums as bytes arrive, and return a `FetchResult`. Rules:

- **`dest_dir` is a fresh, core-owned temporary directory.** Write the item's
  files there under their manifest `name`s. Do **not** write anywhere else, and
  in particular **never touch the final archive location** — you do not know it
  and must not construct it. The core atomically renames `dest_dir` into the
  archive on success and sweeps it on failure, so a partial or failed fetch can
  never corrupt an existing archived copy.
- **Treat file names as untrusted.** Manifest `name`s come from the remote
  source, so before writing confirm each one stays *inside* `dest_dir` — reject
  any absolute path or `..` component. Joining a hostile name onto `dest_dir`
  otherwise resolves outside it (`dest_dir / "/etc/x"` is `/etc/x`;
  `dest_dir / "../../x"` escapes), turning a fetch into an arbitrary file write.
  The core enforces this too — it refuses to fetch an item whose manifest holds
  an escaping name — but guard in the plugin so it is correct on its own.
- **Verify checksums as you go.** If the source provides an md5/size per file,
  confirm the downloaded bytes match. A mismatch is a transient fault (below).
- **Be idempotent.** The core wipes `dest_dir` before each attempt, but write
  your `fetch` so that re-running against an already-populated directory
  verifies and skips complete files rather than corrupting them.
- **Fail fast on restricted items.** If `item.is_restricted`, return
  `FetchResult(success=False, bytes_downloaded=0, error=...)` immediately with
  `retriable=False`; download nothing. (In normal operation the core will not
  call `fetch` for restricted items, but guard anyway.)
- **Do not retry internally.** Make exactly one attempt per call. The core owns
  the retry loop and backoff. If you loop internally you defeat the core's
  bounded-retry accounting and its `Retry-After` handling.

The `format_filters` / `exclude_filters` arguments are the same lists from the
collection, passed for convenience; usually you fetch exactly the files already
in `item.manifest` (which you filtered in `discover`), so you may not need them
again.

#### Classifying failures

On failure, set `retriable` correctly so the core retries only faults worth
retrying:

- **Transient (`retriable=True`)** — checksum mismatch, truncated/partial
  download, rate limit, HTTP 429, HTTP 5xx, connection reset, timeout.
- **Permanent (`retriable=False`)** — item restricted, malformed/missing
  metadata, HTTP 4xx other than 429, or anything a retry cannot fix.

When the source hands you an explicit wait (an HTTP `Retry-After` delta-seconds
header), put it in `retry_after` so the core waits exactly that long. A helper
to classify a requests-style exception:

```python
def _classify(exc: Exception) -> tuple[bool, float | None]:
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) if resp is not None else None
    retriable = status == 429 or (status is not None and 500 <= status < 600)
    retry_after = None
    if resp is not None:
        raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
        try:
            retry_after = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            retry_after = None
    return retriable, retry_after
```

### `verify(item, archive_path)`

```python
def verify(self, item: ItemDescriptor, archive_path: Path) -> VerifyResult:
    ...
```

Return a `VerifyResult` reporting whether every manifest file exists under
`archive_path`. This is existence-only — check `.is_file()`, never open or hash
the file. The core calls `verify` in two places: as a post-fetch completeness
guard (with the temp dir, before promotion) and during `shakedown verify`.
Deep, content-level checking is a separate core-owned path (`verify --deep`).

A complete correct implementation is one line of logic:

```python
def verify(self, item, archive_path):
    missing = [f.name for f in item.manifest.files
               if not (archive_path / f.name).is_file()]
    return VerifyResult(ok=not missing, missing_files=missing)
```

## Capability declaration

Two class attributes declare what your plugin is and what it exposes:

- **`type_name: str`** — the string that maps to `type:` in `shakedown.yaml`.
  It must be unique across plugins. A source config with `type: etree` resolves
  to the plugin whose `type_name == "etree"`.
- **`template_fields: tuple[str, ...]`** — the metadata keys your plugin
  guarantees to surface in `ItemDescriptor.metadata`, available to
  `library_layout` templates. Config validation checks every collection's
  `library_layout` against this tuple and **rejects** the config at load time if
  a layout references a field you do not list. So list every key a user may
  reference (e.g. `("identifier", "date", "venue", "year")`), and make sure
  `discover` actually populates them in `metadata`. Defaults to `()` (only the
  `passthrough` layout, which uses no fields, would then be allowed).

## Registration

Two steps wire your plugin into the registry:

1. Decorate the class with `@register` from `shakedown.plugins.registry`. This
   records `type_name -> class`.
2. Add a lazy import of your plugin module inside
   `registry._ensure_builtins_loaded`, so the class is imported (and thus
   registered) the first time the registry is consulted. The import lives inside
   that function to avoid an import cycle between the registry and plugins.

```python
# in shakedown/plugins/registry.py
def _ensure_builtins_loaded() -> None:
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    from shakedown.plugins.ia.plugin import IAPlugin        # noqa: F401
    from shakedown.plugins.etree.plugin import EtreePlugin  # noqa: F401  <- add yours
```

After both steps, `type: etree` in config resolves; an unknown `type:` raises a
clear config error listing the known types.

## Core-owned durability and backoff

Read this section carefully — it defines the boundary your plugin must respect.
These behaviors are the **core's** responsibility, implemented and tested as
completed features §road:core-atomic-fetch and §road:core-retry-backoff. Your
plugin must not reimplement any of them.

- **Fresh temp dir per fetch.** For each item the core creates a private
  temporary directory and passes it as `dest_dir`. You write only there.
- **Atomic promotion.** On a successful, complete fetch the core renames the
  temp dir into the archive as a single atomic operation (renaming any prior
  copy aside first, restoring it if the swap fails). The archive location is
  therefore always either the old complete tree or the new complete tree, never
  a partial mix. You never see or construct the archive path in `fetch`.
- **Temp-dir sweeping.** On failure the core leaves nothing behind in the
  archive and sweeps the temp dir (immediately or on a later run). You do not
  clean up after a failed fetch.
- **Bounded retries with exponential backoff.** The core calls your `fetch` up
  to a fixed number of attempts, retrying only when the returned
  `FetchResult.success` is `False` **and** `retriable` is `True`. Between
  attempts it waits — honoring your `retry_after` if set, otherwise an
  exponential backoff (doubling, capped). The temp dir is wiped before each
  attempt. Your `fetch` makes one attempt and reports; the loop is the core's.
- **Post-fetch completeness guard.** Even after `fetch` returns success, the
  core routes an existence check through your own `verify()` against the temp
  dir before promoting it. If a manifest file is missing, promotion is aborted
  and the item fails. This is why `verify` must be honest and cheap.
- **No byte hashing for change detection.** Planning compares recorded manifest
  against current manifest (§spec:sync-identity). The only content hashing in the
  whole system is the operator-invoked `verify --deep`, in the core.

## A complete minimal example

A working skeleton for a hypothetical HTTP/JSON source. The source exposes a
search endpoint returning item ids, and a per-item endpoint returning metadata
plus a file list with sizes and md5s. Adapt the HTTP details to your source; the
*shape* is the contract.

```python
"""Example source plugin for a hypothetical HTTP/JSON archive."""
from __future__ import annotations

import fnmatch
import hashlib
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import Manifest, ManifestFile
from shakedown.plugins.base import (
    FetchResult,
    ItemDescriptor,
    SourcePlugin,
    VerifyResult,
)
from shakedown.plugins.registry import register

API = "https://example-archive.org/api"


def _keep(name: str, fmt: str, formats: list[str], excludes: list[str]) -> bool:
    if any(fnmatch.fnmatch(name, pat) for pat in excludes):
        return False
    if not formats:
        return True
    fmt = (fmt or "").lower()
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return any(f.lower() == fmt or f.lower() == ext for f in formats)


def _contained(base: Path, name: str) -> bool:
    """True iff joining a remote-supplied name onto base stays inside base."""
    base_n = os.path.normpath(base)
    target = os.path.normpath(base / name)
    return target.startswith(base_n + os.sep)


@register
class ExamplePlugin(SourcePlugin):
    type_name = "example"
    template_fields = ("identifier", "title", "date", "venue", "year")

    def __init__(self, source_config: SourceConfig) -> None:
        super().__init__(source_config)
        self._http = requests.Session()

    def discover(self, collection: CollectionConfig) -> Iterator[ItemDescriptor]:
        # Raising on enumeration failure is correct: the core flags the
        # collection stale and retains existing items.
        resp = self._http.get(f"{API}/search", params={"q": collection.query})
        resp.raise_for_status()
        for hit in resp.json()["items"]:
            identifier = hit["id"]
            meta_resp = self._http.get(f"{API}/items/{identifier}")
            meta_resp.raise_for_status()
            doc = meta_resp.json()

            files = [
                ManifestFile(name=f["name"], size=f.get("size"), md5=f.get("md5"))
                for f in doc["files"]
                if _keep(
                    f["name"], f.get("format", ""),
                    collection.format_filters, collection.exclude_filters,
                )
            ]

            metadata: dict[str, Any] = {
                "identifier": identifier,
                "title": doc.get("title"),
                "date": doc.get("date"),
                "venue": doc.get("venue"),
                "year": (doc.get("date") or "")[:4] or None,
            }

            restricted = bool(doc.get("stream_only"))
            yield ItemDescriptor(
                identifier=identifier,
                manifest=Manifest(files=tuple(files)),
                metadata=metadata,
                is_restricted=restricted,
                restriction_reason="stream-only" if restricted else None,
            )

    def fetch(
        self,
        item: ItemDescriptor,
        dest_dir: Path,
        format_filters: list[str],
        exclude_filters: list[str],
    ) -> FetchResult:
        if item.is_restricted:
            return FetchResult(
                success=False, bytes_downloaded=0,
                error=f"restricted: {item.restriction_reason}", retriable=False,
            )

        total = 0
        for mf in item.manifest.files:
            # Never write a remote-supplied name outside the item directory.
            if not _contained(dest_dir, mf.name):
                return FetchResult(
                    success=False, bytes_downloaded=total,
                    error=f"unsafe file name: {mf.name!r}", retriable=False,
                )
            out = dest_dir / mf.name
            out.parent.mkdir(parents=True, exist_ok=True)
            # Idempotent: skip a file already present and correct.
            if out.is_file() and (mf.size is None or out.stat().st_size == mf.size):
                total += out.stat().st_size
                continue
            try:
                r = self._http.get(f"{API}/items/{item.identifier}/{mf.name}")
                r.raise_for_status()
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                retriable = status == 429 or (status is not None and 500 <= status < 600)
                retry_after = None
                if e.response is not None:
                    raw = e.response.headers.get("Retry-After")
                    retry_after = float(raw) if raw and raw.isdigit() else None
                return FetchResult(
                    success=False, bytes_downloaded=total, error=str(e),
                    retriable=retriable, retry_after=retry_after,
                )
            except requests.RequestException as e:
                # Connection reset / timeout: transient.
                return FetchResult(
                    success=False, bytes_downloaded=total,
                    error=str(e), retriable=True,
                )

            data = r.content
            if mf.md5 is not None and hashlib.md5(data).hexdigest() != mf.md5:
                # Checksum mismatch is a transient fault worth another attempt.
                return FetchResult(
                    success=False, bytes_downloaded=total,
                    error=f"checksum mismatch for {mf.name}", retriable=True,
                )
            out.write_bytes(data)
            total += len(data)

        return FetchResult(success=True, bytes_downloaded=total)

    def verify(self, item: ItemDescriptor, archive_path: Path) -> VerifyResult:
        missing = [
            f.name for f in item.manifest.files
            if not (archive_path / f.name).is_file()
        ]
        return VerifyResult(ok=not missing, missing_files=missing)
```

Register it by adding to `registry._ensure_builtins_loaded` as shown in
[Registration](#registration).

The matching config stanza in `shakedown.yaml`:

```yaml
sources:
  - name: example-archive
    type: example
    collections:
      - name: my-collection
        query: 'genre:jazz year:1975'
        format_filters: [flac, shn]
        exclude_filters: ['*_64kb.mp3']
        # Renders the staging directory; original filenames are appended.
        # Only fields in the plugin's template_fields may be referenced.
        library_layout: '{year}/{date} - {venue|sanitize}'
```

With that in place, `shakedown sync --source example-archive` will discover,
plan against recorded manifests, fetch new/changed items into core-owned temp
dirs, promote them atomically into the archive, and stage them into the library
tree — all using only the three methods above.
