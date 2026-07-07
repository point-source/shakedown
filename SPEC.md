# Shakedown — Specification

> Shakedown maintains durable local mirrors of music collections from
> open archives and presents them to library tools through a hardlink
> staging tree. This document is the solution-space companion to
> [REQUIREMENTS.md](REQUIREMENTS.md): it records what the system does
> and why — architecture, design decisions, tradeoffs, and rejected
> alternatives. Work breakdown lives in ROADMAP.md. Together with
> REQUIREMENTS.md, this document supersedes the original PRD.

## System shape §spec:system-shape

*Status: implemented*

The system is three logical layers, two on-disk trees, and one process:

```text
Remote sources (IA, etree, ...)
        │  fetch
        ▼
ARCHIVE TREE   <archive_root>/<source>/<collection>/<item>/
        │  hardlink (same filesystem)
        ▼
LIBRARY TREE   <library_root>/<source>/...   ← library tool reads here
        │  (library tool's own pipeline)
        ▼
Streaming server (Navidrome, etc.)
```

- The **archive tree** is Shakedown's domain and the source of truth
  for "do I have it?". Directory and file names mirror the source's
  identifiers exactly, so an item's on-disk location is derivable from
  its identity alone. It is immutable from the user's perspective:
  Shakedown never asks the user to touch it, and no downstream tool
  needs to know it exists.
- The **library tree** is a human-readable projection of the archive,
  materialized entirely as hardlinks. It is disposable: it can be wiped
  and rebuilt from the archive at any time without network traffic.
- Every file in the library tree shares an inode with its archive
  counterpart. Renaming, retagging, or deleting the library-side link
  never affects the archive-side file or Shakedown's record of it, and
  vice versa.
- At startup, the system shall verify that `archive_root` and
  `library_root` reside on the same filesystem and refuse to run with a
  clear error if they do not. (§req:constraints)

**Why hardlinks, not symlinks or copies.** Symlinks dangle when a
library tool moves the target into its own managed root — Beets does
this by default. Copies double storage and let the two trees diverge
silently. Hardlinks make both paths first-class references to the same
bytes: either can be deleted without affecting the other, storage cost
is 1x (§req:quality-attributes), and the mechanism is
filesystem-native with zero runtime overhead. The accepted tradeoff is
the same-filesystem constraint, which the QNAP deployment satisfies by
putting both trees under one share (§spec:deployment). This is the
same seam the *arr ecosystem uses for its download→library handoff.

## Sync identity §spec:sync-identity

*Status: implemented*

This is the load-bearing design decision of the project
(§req:priorities #1). The system's answer to "do I already have this
item?" comes from recorded state, never from hashing bytes on disk.

- At fetch time, the system shall record the source's manifest for the
  item — its file list with sizes and checksums **as reported by the
  source** — alongside where the files were placed and when.
- On every subsequent sync, the system shall compare the source's
  *current* manifest against the *recorded* manifest. An item is
  "have it" when the manifests match and the expected archive paths
  still exist (existence check only — no hashing). An item needs
  fetching when it is unknown, or when the source manifest shows new
  files or changed upstream checksums.
- No scheduled operation shall hash on-disk bytes. Byte hashing happens
  only in the explicit, operator-invoked `verify --deep`
  (§spec:verify-drift).

**Why not checksum the disk.** The naive design — re-verify local
files against the source's checksums each run — breaks the moment a
library tool retags a file. Beets writing Vorbis comments or Picard
fixing genre tags mutates the bytes, which mutates the checksum, which
makes a disk-vs-source comparison conclude "missing" and re-download —
after which the library tool retags again, forever. Because the
archive and library share inodes, a tag write through either path
mutates both copies; that is acceptable precisely because sync never
reads the bytes. **Drift is allowed, not feared**
(§req:success-criteria criterion 4): library tools may be pointed at
the library tree with in-place tag writing enabled, and sync remains
idempotent. This mirrors how
Lidarr/Sonarr/Radarr track imported files — record what was imported,
trust the database.

The accepted tradeoff: the archive copy is not guaranteed to be
byte-identical to upstream over time. Users who want byte fidelity
(re-seeding, public mirroring, preservation) reconcile explicitly via
`verify --deep --reconform` (§spec:verify-drift; §req:success-criteria
criterion 5).

## Item lifecycle §spec:item-lifecycle

*Status: in progress — pruning retains no record; see §road:prune-retains-record*

Every discovered item carries a persistent status:

| Status | Meaning |
| --- | --- |
| `pending` | Discovered, not yet attempted |
| `downloading` | Fetch in progress |
| `complete` | Fetched successfully; manifest recorded |
| `failed` | Fetch attempted and failed; retried on the next run |
| `unavailable` | Source explicitly disallows download (e.g. stream-only); a human-readable `restriction_reason` is recorded and shown in `status` |
| `disappeared` | Previously `complete`, no longer in the source's enumeration; local files retained |

Behavior the user can observe:

- Restricted items shall appear in `shakedown status` with their
  restriction reason, so the user can see what is not being mirrored
  and why (§req:success-criteria #7). They are re-checked on each
  discover; when a restriction lifts, the item is fetched as new.
- When an item vanishes from the source's enumeration, the system
  shall retain its local files and flag it `disappeared` — an upstream
  takedown never deletes the user's copy (§req:success-criteria #9,
  §req:quality-attributes). A per-collection `prune_disappeared: true`
  opt-in instead removes the item's archive files and staging links;
  the database record is retained (marked pruned) so `status` can
  still report the takedown.

**Why retention is the default.** Durable mirroring is the product's
reason to exist; losing content must only ever be the user's explicit
choice, never an upstream side effect. Pruning exists because strict
mirror-the-source-exactly users are real, but it is opt-in per
collection so a config typo can't silently discard recordings.

## Sync workflow §spec:sync-workflow

*Status: in progress — atomic fetch lives in the IA plugin, not the core, and collection concurrency is unbounded; see §road:core-atomic-fetch, §road:concurrent-collections*

A sync run for a (source, collection) pair proceeds in six phases:

1. **Discover.** The source plugin enumerates all items currently
   matching the collection's query, returning lightweight descriptors
   with current manifests and metadata.
2. **Plan.** Each remote item's current manifest is compared against
   the recorded manifest (§spec:sync-identity) and classified:
   `new` (not in DB), `unchanged` (manifests match — skip),
   `changed-upstream` (manifests differ — refetch), or `unavailable`
   (source disallows — record reason). Each DB item absent from the
   discover results is classified `disappeared`.
3. **Fetch.** For `new` and `changed-upstream` items, the plugin
   downloads into a temporary directory inside the archive tree,
   verifying source-provided checksums as bytes arrive. On full
   success the temp directory is atomically renamed to its final
   archive location and the manifest is persisted. A partial failure
   leaves only the temp directory, cleaned up on the next run —
   no partial state ever reaches the archive tree or the database.
4. **Stage.** For every item in the archive, the library layout
   template is rendered and hardlinks are created
   (§spec:library-staging).
5. **Notify.** Configured handoff and failure notifications fire
   (§spec:handoff).
6. **Record.** A run record (counts, bytes, errors, timing) is
   persisted for `status` reporting.

Guarantees:

- **Idempotency is non-negotiable** (§req:quality-attributes). Running
  sync twice in a row does nothing the second time; running after a
  partial failure resumes correctly; running at any cadence —
  including after missed scheduled runs — converges to the same state.
- Per-item failures (malformed metadata, checksum mismatch after
  retries) mark that item `failed` and the run continues; they never
  abort the whole run (§spec:failure-behavior).
- `--dry-run` performs Discover and Plan and reports what would happen
  without touching disk or database.
- Concurrency is bounded by the global `max_concurrent_downloads` and
  `max_concurrent_collections` config caps.

**Why temp-dir + atomic rename.** The fetch phase is where crashes,
disk-full, and network drops live. Renaming within one filesystem is
atomic, so an item is either fully present in the archive with its
manifest recorded, or not there at all — the invariant that makes
"existence check only" sufficient during Plan.

## Source plugins §spec:source-plugins

*Status: in progress — interface and IA plugin done; interface doc and etree plugin missing; see §road:plugin-interface-doc, §road:etree-plugin*

Sources are pluggable so that new archives can be added without core
changes (§req:constraints). A plugin implements three operations plus
a capability declaration:

- `discover(collection_config) → iterator of item descriptors` —
  enumerate all items matching the collection's query; each descriptor
  carries the identifier, the current file manifest, and source-native
  metadata.
- `fetch(item, dest_dir, filters) → result` — download files matching
  the collection's format/exclude filters into `dest_dir`, verifying
  source-provided checksums. Idempotent: re-running against an
  existing directory skips files that already match.
- `verify(item, archive_path) → result` — cheap re-check that expected
  files still exist. Existence only; byte hashing is `verify --deep`'s
  job, implemented by the core, not plugins.
- A declaration of which query kinds the plugin understands and which
  metadata fields it surfaces (date, venue, lineage, …) for library
  layout templates (§spec:library-staging).

Plugins shipped in v1:

- **Internet Archive (`ia`).** Wraps the official `internetarchive`
  Python package for search, download, and per-file checksum
  verification — those problems are solved; the plugin does not
  reimplement them. Optional credentials (for download-restricted
  items) are read from environment variables only.
- **etree (`etree`).** A second, independently written plugin that
  proves the seam is real (§req:success-criteria #8): the interface
  documentation must be sufficient to write it without reading the IA
  plugin's internals.

Plugins are built-in and registered in code in v1; external plugin
distribution is deferred. **Why:** a packaging/distribution story is a
project of its own, and the requirement the seam must satisfy in v1 is
"a competent user can add a source type by writing one class against a
documented interface" — not "installable third-party ecosystem."

## Library staging §spec:library-staging

*Status: in progress — missing template fields raise instead of rendering `unknown`; see §road:template-unknown-fallback*

Per collection, the user provides a `library_layout` template that
renders each item's **staging directory** under the library tree, from
fields the source plugin surfaces (e.g.
`{year}/{date} - {venue|sanitize}`). The item's original archive
filenames are then hardlinked into that directory unchanged.

- **Templates produce directories, never renamed or retagged files.**
  Filenames in the deepest directory always match the archive, so
  tools (and users) can correlate the two trees by name. Tagging and
  renaming are the library tool's job, out of scope by charter
  (§req:constraints).
- **Sanitization is conservative:** filesystem-hostile characters
  (slashes, colons, and similar) are replaced; non-ASCII is preserved.
  Archival metadata is multilingual and lossy transliteration would
  destroy information the library tool may want.
- **Collisions are an error, not a silent overwrite.** If two items
  render the same staging path — or a hardlink target exists pointing
  at a different inode — the system shall report the collision and
  skip, leaving the user to disambiguate the template. Silent
  overwrites would make the staging tree's contents depend on sync
  order.
- **Missing template fields** render as a literal `unknown` segment
  rather than failing the item; collision detection catches any
  resulting conflicts. Archival metadata is patchy, and dropping an
  item from the library because its venue tag is absent serves no one.
- `library_layout: passthrough` (the default when unset) mirrors the
  archive structure 1:1 for users who want no opinion imposed.
- `shakedown restage` rebuilds the staging tree from the archive
  without any network traffic — used after changing a template or
  wiping the library tree — and completes in under five minutes for a
  ~14k-item collection (§req:success-criteria #3). Staging is also
  self-healing: links deleted by a library tool are restored on the
  next sync or restage.

## Verification and drift §spec:verify-drift

*Status: implemented*

`shakedown verify` is the explicit, operator-invoked integrity tool.
It is never scheduled by default (§req:constraints).

- Without `--deep`: existence-only — expected archive paths are
  checked against recorded manifests; missing files are reported.
- With `--deep`: on-disk files are hashed and compared against
  recorded manifests, reporting **drift**. Drift is informational, not
  an error (exit code 0): with in-place retagging allowed
  (§spec:sync-identity), every file a library tool touches will drift,
  and that is the system working as intended.
- Output defaults to a one-line summary per collection ("47 files
  drifted, run with `--list` for details"); `--list` enumerates
  drifted paths. **Why:** drift is expected and common, so a wall of
  paths by default would train users to ignore the tool's output.
- `--reconform` (only valid with `--deep`) re-fetches drifted files
  from the source, overwriting local copies with original upstream
  bytes. Because this destroys retained tag work, it shall require
  interactive confirmation, or an explicit `--yes` flag when
  non-interactive. This serves users who need byte fidelity to
  upstream — re-seeding, public mirrors, preservation
  (§req:success-criteria #5).

## CLI surface §spec:cli

*Status: implemented*

Shakedown is a one-shot CLI; every command runs to completion and
exits (§req:constraints). The command surface:

| Command | Behavior |
| --- | --- |
| `sync [--source S] [--collection C] [--dry-run]` | Run a sync now; defaults to all configured collections. What the scheduler invokes. |
| `status [--json]` | Last run per collection, item counts by status, disk usage, restricted items with reasons, disappeared items, known drift. |
| `verify [--source S] [--collection C] [--deep] [--list] [--reconform] [--yes]` | Integrity check (§spec:verify-drift). |
| `restage [--source S] [--collection C]` | Rebuild the staging tree from the archive without downloading. |
| `reconcile` | Rebuild the state database from the archive tree plus current source manifests (§spec:state). |
| `item show \| refetch \| forget <identifier>` | Single-item inspection and surgery. |
| `serve [--host] [--port]` | Optional HTTP control plane (§spec:serve). |

- Commands shall exit non-zero on failure so failures surface at the
  scheduler/container layer (§req:quality-attributes: operability).
- `status --json` emits machine-readable output for dashboards
  (Homepage/Homarr) in lieu of a web UI (§req:constraints).

**Why one-shot rather than a daemon.** The container is stateless
between runs — state lives in the on-disk database — so failures are
visible as container exit codes and scheduler logs rather than buried
inside a long-running process; testing is `shakedown <verb>` with no
daemon to stand up; and it matches how the *arr/homelab cron ecosystem
already works. Scheduling is deliberately someone else's job
(§spec:deployment).

## Configuration §spec:configuration

*Status: in progress — `max_concurrent_collections` was removed from config and must be restored; see §road:concurrent-collections*

All configuration lives in a single YAML file mounted at
`/config/shakedown.yaml`, editable through File Station or any text
editor — adding or changing a collection is a YAML edit, never a code
change (§req:success-criteria #2). See
[`shakedown.example.yaml`](shakedown.example.yaml) for the canonical
annotated example.

Structure: top-level roots (`archive_root`, `library_root`,
`state_db`) and global concurrency caps (`max_concurrent_downloads`,
`max_concurrent_collections`); a list of `sources` (name, plugin
`type`, optional auth) each containing `collections` (name, `query`,
`format_filters`, `exclude_filters`, `library_layout`,
`prune_disappeared`, `on_complete`); and global `notifications`.

- The system shall validate the full configuration at startup and
  refuse to run on errors, naming the offending key — a scheduled
  headless tool that limps along on half-parsed config fails at 3am
  with nobody watching.
- **Secrets never appear in the YAML** (§req:quality-attributes:
  security). Credential fields name environment variables (e.g.
  `email_env: IA_EMAIL`) rather than holding values, so the config
  file is safe to back up, share, and commit.
- **There is no `schedule:` field.** Timing lives entirely in the
  deployment (ofelia labels in the compose file, §spec:deployment).
  **Why:** one place to answer "when does what run," and Shakedown
  stays out of the scheduler business — no cron parser, no missed-run
  logic, no drift between two sources of timing truth.

## State persistence and recovery §spec:state

*Status: implemented*

Sync state lives in a single-file embedded SQLite database at
`<archive_root>/.shakedown/state.db`.

- **Co-located with the archive by design** (§req:constraints): a
  backup of the archive tree therefore includes the sync state, and
  the two cannot get separated by a container recreation or volume
  reshuffle. SQLite because the deployment is single-user,
  single-host, single-writer (§req:constraints) — a database server
  would be operational surface with no payoff.
- The database records, per item: identity (source, collection,
  identifier — the composite key), archive path, the **recorded
  manifest** (§spec:sync-identity), lifecycle status and
  `restriction_reason` (§spec:item-lifecycle), source metadata for
  layout templates, and discovery/download/verification timestamps.
  Per run: counts, bytes downloaded, errors, and timing — the raw
  material for `status`.
- `shakedown reconcile` rebuilds the database from scratch by walking
  the archive tree and re-fetching **manifests only** (never content)
  from the source. Because archive paths mirror source identifiers
  (§spec:system-shape), the walk recovers each item's identity; a
  corrupted or reset database therefore never forces a re-download
  (§req:quality-attributes: durability). This is also the migration
  path from a pre-existing manual mirror.

The one deliberately unrecoverable case: the user wipes the archive
tree itself. Then database and library are both invalidated and a full
re-download is required — the archive *is* the system of record, and
pretending otherwise would be false durability.

## Library handoff and notifications §spec:handoff

*Status: in progress — implementation fires per item with an unversioned payload and never sends failure notifications; see §road:handoff-batch-payload, §road:failure-notifications*

After a sync that staged at least one item for a collection, the
system fires that collection's `on_complete` action — this is how new
shows flow into the library tool with nobody watching
(§req:user-stories). Two mechanisms, webhook as default and exec as
fallback (§req:constraints):

- **Webhook** (`on_complete.webhook: <url>`): HTTP POST, fired once
  per (collection, run). The URL may embed template fields
  (`{source}`, `{collection}`, `{staging_root}`).
- **Exec** (`on_complete.exec: <command>`): a configured command run
  once per (collection, run), with the same template fields available
  (e.g. `beet import -q {staging_root}`). Exists because many library
  tools have a CLI but no HTTP listener.

The webhook body is JSON, versioned so downstream integrations can
rely on it:

```json
{
  "payload_version": 1,
  "event": "sync.complete",
  "source": "internetarchive",
  "collection": "grateful-dead",
  "staging_root": "/data/library/internetarchive/grateful-dead",
  "run": {
    "started_at": "…", "finished_at": "…",
    "items_new": 3, "items_updated": 1, "items_failed": 0,
    "bytes_downloaded": 5813772288
  },
  "staged": [
    {
      "identifier": "gd1977-05-08.sbd…",
      "archive_path": "…",
      "staging_path": "…"
    }
  ]
}
```

**Why once-per-run with a batch payload, not once-per-item.** A weekly
sync can stage dozens of items; per-item firing would hammer the
receiver, and library imports are naturally batch operations (`beet
import` on a directory). The `staged` array preserves per-item
precision for receivers that want it. **Why webhook is the default:**
loose coupling — the library tool needs no Shakedown-specific code,
and the contract is one JSON document.

Failure notifications: a global `notifications.on_failure.webhook`
fires (same envelope, `event: sync.failed`, plus an `errors` array)
when a run fails. Handoff/notification delivery failures are logged
and recorded in the run's errors — visible in `status` — but never
fail the sync itself: the mirror's integrity does not depend on a
listener being up. Payloads contain paths and counts only, never
credentials.

## Serve control plane §spec:serve

*Status: in progress — auth uses a custom header instead of bearer tokens; see §road:serve-bearer-auth*

An optional `shakedown serve` command exposes a small HTTP API for
users who want ad-hoc triggers and monitoring without `docker exec`
(§req:priorities #10). It runs as a separate long-lived container in
the compose stack. It never schedules — ofelia remains the only
scheduler (§req:constraints).

| Endpoint | Behavior |
| --- | --- |
| `GET /healthz` | Liveness |
| `GET /status` | JSON equivalent of `shakedown status` |
| `GET /metrics` | Prometheus metrics |
| `POST /sync?source=&collection=` | Trigger an ad-hoc sync (auth required) |
| `POST /verify` | Trigger a verify run (auth required) |

Security posture (this endpoint accepts requests that cause network
fetches and disk writes): mutating endpoints shall require a bearer
token supplied via environment variable; **when no token is
configured, mutating endpoints are disabled** rather than open — a
homelab reverse-proxy misconfiguration must not turn into an
unauthenticated write API. Read endpoints (`/healthz`, `/status`,
`/metrics`) are unauthenticated: they expose only operational
metadata (paths, counts, timings — no credentials), and health/metrics
scrapers generally can't send tokens. Most users won't run `serve` at
all; it is not required for any success criterion.

## Deployment §spec:deployment

*Status: implemented*

The deployment is a single Docker Compose stack, paste-able into QNAP
Container Station, operable entirely through web UIs — no SSH at any
step (§req:success-criteria #6). See
[`docker-compose.example.yaml`](docker-compose.example.yaml) for the
canonical example.

- **`shakedown`** — the one-shot CLI container; runs only while a
  command runs; no restart policy.
- **`ofelia`** — the scheduler; invokes `shakedown sync …` inside the
  shakedown container on cron schedules defined as Docker labels in
  the compose file.
- Library tool and streaming server (Beets, Navidrome, …) join the
  same stack, reading the library tree — they are the user's choice
  and Shakedown places no requirements on them.

All data paths live under a single share
(`/share/data/{archive,library,music,shakedown-config}`) so archive
and library share a filesystem (§spec:system-shape).

**Why ofelia rather than host cron or a built-in scheduler.** Ofelia
is itself a container, so the entire deployment — including timing —
is one compose YAML editable in Container Station: no host cron, no
SSH, no scheduler code in Shakedown. Schedule changes are compose-file
label edits; "did the Sunday sync run?" is answerable from ofelia's
container logs. Missed schedules are harmless because sync is
idempotent at any cadence (§spec:sync-workflow). Per-collection
schedules are additional ofelia jobs invoking
`shakedown sync --collection <name>`. The tradeoff — schedules are
more verbose as labels than a `schedule:` key in shakedown.yaml would
be — is accepted for having exactly one source of timing truth
(§spec:configuration).

Setup flow (web-UI-only): create the share folders in File Station,
upload an initial `shakedown.yaml`, paste the compose YAML into
Container Station with credentials as environment variables. Day 2:
collection changes via File Station (YAML edit), schedule changes via
Container Station (compose edit), logs via Container Station.

## Failure behavior §spec:failure-behavior

*Status: in progress — retry/backoff is delegated to the `internetarchive` library and stale collections are not reported; see §road:core-retry-backoff, §road:stale-collection-status*

The resilience contract (§req:quality-attributes): single failures
degrade a single item or a single run, never the archive.

| Failure | Behavior |
| --- | --- |
| Network drops mid-download | Partial temp dir cleaned up; resume on next run |
| Malformed source metadata | Item marked `failed`, error logged, run continues |
| Checksum mismatch during download | Bounded retries, then item marked `failed` |
| Cross-filesystem archive/library | Hard error at startup; refuse to run (§spec:system-shape) |
| State DB corrupted or lost | `reconcile` rebuilds it without re-downloading (§spec:state) |
| Disk full | Run aborts cleanly; no partial commits reach the archive |
| Source rate-limits | Plugin honors `Retry-After`, backs off, resumes |
| Library tool retags files in place | Invisible to sync (§spec:sync-identity); reported by `verify --deep` on request |
| Library tool deletes staging links | Restored on next sync or `restage` |
| User wipes library tree | Rebuilt by `restage` with zero downloads |
| User wipes archive tree | DB and library invalidated; full re-download — working as intended (§spec:state) |
| Item restricted upstream | Local copy retained; status flips to `unavailable` with reason |
| Restriction lifted upstream | Detected on next discover; fetched as new |
| Item disappears upstream | Files retained by default; flagged `disappeared` (§spec:item-lifecycle) |
| Source permanently gone | Existing items retained; collection reported stale in `status` |
| Scheduler misses runs (host down) | Next run converges; sync is idempotent at any cadence |
| Handoff webhook receiver down | Logged in run errors, visible in `status`; sync still succeeds (§spec:handoff) |
