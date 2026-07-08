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
- A collection may opt into skipping the per-item metadata fetch for
  items a cheap source-side change signal reports unchanged
  (§spec:incremental-discovery). That fast-path only ever *skips* work;
  it never concludes "unchanged" where this manifest comparison would
  find a change, so the identity guarantee here is preserved, not
  weakened (§req:quality-attributes: speed never costs correctness).

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

*Status: implemented*

Every discovered item carries a persistent status:

| Status | Meaning |
| --- | --- |
| `pending` | Discovered, not yet attempted |
| `downloading` | Fetch in progress |
| `complete` | Fetched successfully; manifest recorded |
| `failed` | Fetch attempted and failed; retried on the next run |
| `unavailable` | Source explicitly disallows download (e.g. stream-only); a human-readable `restriction_reason` is recorded and shown in `status` |
| `disappeared` | Previously `complete`, no longer in the source's enumeration; local files retained |
| `pruned` | Vanished from a `prune_disappeared`-opted collection: archive files and staging links removed, DB record retained so `status` still reports the takedown |

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
  still report the takedown. Enabling `prune_disappeared` later also
  prunes items that had already disappeared under the retain default —
  the retain→prune transition takes effect on the next sync, so the
  flag is not silently no-op on a collection with an existing backlog
  of disappeared items.

**Why retention is the default.** Durable mirroring is the product's
reason to exist; losing content must only ever be the user's explicit
choice, never an upstream side effect. Pruning exists because strict
mirror-the-source-exactly users are real, but it is opt-in per
collection so a config typo can't silently discard recordings.

## Sync workflow §spec:sync-workflow

*Status: implemented*

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

The Discover and Fetch phases may overlap and run concurrently under
the discovery-performance increment (§spec:discovery-performance); that
increment preserves every guarantee above — in particular, the
prune/`disappeared` decision remains a post-enumeration barrier that
never runs against a partial discovery.

## Discovery performance §spec:discovery-performance

*Status: implemented — the shared per-source concurrency budget
(§spec:source-budget), parallel per-item discovery and discover→download
pipelining (§spec:discovery-pipeline, both levers), the bounded slow-metadata
envelope (§spec:slow-metadata), and opt-in incremental discovery
(§spec:incremental-discovery) are all in place.*

On a large collection (~14k items, e.g. IA GratefulDead), discovery is
the wall-clock bottleneck in two regimes. A **first full sync**
resolves one metadata round-trip per item (an identifier-only search
followed by a per-item metadata fetch) and fully materializes the
enumeration before any download begins, so thousands of sequential
metadata calls precede any fetching. A **recurring no-op sync** — the
production path, a weekly scheduled run where almost nothing has changed
— pays that same per-item metadata cost only to conclude "nothing
changed." And because discovery is sequential, one slow upstream
metadata endpoint is head-of-line blocking: a single item can stall the
whole run for minutes. (§req:success-criteria #11, #12;
§req:quality-attributes: sync speed)

This section describes the end state: three levers, under one politeness
bound.

### Shared source concurrency budget §spec:source-budget

All of a source's concurrent traffic to its upstream host — per-item
metadata calls during discovery and file downloads during fetch, across
every collection syncing at once — draws from a **single shared
concurrency budget per source**: the hard ceiling on simultaneous
connections Shakedown opens to the host, which the ceiling can never
exceed no matter how many collections run concurrently.

**Why per-source, not per-collection.** Politeness is a property of the
upstream host, not of any one collection: archive.org sees the sum of
every connection Shakedown opens across all collections. A
per-collection budget would let each of `max_concurrent_collections`
collections open its own quota and sum past archive.org's tolerance,
risking throttling or a blacklist — the exact failure
§req:quality-attributes ("politeness to the source") forbids. A single
per-source budget spanning discovery and fetch is the only bound that
holds regardless of internal parallelism.

**Tradeoff accepted.** Collections of the same source no longer get
independent throughput — one collection's downloads and another's
discovery compete for the same slots. This is correct: the protected
resource is the upstream host's goodwill, which is genuinely shared.
**Rejected:** two independent pools (a discovery pool plus the existing
fetch pool) each individually polite — their *sum* is unbounded, which
is precisely the blacklist risk.

### Parallel, pipelined discovery §spec:discovery-pipeline

Per-item metadata resolution runs concurrently rather than serially,
and download begins while discovery is still in progress: as each item
is discovered and classified `new` or `changed-upstream`, it is handed
to the fetch stage without waiting for the rest of the enumeration.
Discovery and download overlap, both drawing from the shared source
budget (§spec:source-budget). Because file downloads take far longer
than metadata calls, overlapping them hides most discovery latency
behind download time on a first full sync.

**The enumeration-completeness contract.** A per-item fetch decision
(`new`/`changed-upstream`/`unchanged`) is a comparison of one item's
current manifest against its recorded manifest (§spec:sync-identity) and
needs no knowledge of the rest of the collection — so it is safe to act on
the moment an item is discovered. But the prune/`disappeared` decision
("in the database but not in this discovery") requires the *complete*
enumeration, so it is a **post-discovery barrier** that runs only after
discovery finishes successfully, never against a partial set. The load-
bearing invariants:

- An item may be fetched and staged before discovery completes.
- Pruning runs only after a complete, successful enumeration.
- A partial or failed enumeration prunes nothing and marks the
  collection stale (§spec:failure-behavior) — a failed discovery is
  never read as items having disappeared.

**Tradeoff.** Run counters accumulate from concurrent producers
(discovery) and consumers (fetch), so per-run counts must be exact under
concurrency. The invariant is what matters (counts are exact), not the
mechanism.

### Incremental discovery §spec:incremental-discovery

*Status: implemented*

Opt-in per collection (`incremental_discovery`, default off,
§spec:configuration). When enabled, discovery requests a cheap per-item
**change signal in the search response itself** — a source-provided
token that moves whenever the item's contents change (for IA, the
item's update timestamp together with its total size) — and persists it
alongside the recorded manifest (§spec:state). On a later sync, an item
whose current signal matches the stored one is classified `unchanged`
**without** the per-item metadata round-trip. An item whose signal
differs, or that has no stored signal, falls through to the full
manifest comparison (§spec:sync-identity) exactly as today. This
collapses a recurring no-op sync from N metadata calls to a single
paginated search (§req:success-criteria #11): a weekly run's wall-clock
scales with what changed, not with collection size.

**Correctness bound — speed never costs a missed change**
(§req:quality-attributes). The fast-path is a *skip*, never a
*substitute*: it may only conclude "unchanged," and only when the
source's own signal says the item has not changed. It must never
conclude "unchanged" for an item the full manifest comparison would
flag `changed-upstream`. This holds when the signal is monotone in the
item's contents — any change that would alter the recorded manifest (a
file added, removed, resized, or re-derived with a new checksum) also
moves the signal. The design leans on this invariant of the source's
update semantics; a source that cannot guarantee it is not offered
incremental discovery.

Because that invariant is a property of the upstream — not something
Shakedown can prove per run — three guards make the opt-in safe:

- It is **off by default**: the correct-by-construction full comparison
  is what every collection gets unless an operator opts in.
- The signal combines **both** an update timestamp **and** total size,
  so a change moving either is caught even if the other is stale.
- **`verify --deep`** (§spec:verify-drift) remains the periodic
  byte-level safety net that catches anything a signal ever missed.

**Rejected.** Making the cheap-signal skip the *default* (fastest, but
silently trades the project's load-bearing correctness guarantee on an
unprovable upstream invariant — §req:quality-attributes). **Rejected:**
replacing the manifest comparison with the signal outright — the
manifest stays the source of truth for "do I have it?" and for
`changed-upstream` detection; the signal only gates whether it is worth
a metadata fetch to find out.

### Slow-metadata resilience §spec:slow-metadata

A single slow or timing-out upstream metadata response never stalls the
whole run. Parallel discovery (§spec:discovery-pipeline) removes
head-of-line blocking — a slow item holds one budget slot while the
others proceed — and per-item metadata requests carry a **bounded
timeout and retry envelope** matched to the upstream's real latency.
Transient discovery-time faults (slow, 5xx, or 429) are classified and
backed off with the same discipline the fetch path owns
(§spec:failure-behavior), so mere transient slowness never surfaces as an
enumeration failure.

**Why both parallelism and a tuned timeout** (issue-complementary).
Parallelism removes the *blocking* — the run no longer waits on the slow
item; the tuned timeout reduces the *wasted time per* slow item. Either
alone leaves value on the table; together they satisfy
§req:success-criteria #11's "never stalls for minutes on a single slow
upstream response."

## Source plugins §spec:source-plugins

*Status: complete*

Sources are pluggable so that new archives can be added without core
changes (§req:constraints). A plugin implements `discover`, `fetch`, and
`verify`, plus a capability declaration (`type_name` and the metadata
`template_fields` it surfaces for library layout, §spec:library-staging).
The authoring contract — exact method signatures, the durability and
backoff the core owns, and a worked example — is documented for third-party
authors in [`docs/plugins.md`](docs/plugins.md).

The seam's load-bearing invariant: **plugins never own durability.** A
plugin only writes into a fresh, core-owned temp directory and reports an
outcome; the core performs the atomic archive promotion, the bounded
retries with backoff (honoring a plugin-surfaced `Retry-After`), and the
post-fetch completeness guard (§spec:sync-workflow, §spec:failure-behavior).
This keeps every plugin small and correct-by-default — the hard parts can't
be reimplemented wrong per source. `verify` is existence-only by contract;
byte hashing lives only in the core's `verify --deep` (§spec:verify-drift).

The core — not the plugin — owns discovery concurrency and its politeness
bound (§spec:source-budget): a plugin's `discover` remains a simple
per-item enumeration, and the core parallelizes and pipelines it under the
shared budget. A plugin whose source exposes a cheap per-item change signal
may surface it on the descriptor so the core can offer incremental
discovery for that source (§spec:incremental-discovery); a plugin that
cannot is unaffected and simply always resolves full manifests.

Plugins shipped in v1:

- **Internet Archive (`ia`).** Wraps the official `internetarchive`
  Python package for search, download, and per-file checksum
  verification — those problems are solved; the plugin does not
  reimplement them. Optional credentials (for download-restricted
  items) are read from environment variables only.
- **etree (`etree`).** A second, independently written plugin that
  proves the seam is real (§req:success-criteria #8): it drives
  archive.org's public HTTP/JSON API directly — a different integration
  path from the `internetarchive` package — and was authored from
  `docs/plugins.md` alone, without reading the IA plugin's internals.

Plugins are built-in and registered in code in v1; external plugin
distribution is deferred. **Why:** a packaging/distribution story is a
project of its own, and the requirement the seam must satisfy in v1 is
"a competent user can add a source type by writing one class against a
documented interface" — not "installable third-party ecosystem."

## Library staging §spec:library-staging

*Status: complete*

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

*Status: implemented*

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

The discovery-performance increment adds a per-source politeness ceiling
(the shared concurrency budget of §spec:source-budget) and a
per-collection `incremental_discovery` opt-in
(§spec:incremental-discovery), both validated at load time like every
other key (`extra="forbid"`, so an unknown or misspelled key fails the
run rather than being ignored).

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
  For collections using incremental discovery it also records the
  item's last-seen source change signal (§spec:incremental-discovery),
  the token a later sync matches against to skip a metadata fetch.
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

*Status: implemented*

After a sync that staged at least one item for a collection, the
system fires that collection's `on_complete` action — this is how new
shows flow into the library tool with nobody watching
(§req:user-stories). Two mechanisms, webhook as default and exec as
fallback (§req:constraints), both fired once per (collection, run) with
the `{source}`/`{collection}`/`{staging_root}` template fields available
in the URL or command:

- **Webhook** (`on_complete.webhook: <url>`): HTTP POST of a versioned
  JSON body (`payload_version`, `event: sync.complete`, a `run` counts/
  timing object, and a `staged` array of per-item identifier + archive/
  staging paths). See `notify.py` for the exact shape.
- **Exec** (`on_complete.exec: <command>`): a configured command run
  with the same JSON on stdin (e.g. `beet import -q {staging_root}`).
  Exists because many library tools have a CLI but no HTTP listener.

**Why once-per-run with a batch payload, not once-per-item.** A weekly
sync can stage dozens of items; per-item firing would hammer the
receiver, and library imports are naturally batch operations (`beet
import` on a directory). The `staged` array preserves per-item
precision for receivers that want it. **Why webhook is the default:**
loose coupling — the library tool needs no Shakedown-specific code,
and the contract is one JSON document. **Why versioned:** downstream
integrations can rely on the shape and detect a breaking change.

Failure notifications: a global `notifications.on_failure.webhook`
fires (same envelope, `event: sync.failed`, plus an `errors` array)
when a run fails — a stale source enumeration or one or more failed
items. Handoff/notification delivery failures are logged and recorded
in the run's errors — visible in `status` — but never fail the sync
itself: the mirror's integrity does not depend on a listener being up.
Payloads contain paths and counts only, never credentials.

## Serve control plane §spec:serve

*Status: implemented*

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

**The scheduler launches a fresh container per tick.** Because
Shakedown is a one-shot, stateless-between-runs CLI (§req:constraints),
no long-lived worker container exists to attach to. Each scheduled
tick therefore *starts a new container* from the image, runs one
`sync --collection <name>`, and exits — matching the batch design.
When a tick fires, the sync runs; "the schedule is configured" and
"the sync ran" are the same event (§req:success-criteria #1,
§req:quality-attributes "scheduled runs are observable and
self-contained").

Long-running services in the stack:

- **`ofelia`** — the scheduler. Runs fresh per-tick containers from
  the Shakedown image, one job per collection, on cron schedules
  defined as Docker labels. Each job carries the same
  archive/library/config mounts and `IA_*` credentials as a manual
  run, mounts config read-only, and reaps its finished container.
- **`shakedown-serve`** *(optional)* — the only long-lived Shakedown
  process, when present: the auth-protected control plane
  (§spec:serve) for status/metrics and ad-hoc triggers without SSH.
- Library tool and streaming server (Beets, Navidrome, …) join the
  same stack, reading the library tree — the user's choice; Shakedown
  places no requirements on them.

There is **no idle standalone `shakedown` service**. A one-shot
container that exits at bring-up and then sits `Exited` does no work,
cannot be a scheduler target, and reads as the worker while doing
nothing — so it is omitted. The Shakedown image is still present in
the stack, referenced by ofelia's per-tick jobs and (optionally) by
`serve`.

All data paths live under a single share
(`/share/data/{archive,library,music,shakedown-config}`) so archive
and library share a filesystem (§spec:system-shape).

**First bring-up does no unscoped work.** A freshly deployed stack
does not sync anything on `up`; each collection syncs on its own
schedule. There is deliberately no implicit "sync everything at
startup" — with many configured collections that would be surprising
and expensive (§req:quality-attributes). A user who wants an
immediate first run triggers one explicitly (see manual triggers).

**Manual triggers (no-SSH-first, SSH supported).** To run a sync
without waiting for the next tick:

- **Primary, no SSH:** `POST /sync` on the `serve` control plane
  (bearer-token authed, §spec:serve).
- **Fallback:** start a one-off container from the same image with
  `sync --collection <name>` — via Container Station's *Create
  Container* UI (no SSH) or `docker run` over SSH. This is the manual
  equivalent of a tick; ofelia itself has no "run now" command, so a
  one-off is simply launching the image by hand.

SSH remains fully supported and is documented as guidance, not
designed out; the web-UI paths exist so it is never *required*
(§req:success-criteria #6).

**Why ofelia, launching fresh containers, rather than host cron, a
built-in scheduler, or exec-into-a-running-container.** Ofelia is
itself a container, so the entire deployment — including timing — is
one compose YAML editable in Container Station: no host cron, no SSH,
no scheduler code in Shakedown. Launching a fresh container per tick
(rather than `exec`-ing into a persistent one) is what keeps Shakedown
one-shot: there is no daemon to keep alive, and a tick can never
silently no-op because a prior run has exited. Schedule changes are
compose-file label edits; "did the Sunday sync run?" is answerable
from the per-tick container logs. Missed schedules are harmless
because sync is idempotent at any cadence (§spec:sync-workflow).
Accepted tradeoffs: per-tick job definitions repeat the mount/env
wiring and live on the scheduler rather than on a worker service (more
verbose than a `schedule:` key in shakedown.yaml would be) — accepted
for exactly one source of timing truth (§spec:configuration); and
ofelia requires access to the host Docker socket to start containers
(mounted read-only), whose blast radius is host-root-equivalent — an
unavoidable property of any in-stack container scheduler, called out
so operators place the stack accordingly.

Setup flow (web-UI-only): create the share folders in File Station,
upload an initial `shakedown.yaml`, paste the compose YAML into
Container Station with credentials as environment variables. Day 2:
collection changes via File Station (YAML edit), schedule changes via
Container Station (compose edit), logs via Container Station.

## Failure behavior §spec:failure-behavior

*Status: implemented*

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
| Source rate-limits | Core honors the plugin-surfaced `Retry-After`, backs off, resumes |
| Slow/timing-out metadata during discovery | Bounded per-item timeout + back-off; other items proceed in parallel — one slow endpoint never stalls the run (§spec:slow-metadata) |
| Discovery fails partway (pipelined) | Collection marked stale; nothing pruned — a partial enumeration is never read as items disappearing (§spec:discovery-pipeline) |
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

## Real-source end-to-end check §spec:e2e-real-source

*Status: implemented — `tests/test_e2e_real_source.py` (opt-in `network`
marker), run instructions and pinned-item replacement procedure in README.*

The entire test suite runs offline against fakes and mocked HTTP.
That leaves two failure classes invisible: archive.org's real API
drifting away from what the fixtures encode, and integration seams
that only misbehave with real files on a real filesystem. The
real-source check closes both gaps and doubles as a manual
pre-release gate (§req:success-criteria #10).

Observable behavior:

- One documented command (`pytest -m network`) runs the check on a
  clean machine; the default test invocation and CI select zero
  network-dependent tests (§req:constraints). Opting in is the only way
  to reach the network.
- The check drives the `ia` plugin against exactly one pinned, small (a
  few megabytes), public, unrestricted Internet Archive item — no
  credentials required — through the typical lifecycle in order: real
  `sync` (core verifying source checksums as bytes arrive,
  §spec:sync-workflow); inode-sharing library staging
  (§spec:library-staging); a no-op re-`sync` (§spec:sync-identity);
  `restage` after a `library_layout` change (no network); the item
  leaving enumeration → `disappeared` with files retained by default,
  then pruned once `prune_disappeared` is set, so the retain→prune
  transition is exercised, not just retain (§spec:item-lifecycle); and
  finally `item forget` dropping the record.
- The check runs entirely in throwaway temporary trees (archive,
  library, state DB) on one filesystem; it can never touch a real
  deployment's data.
- A network or upstream failure fails the check loudly. It never
  skips itself into a false pass — a release gate that silently
  degrades is worse than none.

**Why one pinned item.** The check exists to prove the pipeline, not
to mirror content: one small item keeps runtime and bandwidth bounded
and stays polite to archive.org (§req:constraints). The identifier is
pinned — not queried live — so runs are comparable over time; the
selection criteria (public domain or freely licensed, unrestricted,
few megabytes, long-lived) and the replacement procedure when the item
ever disappears upstream are documented alongside the check.

**Why opt-in rather than scheduled CI.** Real-network tests are
inherently flaky and consume upstream bandwidth; putting them in CI
either blocks PRs on upstream hiccups or trains developers to ignore
red. The check's three jobs — real-API drift detection, whole-pipeline
integration confidence, and a pre-release gate — are all served by a
developer running it deliberately before tagging a release
(§req:user-stories).

**Rejected alternatives.** Recorded-response replay (cassettes) was
rejected because it re-mocks the network and cannot detect API drift —
the primary failure class the check targets. Exercising the `etree`
plugin as well was deferred: the IA plugin is the primary integration
path, etree already has an offline end-to-end slice, and doubling the
real downloads buys little; the harness does not preclude
parameterizing over plugins later.
