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

- **The plugin** answers single requests and declares its capabilities:
  - `list_items` — list the items currently in a collection.
  - `lookup_item` — look up one item: its metadata, any restriction on the whole
    item, and every file the source lists for it.
  - `fetch` — download the given files of one item into a directory the core
    hands it.
  - `verify` — cheaply confirm files are present on disk.
  - Plus class attributes: `type_name` (the config key) and `template_fields`
    (metadata keys it exposes to library layout templates).
- **The core** owns everything else:
  - running lookups in parallel under the source's politeness budget, with a
    timeout and bounded retries,
  - checking that each lookup answer is well formed,
  - isolating a failed lookup to its own item, so one bad item never stops the
    rest of the collection,
  - choosing which of an item's files to mirror,
  - the temporary-directory lifecycle for each fetch,
  - the atomic promotion of a completed fetch into the archive,
  - bounded retries with exponential backoff for transient faults,
  - a post-fetch completeness guard,
  - measuring network activity for the dashboard — bytes, requests in flight,
    failures, retries, and back-off,
  - library staging (hardlinks), the state database, planning
    (change detection), and notifications.

You never write retry loops, you never catch errors to protect the rest of a
collection, you never decide which files to keep, you never touch the final
archive directory, and you never hash bytes for change detection. Those are core
responsibilities with strong invariants (see
[Core-owned durability and backoff](#core-owned-durability-and-backoff)). Your
plugin stays small.

## The data types

Import these from `shakedown.plugins.base` and `shakedown.models`:

```python
from shakedown.plugins.base import (
    SourcePlugin,
    ListedItem,
    ItemLookup,
    FetchResult,
    VerifyResult,
)
from shakedown.models import ManifestFile
```

### `ListedItem`

`list_items` yields one of these per item. It is a frozen dataclass:

```python
ListedItem(identifier: str, change_signal: str | None = None)
```

- `identifier` — a stable, unique id for the item within its source. The core
  uses it as the archive subdirectory name
  (`<archive_root>/<source>/<collection>/<identifier>/`), so it must be a single
  safe path segment. The core rejects identifiers that would escape the archive
  tree and fails that item.
- `change_signal` — optional. A cheap token from the listing itself that moves
  whenever the item's contents change (for archive.org, its update time and total
  size). When a collection opts into `incremental_discovery` and an item's signal
  matches the one recorded at its last fetch, the core skips that item's lookup.
  Only provide one if *every* change to the item's files moves it; otherwise
  leave it `None` and the core always looks the item up.

### `ItemLookup`

`lookup_item` returns one of these. It is a frozen dataclass holding the
source's answer, passed through without checking:

```python
ItemLookup(
    metadata: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]],
    restriction_reason: str | None = None,
)
```

- `metadata` — the item's metadata. Fill every key you list in
  `template_fields`; include whatever a user might want in their library path
  (title, date, venue, and so on).
- `files` — **every** file the source lists for the item, one mapping each, with
  these keys:

  | Key | Value |
  | --- | --- |
  | `name` | The file's path relative to the item directory. Required. |
  | `size` | Byte size, as an int or a digit string, if the source reports it. |
  | `md5` | The source's MD5 checksum (hex), if it reports one. |
  | `format` | The source's format name (e.g. `"Flac"`, `"VBR MP3"`), if any. Files sharing a format name form one copy of the show; when no single name covers every track, the uploader's files (no `derived_from`) of one audio class and file type do. |
  | `refused` | `True` when the source will not serve this file (e.g. archive.org's per-file `private` flag). |
  | `derived_from` | The original file's name, or a list of names, when this file is a derivative of others (e.g. archive.org lists every track behind a `.ffp` it generated). A non-audio file with an original stays out of the library. |
  | `bitrate` | Bitrate in kbps, when the source lists one. |
  | `title` | The file's track title, when the source lists one. Used to fill tags on collections with `fill_tags`. |
  | `track` | The file's track number as the source writes it (e.g. `"03"`), when it lists one. |

  Pass the source's values through as they are. Do not filter the list and do
  not coerce values: the core checks them and decides which files to mirror.
- `restriction_reason` — set it when the source restricts the *whole* item
  (access-restricted, dark). A short human-readable reason; it is shown in
  `status`. Leave it `None` when only some files are refused — mark those files
  `refused` instead.

### `ManifestFile`

```python
ManifestFile(name: str, size: int | None, md5: str | None)
```

The core builds each item's manifest from the files it chooses to mirror, and
hands those files to `fetch` and `verify` as `ManifestFile`s. Change detection
compares the `(name, size, md5)` set against the one recorded at the last
successful fetch — **the core never hashes bytes on disk to decide this** — so
report `size` and `md5` whenever the source has them. If both are missing, only
a change in the set of file names is detectable.

### `FetchResult`

`fetch` returns one of these. It is a mutable dataclass:

```python
FetchResult(
    success: bool,
    error: str | None = None,
    retriable: bool = False,
    retry_after: float | None = None,
    unavailable: bool = False,
    cause: FailureCause | None = None,
    http_status: int | None = None,
)
```

- `success` — `True` only if every given file was written to `dest_dir` and
  passed the source's checksum. Otherwise `False`.
- `error` — a short message when `success` is `False`; recorded and shown in
  `status`.
- `retriable` — on failure, `True` for a *transient* fault the core should retry
  (see [Classifying failures](#classifying-failures)), `False` for a permanent
  one. Ignored when `success` is `True`.
- `retry_after` — seconds the source explicitly asked you to wait (e.g. an HTTP
  `Retry-After` delta-seconds value on a 429). When set, the core waits that
  long instead of its own backoff. Leave `None` to use core backoff.
- `unavailable` — `True` when the source refuses to serve the item at all (e.g.
  an HTTP 403 on a stream-only item). The core records it `unavailable` with
  `error` as the reason and keeps any archive copy.
- `cause` — on a failed request, why it failed: a `FailureCause`
  (`rate_limited`, `server_error`, `timeout`, `connection_error`, `refused`).
  Use `failure_cause(exc)`. Leave `None` for a failure that was not a request,
  such as a checksum mismatch.
- `http_status` — the HTTP status of a failed request, if it had one. Use
  `http_status(exc)`.

### `VerifyResult`

`verify` returns one of these:

```python
VerifyResult(ok: bool, missing_files: list[str] = [])
```

- `ok` — `True` when every given file exists under the given path.
- `missing_files` — the names that were absent (used in log/error messages).
  Empty when `ok` is `True`.

`verify` is existence-only: it checks that files are present, never their
contents. Byte-level hashing is `verify --deep`'s job and is owned by the core.

## The four methods

Your class subclasses `SourcePlugin`. The base `__init__` stores the source's
config as `self.source_config` (a `SourceConfig`); override it only if you need
to build a client/session, and call `super().__init__(source_config)`.

### `list_items(collection)`

```python
def list_items(self, collection: CollectionConfig) -> Iterator[ListedItem]:
    ...
```

Yield a `ListedItem` for every item currently matching `collection.query` — the
source-specific query string (a search expression, a collection id; its meaning
is up to your source). Only list; do not look items up here.

#### When listing fails

If you cannot list the collection (the source is unreachable, the query endpoint
errors), **raise an exception**. Do not return a partial or empty iterator. The
core flags the collection *stale* in `status`, prunes nothing, and retains every
existing item untouched — a failed listing is not evidence that any item
disappeared (§spec:failure-behavior). Returning an empty list, by contrast,
would look like "every item disappeared" and could trigger pruning. Raise.

A listing that stops short of the total the source itself reported (for
example, a paged search that returns an empty page before its `numFound` is
reached) is a failed listing too. Raise rather than yield the short list; the
bundled `etree` plugin does this.

The listing is the only request without a core retry around it. If your client
library retries a search itself, keep that retry bounded and capped, and report
each retry to the core with `self.report_listing_retry(...)`:

```python
self.report_listing_retry(
    cause=failure_cause(exc),     # or status_cause(status) when you only have a status
    http_status=http_status(exc),
    wait_seconds=wait,            # how long you wait before the next attempt
    detail=str(exc),
)
```

Call it from the thread that iterates `list_items`, once per retry, before you
wait. It never raises. The core shows these retries with every other network
event. The bundled `ia` plugin reports the retries of its urllib3 `Retry` this
way. A plugin whose listing does not retry, like `etree`, has nothing to report.

### `lookup_item(identifier, timeout)`

```python
def lookup_item(self, identifier: str, timeout: float) -> ItemLookup:
    ...
```

Make **one** request for the item, with `timeout` (seconds) as that request's
timeout, and return an `ItemLookup`. That is all:

- **Do not retry.** The core retries a timeout, a connection error, an HTTP 429,
  or an HTTP 5xx up to a fixed number of attempts, honoring the response's
  `Retry-After`.
- **Do not catch errors.** Let them propagate. Call your client's
  `raise_for_status()` (or equivalent) so an HTTP error carries its status and
  headers; the core reads `exc.response.status_code` and
  `exc.response.headers`. A permanent error, a malformed answer, or any other
  exception fails that item for this run, and only that item: it is listed with a
  reason in the run summary and looked up again next run. A show already in the
  archive keeps its status and files.
- **Do not filter or validate.** Return every listed file. The core rejects an
  answer that is not well formed — a file entry without a name, a size that is
  not a number, a checksum that is not an MD5 — and chooses which of the files to
  mirror, the same way for every source.

### `fetch(identifier, files, dest_dir)`

```python
def fetch(
    self, identifier: str, files: Sequence[ManifestFile], dest_dir: Path
) -> FetchResult:
    ...
```

Download the given `files` of item `identifier` into `dest_dir`, verifying
source-provided checksums, and return a `FetchResult`. The core only calls
`fetch` for items that are not restricted and have at least one file. Rules:

- **`dest_dir` is a fresh, core-owned temporary directory.** Write the files
  there under their `name`s. Do **not** write anywhere else, and in particular
  **never touch the final archive location** — you do not know it and must not
  construct it. The core atomically renames `dest_dir` into the archive on
  success, so a partial or failed fetch can never corrupt an existing archived
  copy.
- **Treat file names as untrusted.** Names come from the remote source, so
  before writing confirm each one stays *inside* `dest_dir` — reject any
  absolute path or `..` component. Joining a hostile name onto `dest_dir`
  otherwise resolves outside it (`dest_dir / "/etc/x"` is `/etc/x`;
  `dest_dir / "../../x"` escapes), turning a fetch into an arbitrary file write.
  The core enforces this too — it refuses to fetch an item with an escaping
  name — but guard in the plugin so it is correct on its own.
- **Verify checksums as you go.** If the source provides an md5/size per file,
  confirm the downloaded bytes match. A mismatch is a transient fault (below).
- **Reuse what an earlier attempt finished.** `dest_dir` may hold files a failed
  attempt left behind. Keep one only when `reusable_leftover(path, file)` (from
  `shakedown.plugins.base`) accepts it — it matches the file's size and md5 —
  and fetch everything else.
- **Do not retry internally.** Make exactly one attempt per call. The core owns
  the retry loop and backoff. If you loop internally you defeat the core's
  bounded-retry accounting and its `Retry-After` handling.

#### Classifying failures

On failure, set `retriable` correctly so the core retries only faults worth
retrying:

- **Transient (`retriable=True`)** — checksum mismatch, truncated/partial
  download, rate limit, HTTP 429, HTTP 5xx, connection reset, timeout.
- **Permanent (`retriable=False`)** — HTTP 4xx other than 429, or anything a
  retry cannot fix.

When the source hands you an explicit wait (an HTTP `Retry-After` delta-seconds
header), put it in `retry_after` so the core waits exactly that long.

For a failed request, also say what failed: `cause` and `http_status`. Helpers
in `shakedown.plugins.base` read requests-, httpx-, and urllib3-style errors:

- `failure_cause(exc)` gives the `FailureCause`: 429 is `rate_limited`, 5xx is
  `server_error`, 403 is `refused`, timeouts are `timeout`, and connection
  errors (including a file cut off mid-stream) are `connection_error`. It gives
  `None` for anything else.
- `http_status(exc)` gives the response's status (or `None`).
- `retry_after_seconds(exc)` gives a usable `Retry-After` (or `None` for a
  missing, date-form, negative, or non-finite value).
- `TRANSIENT_CAUSES` holds the causes worth retrying.

```python
from shakedown.plugins.base import (
    TRANSIENT_CAUSES, FetchResult, failure_cause, http_status, retry_after_seconds,
)

def _failed(exc: Exception) -> FetchResult:
    cause = failure_cause(exc)
    return FetchResult(
        success=False, error=str(exc),
        retriable=cause in TRANSIENT_CAUSES,
        retry_after=retry_after_seconds(exc),
        cause=cause, http_status=http_status(exc),
    )
```

A 403 on a download usually means the source refuses to serve the item: report
`unavailable=True` rather than a failure.

These fields are all the dashboard needs from you. The core measures the rest
itself: bytes from what lands in `dest_dir`, requests in flight from the work it
runs under the politeness budget, and failures, retries, and back-off from each
attempt. You never report bytes or timings.

### `verify(files, archive_path)`

```python
def verify(self, files: Sequence[ManifestFile], archive_path: Path) -> VerifyResult:
    ...
```

Return a `VerifyResult` reporting whether every given file exists under
`archive_path`. This is existence-only — check `.is_file()`, never open or hash
the file. The core calls `verify` in two places: as a post-fetch completeness
guard (with the temp dir, before promotion) and during `shakedown verify`.
Deep, content-level checking is a separate core-owned path (`verify --deep`).

A complete correct implementation is one line of logic:

```python
def verify(self, files, archive_path):
    missing = [f.name for f in files if not (archive_path / f.name).is_file()]
    return VerifyResult(ok=not missing, missing_files=missing)
```

## Capability declaration

Two class attributes declare what your plugin is and what it exposes:

- **`type_name: str`** — the string that maps to `type:` in `shakedown.yaml`.
  It must be unique across plugins. A source config with `type: etree` resolves
  to the plugin whose `type_name == "etree"`.
- **`template_fields: tuple[str, ...]`** — the metadata keys your plugin
  guarantees to surface in `ItemLookup.metadata`, available to
  `library_layout` templates. Config validation checks every collection's
  `library_layout` against this tuple and **rejects** the config at load time if
  a layout references a field you do not list. So list every key a user may
  reference (e.g. `("identifier", "date", "venue", "year")`), and make sure
  `lookup_item` actually populates them in `metadata`. Defaults to `()` (only the
  `passthrough` layout, which uses no fields, would then be allowed).

One optional attribute ties your plugin to the politeness budget:

- **`upstream_host: str | None`** — the host your plugin's requests go to (e.g.
  `"archive.org"`). Every configured source whose plugin names the same host
  shares one concurrency budget, so two sources never add up past what one host
  should see. Defaults to `None`: each source of your plugin gets its own budget.

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

- **Lookups: parallel, polite, retried, isolated.** The core calls
  `lookup_item` for many items at once, but never past the source's politeness
  budget, which it shares with downloads and with every other source on the same
  host. Each call gets the core's timeout. A timeout, connection error, 429, or
  5xx is retried a fixed number of times, honoring `Retry-After` up to a cap; a
  malformed answer or any other exception fails at once. Either way only that
  item fails: it is listed with a plain-language reason, a new item is recorded
  `failed`, an item already mirrored keeps its status and files, and the
  collection is not stale. Only a failed *listing* makes a collection stale.
- **One file-selection rule.** The core decides which of an item's listed files
  to mirror, the same way for every source (SPEC §spec:quality-selection): the
  best available copy, the best playable copy when that is a different format,
  and every non-audio file. The library gets the playable copy and the
  uploader's non-audio files: not those with a `derived_from`, nor the
  source's own index and metadata files, nor archive files (`.zip`, `.rar`,
  `.7z`, `.tar`, `.gz`, …) and system or editor leftovers (`.DS_Store`,
  `Thumbs.db`, `._*`, `*~`, `.bak`), which the core tells by file name. It ranks copies by the file extension, `format`,
  `bitrate`, and `size` you pass through, and leaves out files the source
  refuses. There are no per-collection file settings.
- **Fresh temp dir per fetch.** For each item the core creates a private
  temporary directory and passes it as `dest_dir`. You write only there.
- **Atomic promotion.** On a successful, complete fetch the core renames the
  temp dir into the archive as a single atomic operation (renaming any prior
  copy aside first, restoring it if the swap fails). The archive location is
  therefore always either the old complete tree or the new complete tree, never
  a partial mix. You never see or construct the archive path in `fetch`.
- **Temp-dir sweeping.** A failed fetch never touches the archive. The core
  keeps the temp dir so the next attempt (or run) can reuse files that match
  their size and checksum, and removes it once nothing will resume it. You do
  not clean up after a failed fetch.
- **Bounded retries with exponential backoff.** The core calls your `fetch` up
  to a fixed number of attempts, retrying only when the returned
  `FetchResult.success` is `False` **and** `retriable` is `True`. Between
  attempts it waits — honoring your `retry_after` if set, otherwise an
  exponential backoff (doubling, capped). Your `fetch` makes one attempt and
  reports; the loop is the core's.
- **Post-fetch completeness guard.** Even after `fetch` returns success, the
  core checks each file's size and routes an existence check through your own
  `verify()` against the temp dir before promoting it. If a file is missing,
  promotion is aborted and the item fails. This is why `verify` must be honest
  and cheap.
- **Network activity, measured by the core.** The dashboard shows, per source,
  download speed, requests in flight, and every rate limit, server error,
  timeout, connection error, refusal, retry, and back-off. The core measures all
  of it at the seams it owns: the temp dirs, the politeness budget, and each
  attempt's outcome. You need nothing dashboard-specific. The one exception is a
  listing your client retries itself: report each retry (see
  [When listing fails](#when-listing-fails)).
- **No byte hashing for change detection.** Planning compares recorded manifest
  against current manifest (§spec:sync-identity). The only content hashing in the
  whole system is the operator-invoked `verify --deep`, in the core.

## A complete minimal example

A working skeleton for a hypothetical HTTP/JSON source. The source exposes a
search endpoint returning item ids, and a per-item endpoint returning metadata
plus a file list with sizes and md5s. Adapt the HTTP details to your source; the
*shape* is the contract. Note what is absent: no retry loop, no `try`/`except`
around a lookup, no file filtering.

```python
"""Example source plugin for a hypothetical HTTP/JSON archive."""
from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator, Sequence
from pathlib import Path

import requests

from shakedown.config import CollectionConfig, SourceConfig
from shakedown.models import ManifestFile
from shakedown.plugins.base import (
    FetchResult,
    ItemLookup,
    ListedItem,
    SourcePlugin,
    VerifyResult,
    TRANSIENT_CAUSES,
    FailureCause,
    failure_cause,
    http_status,
    retry_after_seconds,
    reusable_leftover,
)
from shakedown.plugins.registry import register

API = "https://example-archive.org/api"


def _contained(base: Path, name: str) -> bool:
    """True iff joining a remote-supplied name onto base stays inside base."""
    base_n = os.path.normpath(base)
    target = os.path.normpath(base / name)
    return target.startswith(base_n + os.sep)


@register
class ExamplePlugin(SourcePlugin):
    type_name = "example"
    upstream_host = "example-archive.org"
    template_fields = ("identifier", "title", "date", "venue", "year")
    per_item_unique_fields = ("identifier",)

    def __init__(self, source_config: SourceConfig) -> None:
        super().__init__(source_config)
        self._http = requests.Session()

    def list_items(self, collection: CollectionConfig) -> Iterator[ListedItem]:
        # Raising here is correct: the core flags the collection stale and
        # keeps every existing item.
        resp = self._http.get(f"{API}/search", params={"q": collection.query}, timeout=60)
        resp.raise_for_status()
        for hit in resp.json()["items"]:
            yield ListedItem(hit["id"])

    def lookup_item(self, identifier: str, timeout: float) -> ItemLookup:
        # One request; any error propagates to the core, which retries or fails
        # just this item.
        resp = self._http.get(f"{API}/items/{identifier}", timeout=timeout)
        resp.raise_for_status()
        doc = resp.json()
        date = doc.get("date") or ""
        return ItemLookup(
            metadata={
                "identifier": identifier,
                "title": doc.get("title"),
                "date": doc.get("date"),
                "venue": doc.get("venue"),
                "year": date[:4] or None,
            },
            files=[
                {
                    "name": f.get("name"),
                    "size": f.get("size"),
                    "md5": f.get("md5"),
                    "format": f.get("format"),
                    "refused": bool(f.get("stream_only")),
                    "derived_from": f.get("source_file"),
                }
                for f in doc.get("files", [])
            ],
            restriction_reason="removed by request" if doc.get("removed") else None,
        )

    def fetch(
        self, identifier: str, files: Sequence[ManifestFile], dest_dir: Path
    ) -> FetchResult:
        for mf in files:
            # Never write a remote-supplied name outside the item directory.
            if not _contained(dest_dir, mf.name):
                return FetchResult(
                    success=False, error=f"unsafe file name: {mf.name!r}", retriable=False,
                )
            out = dest_dir / mf.name
            out.parent.mkdir(parents=True, exist_ok=True)
            # Keep a file an earlier attempt finished; fetch anything else.
            if reusable_leftover(out, mf):
                continue
            try:
                r = self._http.get(f"{API}/items/{identifier}/{mf.name}", timeout=60)
                r.raise_for_status()
            except requests.RequestException as e:
                cause = failure_cause(e)
                if cause is FailureCause.REFUSED:
                    return FetchResult(
                        success=False, error="download refused by the source", unavailable=True,
                        cause=cause, http_status=403,
                    )
                return FetchResult(
                    success=False, error=str(e),
                    retriable=cause in TRANSIENT_CAUSES,
                    retry_after=retry_after_seconds(e),
                    cause=cause, http_status=http_status(e),
                )

            data = r.content
            if mf.md5 is not None and hashlib.md5(data).hexdigest() != mf.md5:
                # Checksum mismatch is a transient fault worth another attempt.
                return FetchResult(
                    success=False, error=f"checksum mismatch for {mf.name}", retriable=True,
                )
            out.write_bytes(data)

        return FetchResult(success=True)

    def verify(self, files: Sequence[ManifestFile], archive_path: Path) -> VerifyResult:
        missing = [f.name for f in files if not (archive_path / f.name).is_file()]
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
        # Renders the staging directory; original filenames are appended.
        # Only fields in the plugin's template_fields may be referenced.
        library_layout: '{year}/{date} - {venue}/{identifier}'
```

With that in place, `shakedown sync --source example-archive` will list the
collection, look items up in parallel under the politeness budget, plan against
recorded manifests, fetch new and changed items into core-owned temp dirs,
promote them atomically into the archive, and stage them into the library tree —
all using only the four methods above.
