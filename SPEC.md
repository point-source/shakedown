# Shakedown — Specification

> Shakedown maintains durable local mirrors of music collections from
> open archives and presents them to library tools through a hardlink
> staging tree. This document is the solution-space companion to
> [REQUIREMENTS.md](REQUIREMENTS.md): it records what the system does
> and why — architecture, design decisions, tradeoffs, and rejected
> alternatives. Work breakdown lives in ROADMAP.md. Together with
> REQUIREMENTS.md, this document supersedes the original PRD.

## System shape §spec:system-shape

*Status: complete*

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
  counterpart. Renaming or deleting the library-side link never affects
  the archive-side file or Shakedown's record of it, and vice versa. An
  in-place retag through either path changes the shared bytes of both,
  but never Shakedown's record of the item (§spec:sync-identity).
- At startup, the system shall verify that a file in `archive_root` can
  be hardlinked into `library_root` — by making and removing a real test
  link — and refuse to run with a clear error if it cannot.
  (§req:constraints) Comparing device IDs is not enough: Linux refuses
  links across separate mounts of one filesystem, such as archive and
  library bind-mounted into a container separately.

**Why hardlinks, not symlinks or copies.** Symlinks dangle when a
library tool moves the target into its own managed root — Beets does
this by default. Copies double storage and let the two trees diverge
silently. Hardlinks make both paths first-class references to the same
bytes: either can be deleted without affecting the other, storage cost
is 1x (§req:quality-attributes), and the mechanism is
filesystem-native with zero runtime overhead. The accepted tradeoff is
the same-filesystem constraint, which the QNAP deployment satisfies by
putting both trees under one share, mounted into the container as one
volume (§spec:deployment). This is the
same seam the *arr ecosystem uses for its download→library handoff.

## Sync identity §spec:sync-identity

*Status: complete*

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

*Status: complete*

Every discovered item carries a persistent status:

| Status | Meaning |
| --- | --- |
| `pending` | Discovered, not yet attempted |
| `downloading` | Fetch in progress |
| `complete` | Fetched successfully; manifest recorded |
| `failed` | Fetch attempted and failed; retried on the next run |
| `unavailable` | Source explicitly disallows download (e.g. stream-only, flagged in its metadata or refused when downloading) or lists no audio files; a human-readable `restriction_reason` is recorded and shown in `status` |
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
  of disappeared items. An item the source still lists but whose
  per-item metadata failed to resolve this run is **not** `disappeared`
  — it is left as recorded and retried, since only absence from a
  successful enumeration marks disappearance (§spec:prune-safety).
- **Removed upstream is a fact, not an error.** When a show becomes
  `disappeared`, the system records the date it was first found
  missing from a complete listing. A retained show stays on disk and in
  the library for playback, and `status` shows it informationally —
  "removed upstream on \<date>; kept" (text: a count per collection;
  JSON: each show with its date) — never as a warning, a recovery item,
  or a cause of a non-zero exit. If the show is listed again, it returns
  to `complete` and the date is cleared. (Operator decision,
  2026-11-10.) There is no command to drop a show's record while
  keeping its archive copy: anything in the archive is also in the
  library, so a show leaves both only through `prune_disappeared`
  (operator decision, 2026-11-10; `item forget` was removed).

**Why retention is the default.** Durable mirroring is the product's
reason to exist; losing content must only ever be the user's explicit
choice, never an upstream side effect. Pruning exists because strict
mirror-the-source-exactly users are real, but it is opt-in per
collection so a config typo can't silently discard recordings.

## Sync workflow §spec:sync-workflow

*Status: complete*

A sync run for a (source, collection) pair proceeds in six phases:

1. **Discover.** The source plugin lists all items currently matching
   the collection's query; the core looks each one up and chooses its
   files, giving each item's current manifest and metadata
   (§spec:per-item-isolation).
2. **Plan.** Each remote item's current manifest is compared against
   the recorded manifest (§spec:sync-identity) and classified:
   `new` (not in DB), `unchanged` (manifests match — skip),
   `changed-upstream` (manifests differ — refetch), or `unavailable`
   (source disallows — record reason). Each DB item absent from the
   discover results is classified `disappeared`.
3. **Fetch.** For `new` and `changed-upstream` items, the plugin
   downloads into a temporary directory inside the archive tree,
   verifying source-provided checksums as bytes arrive. On full
   success the core checks each file's size against the manifest
   (retrying a short file like any truncated download), flushes the
   files and the rename to disk, then atomically renames the temp
   directory to its final archive location and persists the manifest
   — so a power cut never leaves a short file recorded `complete`. A partial failure
   leaves only the temp directory; the next attempt (or run) keeps
   the files in it that match the manifest's size and checksum and
   fetches only the rest — no partial state ever reaches the archive
   tree or the database. After a complete enumeration, temp
   directories nothing will resume (the item is unchanged,
   unavailable, or gone upstream) are removed; those of items that
   failed this run are kept for the next run.
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
- **Waiting collections.** A multi-collection sync (an all-collections
  or all-of-a-source sync, or a catch-up, §spec:rescheduled-syncs) runs
  at most `max_concurrent_collections` collections at once; the rest
  wait, in order, for a slot. A collection's run record and operation
  outcome open when it takes a slot, and its run is timed from then.
  Until then it has only a waiting entry (§spec:run-activity "Waiting
  collections"), so `status`, crash recovery, and run history see no
  run of it — never a stuck or crashed one. A stop that comes while it
  waits means it never starts (§spec:graceful-stop). Before this, every
  collection's run record opened when the sync began, so collections
  waiting their turn read as runs stuck in progress, each with a
  recovery warning (operator decision, 2026-11-20).

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

*Status: complete*

The shared per-source concurrency budget
(§spec:source-budget), parallel per-item discovery and discover→download
pipelining (§spec:discovery-pipeline, both levers), the bounded slow-metadata
envelope (§spec:slow-metadata), and opt-in incremental discovery
(§spec:incremental-discovery) are all in place.

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

**Shared across sources on one host.** Two configured sources can reach
the same host — an `ia` source and an `etree` source both talk to
archive.org. A plugin names its upstream host; every source whose plugin
names the same host draws from one budget, sized to the smallest
`max_concurrent_requests` among them, so configuring a second source never
doubles the connections a host sees. A plugin that names no host gets a
budget per source.

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

- An item may be fetched and staged before discovery completes. Each
  finished download is recorded and linked as soon as it completes,
  not when discovery ends, so a run that dies mid-discovery never
  leaves finished downloads unrecorded.
- Pruning runs only after a complete, successful enumeration.
- A partial or failed enumeration prunes nothing and marks the
  collection stale (§spec:failure-behavior) — a failed discovery is
  never read as items having disappeared.
- Within a successful enumeration, an item that was listed but whose
  per-item *description* failed is treated as present-but-unresolved and
  is never pruned — enumeration, not description, is the prune signal
  (§spec:prune-safety).

**Memory stays flat as a collection grows.** A show's details (metadata,
manifest) are held only while that show is handled, never for the whole
listing: per-item lookups run only a few ahead of the consumer, the
listing pauses while a few shows already wait for a download, and the
pre-run snapshot of recorded items leaves out their source metadata.
Overlap is kept — downloads still begin while discovery is in progress —
but discovery never races ahead of downloads, so a first sync of a large
collection fits a small NAS.

**Tradeoff.** Run counters accumulate from concurrent producers
(discovery) and consumers (fetch), so per-run counts must be exact under
concurrency. The invariant is what matters (counts are exact), not the
mechanism.

### Incremental discovery §spec:incremental-discovery

*Status: complete*

Opt-in per collection (`incremental_discovery`, default off,
§spec:configuration). When enabled, discovery requests a cheap per-item
**change signal in the search response itself** — a source-provided
token that moves whenever the item's contents change (for IA, the
item's update timestamp together with its total size) — and persists it
alongside the recorded manifest (§spec:state). On a later sync, a `complete`
item whose current signal matches the stored one is classified `unchanged`
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

## Prune safety: enumeration versus description §spec:prune-safety

*Status: complete*

On a `prune_disappeared` collection, a transient failure to fetch a
*single item's* metadata during discovery must not cost the user that
item's archive files. Discovery is two steps: a cheap **enumeration**
that returns the collection's current identifiers (with change signals)
and a per-item **description** that resolves each identifier's full
manifest (§spec:discovery-pipeline). When an item is enumerated but its
description fails — its bounded retry envelope (§spec:slow-metadata)
exhausted on a transient fault — that item must not be read as having
left the collection. It was still listed by the source; only its
metadata call failed. (§req:success-criteria #13,
§req:quality-attributes: durable mirroring)

The system distinguishes three post-enumeration outcomes for a
DB item, not two:

| Enumeration listed it? | Description resolved? | Outcome |
| --- | --- | --- |
| Yes | Yes | Normal plan: `unchanged` / `changed-upstream` |
| Yes | No (transient) | **Present-but-unresolved**: left exactly as recorded — never pruned or flagged `disappeared` — and reconsidered next run |
| No | — | `disappeared` (retained by default; pruned only if opted in) |

- Prune/`disappeared` eligibility is keyed on **absence from a
  successful enumeration**, never on absence from the set of
  successfully-*described* items. An identifier the enumeration returned
  counts as still present for prune purposes even when its description
  failed this run.
- A present-but-unresolved item's DB row is untouched — status, recorded
  manifest, and files left as they were — so it is simply reconsidered
  next run, when its metadata call may succeed. It is not marked
  `failed`: it was already `complete` and nothing was attempted against
  its bytes.
- This finer distinction applies only on the successful-enumeration
  branch, where the prune barrier runs at all. A partial or failed
  *enumeration* still marks the whole collection stale and prunes
  nothing (§spec:discovery-pipeline); that pre-existing barrier is
  unchanged.

**Why enumeration is the sole prune signal.** Enumeration answers the
collection-level question "what does the source list right now?" — the
only signal that actually reports whether an item left the collection.
Description answers a different question ("what files does this item
have?"), and its failure reflects one metadata endpoint's momentary
health, not the item's membership. Conflating the two let a transient
hiccup masquerade as a takedown — precisely the failure the
durable-mirroring guarantee forbids. **Rejected:** treating a describe
failure as `failed` and leaning on retry/backoff — the identifier still
falls out of the run's seen set and so still prunes; the fix must be in
what feeds the prune barrier, not in retry counts. **Rejected:** tripping
the stale-collection barrier on any per-item describe failure — that
would discard the whole run's plan, and every legitimate fetch in it,
because one item's metadata was slow, throwing away the pipelining the
discovery-performance increment bought (§spec:discovery-pipeline).

## Layout collision safety §spec:layout-collision-safety

*Status: complete*

Includes the config-time guard, and runtime separation
(§spec:library-ownership).

A `library_layout` that cannot distinguish two items in the same
collection renders them to the same library folder. A live-music
collection with multiple recordings of one show (different tapers or
sources, same date and venue) hits this whenever the layout keys on
date/venue with no per-item-unique component. Before separation, the
later recordings were silently dropped from the library behind a buried
per-file `WARNING`. (§req:success-criteria #14, §req:quality-attributes:
no silent library data loss)

Two defenses, one proactive and one at runtime:

- **Config-time guard.** A source plugin declares which of its
  `template_fields` are **per-item-unique** — guaranteed to distinguish
  any two items in a collection (for the IA and etree plugins,
  `identifier`; §spec:source-plugins). At load time, a non-`passthrough`
  `library_layout` that references no per-item-unique field draws a named
  warning naming the collection and suggesting a unique component (e.g.
  `{identifier}` in the leaf). A non-passthrough template that references
  **no plugin field at all** — one constant path for every item, a
  guaranteed total collapse — is a hard config error, consistent with the
  fail-fast config contract (§spec:configuration). So is a filter name
  the template engine does not define (valid: `sanitize`, `short`,
  `lower`, `upper`); a typo like `|sanitise` would otherwise fail
  mid-sync, or mid-restage after the library was cleared.
- **Runtime separation.** When items still render to the same folder at
  run time, no recording is dropped: every clashing item is separated
  into its own folder by its identifier, the same way on every run and
  after a restage, and each separation is reported in the run summary
  and in `shakedown status` (§spec:library-ownership). This replaces the
  earlier skip-the-loser behavior, whose dropped recordings and
  order-dependent winner were the defect criterion 19 removes.

**Why a warning, not a hard error, for the missing-unique-field case.**
Uniqueness of an arbitrary *combination* of fields is undecidable from
config alone — `{date} - {venue} - {title}` is unique in practice for
most collections yet references no single guaranteed-unique field, so
hard-failing it would reject legitimate layouts. The plugin can vouch
only that `identifier` is always unique, so the load-time check warns (a
proactive heads-up before the first sync) while runtime separation is
the correctness backstop — it acts only on an *actual* clash and can
never false-positive. The fully-constant template is the one case config *can*
prove will collapse, so it errors. **Rejected:** silently trusting the
template (the status quo, whose permitted data loss is the bug).
**Rejected:** hard-erroring any layout lacking a guaranteed-unique field
— it rejects unique-in-practice templates and would break existing
working configs on upgrade.

## Source metadata preservation §spec:metadata-preservation

*Status: complete*

For archival and live-music collections the listing context — lineage,
source, taper notes, setlist, description — is often as valuable as the
audio, but it reaches only the state DB (as `source_metadata`,
§spec:state) and never the library. A collection may opt in with
`preserve_source_metadata` (default off, §spec:configuration) to carry
that context into the library as a `metadata.json` sidecar
(§req:success-criteria #15).

Observable behavior when enabled:

| Operation | Sidecar effect |
| --- | --- |
| `sync` fetch of a new/changed item | Core writes the item's full raw metadata dict (the same one recorded as `source_metadata`) to `metadata.json` in the fetch temp dir **before** atomic promotion (§spec:sync-workflow), then hardlinks it into the library staging dir beside the media (§spec:library-staging). |
| `sync` re-run, metadata edited upstream | No effect: the sidecar is **excluded from the recorded manifest**, so an edit to prose never flips an item to `changed-upstream` or triggers a media re-download (§spec:sync-identity). |
| `restage` / library wipe | Sidecar reappears with no network — regenerated from the DB `source_metadata` when the archive copy is absent (§spec:state). |
| `sync --refresh-metadata` | Explicit, operator-invoked: re-resolves metadata for already-mirrored preserve-opted items under the shared politeness budget (§spec:source-budget), rewrites each `metadata.json` and its recorded `source_metadata`, and restages — without re-downloading media (§spec:cli). |

The sidecar is written by the **core**, not the plugin: every plugin
already surfaces this metadata dict on its descriptor, so preservation is
source-agnostic with no plugin-contract change and the seam invariant
that plugins never own durability holds (§spec:source-plugins). Writing
before promotion means it lands in the archive atomically with the media
or not at all; excluding it from the manifest means it stages as an
explicit additional link, not a manifest entry.

**Why a manifest-excluded sidecar, refreshed on demand.** Putting the
sidecar in the manifest was the simplest route to auto-propagation, but
it would make every upstream notes edit churn a media re-fetch and force
the sidecar through `verify` and existence checks — coupling the media
mirror's change detection to volatile prose. Excluding it keeps the media
identity guarantee (§spec:sync-identity) intact and makes metadata a
separate, explicitly-refreshed concern; keeping refresh explicit is what
lets change detection ignore metadata drift without the library's context
going permanently stale. **Rejected:** manifest-included sidecars (churn,
coupling). **Rejected (this increment):** granular per-field sidecars
(`description.txt`, `notes.md`) — the full `metadata.json` carries every
field losslessly and is source-agnostic, whereas splitting fields invites
source-specific naming the plugin contract would then have to
standardize; per-field extracts can be layered later without changing
what is preserved.

## Source plugins §spec:source-plugins

*Status: complete*

Includes the core-owned listing/lookup contract
(§spec:per-item-isolation) and network measurement for the dashboard
(§spec:network-activity).

Sources are pluggable so that new archives can be added without core
changes (§req:constraints). A plugin lists a collection, looks up one
item, fetches, and verifies (the listing/lookup split and what the core
owns around it are specified in §spec:per-item-isolation), plus a
capability declaration (`type_name`, the metadata
`template_fields` it surfaces for library layout, §spec:library-staging,
and which of those fields are **per-item-unique** so the config layer can
guard against lossy layouts, §spec:layout-collision-safety).
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
bound (§spec:source-budget), the retries and per-item isolation of every
lookup, and the choice of which listed files to mirror
(§spec:per-item-isolation): a plugin answers single requests, and the
core parallelizes and pipelines them under the shared budget. A plugin whose source exposes a cheap per-item change signal
may surface it on the descriptor so the core can offer incremental
discovery for that source (§spec:incremental-discovery); a plugin that
cannot is unaffected and simply always resolves full manifests.

The core, not the plugin, measures network activity for the dashboard
(§spec:network-activity): bytes from the core-owned temp folder, requests
in flight from the budget, and events from attempt outcomes and the
failure cause a plugin reports with each failed request, the same cause
that drives retry decisions. A plugin
needs nothing dashboard-specific; the one exception is a plugin that
retries its collection listing itself, which reports each retry to the
core (the IA plugin does).

Plugins shipped in v1:

- **Internet Archive (`ia`).** Wraps the official `internetarchive`
  Python package for search and download — those problems are solved;
  the plugin does not reimplement them. That package checksums only
  files already on disk before a download, never the bytes it just
  wrote, so the plugin checks each downloaded file's size and md5
  against the manifest before reporting success. Optional credentials
  (for download-restricted items) come from environment variables or
  from the dashboard (§spec:dashboard-credentials); the plugin logs in
  to archive.org with the email and password and signs its requests
  with the keys and cookies the login returns.
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

## Per-item isolation on every source §spec:per-item-isolation

*Status: complete*

Includes the core's file-selection rule
(§spec:quality-selection).

A server error, timeout, or malformed file listing for one item marks
only that item failed for the run; every other item still syncs and the
collection is not reported stale. This holds for every shipped source
and for any plugin written from the documented interface, with no
effort from its author (§req:success-criteria #22,
§req:quality-attributes: resilience — per-item isolation is a property
of the product, not of each plugin).

Observable behavior:

- **The plugin answers single requests; the core owns the rest.** A
  source plugin provides: *list a collection* (its current identifiers,
  each with the source's change signal when it has one); *look up one
  item* (the item's metadata, any item-level restriction, and the full
  list of files the source offers — each with name, size, checksum,
  format, whether the source refuses it, and which original file or
  files it is derived from, when it is a derivative); *fetch* given files into a
  core-owned temp folder; and *verify* existence. The plugin writes no
  retry loop, no concurrency, and no error handling for per-item faults.
  The serial plugin-driven `discover` iterator is removed.
- **Every lookup is isolated.** The core runs each item lookup under the
  shared source budget (§spec:source-budget) with a bounded timeout and
  retry envelope, honoring the source's `Retry-After`
  (§spec:slow-metadata). A transient fault (timeout, connection error,
  5xx, 429) is retried within the envelope; a malformed answer (not the
  expected shape, a file entry without a name, a non-numeric size or
  checksum) fails at once. Either way, when the lookup does not yield a
  valid description, that item is listed in the run's item failures with
  a plain-language reason and retried next run — a new item is recorded
  `failed`; an item already mirrored keeps its recorded status and files
  untouched (§spec:prune-safety). Any exception a plugin raises from a
  lookup is treated the same way. The run's exit code reflects item
  failures as it does today; the collection is not marked stale.
- **Only the listing can make a collection stale.** A failure to list
  the collection (after its own retries) still marks it stale and
  prunes nothing (§spec:discovery-pipeline).
- **One file-selection rule for every source.** The core, not the
  plugin, decides which listed files to mirror: the best available and
  best playable copy plus every non-audio file
  (§spec:quality-selection). An etree collection therefore gets exactly
  the same selection, including the refused-lossless case, as an `ia`
  collection.
- **Every listing operation follows the same rules:** sync, the
  explicit metadata refresh, single-show operations
  (§spec:single-show-runs), `reconcile`, and `validate`'s source check.
  In particular, `reconcile` marks an item `disappeared` only when a
  complete listing omits it — never because its lookup failed.
- `docs/plugins.md` documents this contract and its worked example
  contains no retry or per-item error handling.

**Decision and constraint.** The core owns per-item lookup, its
retries, and file selection, because criterion 22 demands isolation
"without extra effort by its author" — a promise only the core can
keep — and because the shipped split proved the alternative fragile:
etree looks items up inside its own listing generator, where one raised
error ends the whole listing; IA's own retry envelope catches network
faults but not a malformed file list; and the fallback feature exists
only in the IA plugin. Deployment status allows the breaking plugin-API
change: no third-party plugins exist (§req:constraints).

**Alternatives rejected.** *Catch per-item exceptions around a plugin's
`discover` generator* — a generator that has raised cannot resume, so
the rest of the listing is lost anyway. *Keep file selection in each
plugin behind a shared helper* — parity by convention, which already
drifted (etree ignored the refused-lossless fallback). *A retry layer per
plugin* — two retry layers multiply requests against the politeness
budget, the failure the review found in the IA library's hidden
retries.

**Tradeoffs.** Plugins must expose the raw file listing including
format and derivation fields, and both shipped plugins are rewritten to
the new contract. The core's description validator becomes the one
place that defines a well-formed listing.

**User-level test path.** In a collection of many items, make one
item's lookup return a server error on every attempt and another's
return a file list with a nameless entry, on an `ia` collection and on
an `etree` collection. Sync: only those two items are listed failed
with reasons, the rest sync, `status` shows the collection current (not
stale), and the next run retries the two. On the etree collection, a
show whose lossless files are refused gets its best lossy copy
mirrored.

## Best available and best playable copy §spec:quality-selection

*Status: complete*

For every show, the system keeps the highest-quality version the source
offers and the highest-quality version a common player can play: one
copy when those are the same files, both when they differ. Every
non-audio file the source lists is kept too; the library gets the
uploader's own non-audio files, except archive files and system or
editor leftovers. Shakedown never converts audio. (§req:problem-statement; §req:success-criteria #26;
§req:constraints: the bounded alternate-format exception. Operator
decisions, 2026-11-10: the product's two goals are a local playable copy
of every configured collection and an archival-quality copy of the
music and its associated data in case the source is lost.)

Observable behavior:

- **The system chooses the audio; the operator doesn't.** Per show, the
  core ranks the source's audio files by format (§spec:per-item-isolation
  owns selection for every source): lossless (FLAC, SHN, WAV, AIFF,
  Apple Lossless) ranks above lossy; lossy copies rank by bitrate, taken
  from the source's per-file bitrate when it lists one and from file
  size per track otherwise. A format counts only when it covers every
  track of the show and the source serves it (files the source refuses
  are not available). A copy is the files sharing the source's format
  name; when no single name covers the show, the uploader's files of
  one audio class and file type count as one copy as long as together
  they hold exactly the show's track count and no track twice. A file
  whose real format the system has checked counts by that format, not
  its listing (§spec:real-format-ranking). The
  source's derivatives never merge: archive.org labels its own copies
  consistently, and its MP3s of different bitrates are separate copies
  of the same tracks. archive.org names a FLAC's format by its bit depth, so a show
  with one 16-bit track among 24-bit ones lists "24bit Flac" for most
  tracks and "Flac" for the rest (`aru2015-08-15.24Bit48khzMS`); its
  FLAC files are still the show's lossless copy. Names that each cover
  the show stay separate copies, ranked as below (operator report,
  2026-11-11). Among lossless copies that each cover every
  track, FLAC beats Apple Lossless beats WAV beats AIFF, then the larger
  size per track wins, so a 24-bit FLAC beats a 16-bit one and a WAV is
  never kept over a FLAC of the same audio; SHN ranks below every
  playable lossless format (operator decision, 2026-11-10). Audio in a
  format the product does not recognise ranks below every recognised
  one. archive.org's ZIP bundles of a show's audio (formats such as
  "VBR ZIP" or "64Kbps MP3 ZIP") are not audio copies and are left out
  (operator decision, 2026-11-10).
- **Best available, best playable.** The *best available* copy is the
  top-ranked format; the *best playable* copy is the top-ranked format a
  common player reads (everything above except SHN). If they are the
  same format — e.g. a show with FLAC originals — only that copy is
  kept. If they differ — e.g. an SHN-only show, whose playable copies
  on archive.org are VBR MP3 and Ogg Vorbis derivatives — both are kept.
  Measured on archive.org, VBR MP3 copies run about twice the bitrate of
  Ogg Vorbis copies (`gd77-05-08.sbd.hicks.4982.sbeok.shnf`: 8.8 MB vs
  4.3 MB per track), so the ranking picks the MP3 there.
- **Refused lossless files are handled automatically.** When the source
  refuses every lossless file (stream-only soundboards), the best
  available copy is the best lossy one, so it is mirrored as `complete`
  and `status` lists the show with the reason "lossless refused by
  archive.org; best lossy copy mirrored instead". When the lossless
  files are served again, the next sync mirrors them and takes the
  lossy copy out of the library if it is no longer the best playable
  one; it stays in the archive (see "Files the rule no longer selects
  stay").
- **Every non-audio file is kept; the library gets the uploader's.**
  Text notes, checksum files, the source's metadata files, artwork, and
  generated images are all mirrored to the archive. The library stages
  only the non-audio files the uploader provided — notes, checksums,
  cover and setlist images, booklet scans, PDFs and documents. Files
  the source derived from another file (on archive.org: spectrograms,
  waveform images, audio fingerprints, analysis data, thumbnails, OCR
  output, and checksum files it generated) and files it keeps about the
  item itself (archive.org's index and metadata files, reviews, torrent,
  and item tile) stay archive-only. A file's origin comes from the
  source's listing: whether it names an original, its format name, and
  archive.org's reserved `__ia_` name prefix. (Operator decision,
  2026-11-11: the generated files cluttered library folders, e.g. 6,784
  spectrogram images in one collection.)
- **Archive files and leftovers stay archive-only, whoever provided
  them.** Archive files (`.zip`, `.rar`, `.7z`, `.tar`, `.gz`, `.tgz`,
  `.bz2`, `.xz`, so also `.tar.gz` and the like), system files
  (`.DS_Store`, `Thumbs.db`, `desktop.ini`, macOS `._` resource forks)
  and editor backups (names ending in `~`, such as `~1~`, and `.bak`)
  are mirrored to the archive and never staged into the library. The
  rule reads the file name, in any folder and any letter case, so it
  holds for every source. A resource fork named like a track
  (`._t01.flac`) is not audio. Rip and recording logs (`.log`) and
  track-length listings (`.len`) are notes and stay staged. (Operator
  decisions, 2026-11-11: `aadam-jacobs` staged 181 raw recordings
  uploaded as `<show>_16bit48khz_rawfile.zip`, 48 GiB that Navidrome
  cannot play, plus editor backups and `.DS_Store` files.)
- **Where files land.** The archive holds the best available copy, the
  best playable copy when different, and the non-audio files; all are
  part of the recorded manifest, so change detection covers them. The
  library holds the best playable copy and the uploader's non-audio
  files other than archive files and leftovers; an unplayable
  best-available copy stays archive-only.
- **No playable copy.** A show whose only audio is unplayable is
  mirrored and staged as is, and `status` lists it as not playable,
  with the reason "only in SHN; archive.org offers no playable copy"
  (or "only in an unrecognised audio format; archive.org offers no
  playable copy"). It stays `complete`. A show whose listing holds no
  audio at all (darked, a glitch, or only archive.org's own metadata
  files) keeps its existing copy and is marked `unavailable` with the
  reason "source lists no audio files". (Operator decisions,
  2026-11-10.)
- **Configuration.** `format_filters`, `exclude_filters`, and
  `fallback_format_filters` are removed. A config that still sets them
  fails to load, naming the key (§spec:configuration); the operator
  deletes them once on upgrade.
- **Upgrading downloads only what is missing.** When a show's selection
  changes — on upgrade (non-audio files and playable copies added) or
  later (lossless served again, a fixed rule) — only files not already
  recorded in the archive are downloaded; files whose recorded name, size, and
  checksum are unchanged are kept without re-downloading or hashing
  (§spec:sync-identity, §req:constraints: an upgrade never forces
  re-downloading the bulk of the archive).
- **Files the rule no longer selects stay.** Audio already in the
  archive that the new rule would not select (e.g. a show mirrored with
  both FLAC and SHN under the old filters) is kept in the archive and
  left out of the library; the rule governs what is downloaded from now
  on, and no archive audio is deleted by a selection change. (Operator
  decision, 2026-11-10.)

**Decision and constraint.** Selection follows the two product goals
directly instead of per-collection format lists, because hand-written
filters can only approximate them and already failed in practice: an
exclude pattern can block the only playable copy, and the refused-
lossless fallback had to be added as a separate opt-in. "Quality" is
decidable from what the source reports (format class, bitrate, size), so
the rule needs no operator judgement per show. Keeping every non-audio
file serves the archival goal: notes and lineage are part of what would
be lost with the source. The library is for listening, so it gets what
the uploader added to the show and not what the source generated, nor
archives no player opens or files left behind by the uploader's
computer.

**Alternatives rejected.** *Keep the format settings and choose only the
extra playable copy by quality* — the rule then holds only where each
collection's settings allow it. *Transcode SHN to FLAC* — out of charter.
*Replace SHN with its playable copy* — loses the preservation original.
*Stage SHN beside the playable copy* — players list unplayable tracks.
*Mirror only text and checksums* — the operator chose every non-audio
file. *Stage every non-audio file* (v2.0.0) — thousands of generated
images and data files filled library folders. *Stage a fixed list of
notes, checksums and images* — drops booklet scans, PDFs and other
files uploaders add. *Group copies by file type alone* — would merge a
show's 24-bit and 16-bit sets, or one track's MP3s of different
bitrates. *Stage uploaded archive files* (v2.0.2) — players cannot open
them, and the raw recordings they hold repeat the show's audio.

**Tradeoffs.** The operator gives up per-collection control (for
example, mirroring only MP3 to save space). Shows with unplayable best
copies cost the extra playable copy (roughly 150 MB per show).
Generated files cost archive space but stay out of the library. A
show whose only lossless files split across format names and that has
no derivative yet (a fresh upload) may count the larger name group as
the whole show until archive.org derives its MP3s; the next sync then
fetches the missing track. If the source re-derives a playable copy,
its checksums change and only that copy is re-fetched.

**User-level test path.** Sync a FLAC show: the archive holds the FLAC
files and every non-audio file, the library holds the FLAC files and
the uploader's notes, checksums and artwork, and neither holds an MP3.
Sync `aru2015-08-15.24Bit48khzMS` (one track labelled "Flac", the rest
"24bit Flac"): both hold its 18 FLAC files. Sync
`ajc00286_eelpie1997-09-11`: the archive holds its
`_16bit48khz_rawfile.zip`, the library holds the FLACs, notes,
fingerprints and cover and no zip. Sync an SHN-only
Grateful Dead show: the archive holds SHN and the VBR MP3 copy; the
library holds the MP3 and the uploader's non-audio files, and no
spectrograms; Navidrome plays it. Sync a
stream-only soundboard: the best MP3 is mirrored and `status` gives the
reason. Upgrade a collection mirrored under `[flac, shn]` filters: the
first sync downloads only the added non-audio files and playable copies.

## Library staging §spec:library-staging

*Status: complete*

Including path ownership, clash separation, stale-copy cleanup
(§spec:library-ownership, §spec:library-cleanup), and the real-extension
exception (§spec:real-format).

Per collection, the user provides a `library_layout` template that
renders each item's **staging directory** under the library tree, from
fields the source plugin surfaces (e.g.
`{year}/{date} - {venue}/{identifier}`). The item's original archive
filenames are then hardlinked into that directory unchanged.

- **Templates produce directories, never renamed files.** Filenames in
  the deepest directory always match the archive, so tools (and users)
  can correlate the two trees by name. The one exception: a file whose
  content is in a different audio format than its name says keeps its
  stem but carries its real extension in the library
  (§spec:real-format). Renaming and retagging are the
  library tool's job, out of scope by charter (§req:constraints) — the
  one bounded exception is opt-in gap-filling of basic tags
  (§spec:tag-filling).
- **Every field is made path-safe, whether or not the template says
  `|sanitize`.** Field values come from source metadata that uploaders
  control, so a field must never add a path separator or a `..`
  segment: hostile characters become `_`, and a value made only of dots
  becomes `_`. Nothing else changes, so two distinct identifiers never
  render to the same folder. `|sanitize` additionally collapses repeated
  underscores and trims trailing dots and spaces. A list value (a
  repeated metadata field) renders as one comma-joined segment.
- **Sanitization is conservative:** filesystem-hostile characters
  (slashes, colons, and similar) are replaced; non-ASCII is preserved.
  Archival metadata is multilingual and lossy transliteration would
  destroy information the library tool may want.
- **Library paths stay inside the collection's library folder.** A
  rendered folder or recorded file name that would land outside it is
  refused and that item fails visibly (sync, restage), so source data
  can never place, replace, or remove a file elsewhere — the library-side
  twin of the archive's identifier containment check.
- **Every library path has one owner; clashes are separated, never
  overwritten.** Two items rendering the same folder are each given
  their own folder, distinguished by identifier; a library file
  shakedown did not place is never replaced or removed
  (§spec:library-ownership). A layout that structurally cannot
  distinguish items is still flagged at config time
  (§spec:layout-collision-safety).
- **The library holds each show only at its current path.** Old
  folders and files from an earlier layout, earlier metadata, or a
  replaced or pruned copy are removed (§spec:library-cleanup).
- When a collection sets `preserve_source_metadata`, the item's
  `metadata.json` sidecar is hardlinked into the staging directory
  alongside the media, so the library folder is self-describing
  (§spec:metadata-preservation).
- **Missing template fields** render as a literal `unknown` segment
  rather than failing the item; collision detection catches any
  resulting conflicts. Archival metadata is patchy, and dropping an
  item from the library because its venue tag is absent serves no one.
- `library_layout: passthrough` (the default when unset) mirrors the
  archive structure 1:1 for users who want no opinion imposed.
- `shakedown restage` rebuilds the staging tree from the archive
  without any network traffic — used after changing a template or
  wiping the library tree — and completes in under five minutes for a
  ~14k-item collection (§req:success-criteria #3). It links every item
  whose archive copy is on disk, whatever its status (a `disappeared`
  show keeps its files, so it keeps its library folder). It renders every
  item's library folder before removing anything, so a layout that
  cannot render leaves the existing tree in place, and it removes only
  files shakedown placed (§spec:library-cleanup). Staging is also
  self-healing: links deleted by a library tool are restored on the
  next sync or restage.

## Library path ownership §spec:library-ownership

*Status: complete*

Every library folder and file has exactly one owning show, and two
distinct recordings never share, replace, or merge into one another's
library folder (§req:success-criteria #19, §req:quality-attributes: the
library is a faithful, deterministic view of the archive; no silent
library data loss).

Observable behavior:

- **Shakedown records what it places.** For every show it stages, the
  system records the show's library folder and each file it placed
  there (media, and the `metadata.json` sidecar when preserved). This
  placement record is the sole authority for "who owns this library
  path" and for "did shakedown put this file here"
  (§spec:library-cleanup, §spec:state).
- **Clashing folders are separated by identifier, for every clasher.**
  Within a collection, when two or more shows that have an archive copy
  render the same library folder from `library_layout`, *each* of them
  is staged into that folder name with a space and `[<identifier>]` appended to its
  last segment (`1977-05-08 - Barton Hall` →
  `1977-05-08 - Barton Hall [gd77-05-08.sbd.hicks.4982.sbeok.shnf]`). No
  show keeps the bare name while another is labelled. A show whose
  folder clashes with no other keeps the bare rendered name. Folders
  are compared by their full rendered path; files of different shows
  never share a folder, whatever their names.
- **The library is a function of archive state and config.** Which
  folder each show gets depends only on the set of shows with archive
  copies, their recorded metadata, and the layout — never on run order,
  fetch timing, which show arrived first, or whether the tree was
  reached by incremental syncs, a full `restage`, or after `reconcile`
  rebuilt the database. Consequently, when a second recording of a show
  arrives, the first recording's folder moves once to its labelled
  name; when the clash ends (a recording pruned), the
  survivor moves back to the bare name. The old folder is removed by
  cleanup (§spec:library-cleanup).
- **A file shakedown did not place is never replaced.** If a target
  path already holds a file that is neither shakedown's recorded
  placement nor the same file as the archive copy, the system leaves it
  in place, skips that one link, and reports the conflict for the show
  (a per-item issue in the run summary and `status`); the rest of the
  show still stages. The record names the exact file placed, so a file
  swapped in later at a placed path counts as one shakedown did not
  place: it is neither replaced nor removed by cleanup.
- **Separations are reported, not failures.** The run summary lists the
  separations made in that run; `shakedown status` (text and JSON) lists
  every separation currently in effect, naming each clashing show and
  the folder it received. A separation alone does not make the run exit
  non-zero: no recording was lost, and a collection holding two tapes of
  one show would otherwise fail every scheduled run forever.
- `passthrough` layouts never clash (archive paths are unique by
  identifier), and shows in different collections never clash (each
  collection has its own library folder).

**Decision and constraint.** Separation labels *every* clashing show
because the requirement that the same archive and config always yield
the same library (§req:quality-attributes) rules out any history-based
winner, and labelling all clashers is the only rule under which a show's
folder name says plainly that other recordings of that show exist. The
placement record exists because the filesystem alone cannot answer
"whose file is this?" once an archive copy is replaced — and both
ownership and cleanup (§spec:library-cleanup) need that answer.
(Operator decision, 2026-11-10.)

**Alternatives rejected.** *First arrival keeps the bare name* — keeps
existing folders still, but the result depends on history, so a
`reconcile` or a fresh restage could produce a different library,
breaking determinism. *Lowest identifier keeps the bare name* —
deterministic, but a newly arrived recording can evict an existing one
from the bare name, which is the same folder move with less clarity.
*Skip the loser and report* (the shipped behavior) — loses a recording
from the library, contradicting criterion 19. *Merge into one folder
when filenames differ* (the shipped accidental behavior) — two tapes'
tracks interleave in one album.

**Tradeoffs.** A show's folder moves when its clash state changes; a
library tool that keys on paths (Navidrome play counts, stars) sees a
remove plus an add for that show. Accepted: it happens once per clash
change and only to shows with multiple recordings.

**User-level test path.** Configure a layout of `{date} - {venue}` on a
collection with two recordings of one show; sync; see two folders, each
labelled with its identifier, and the separation in the run summary and
`status`; sync again and `restage`; the folders are identical each
time. Prune one recording; sync; the survivor's folder returns to the
bare name and the labelled folder is gone. Drop a file of your own into
a show's folder under a name the show also has; sync; your file is
untouched and the conflict is reported.

## Stale library copy cleanup §spec:library-cleanup

*Status: complete*

The library holds each show only at its current path. Whenever a show's
library folder or file set changes, the system removes what it placed
before and no longer belongs there, and a pruned show's library links
are removed so its bytes are actually freed (§req:success-criteria #20,
§req:user-stories: change the layout without cleanup).

Observable behavior:

- **Triggers.** Any operation that stages a show reconciles its
  library placement against its current one: sync (layout changed
  without a restage, a re-fetch that brings new metadata or a changed
  file list, a lossy copy replaced by lossless files), the explicit
  metadata refresh, single-show operations (§spec:single-show-runs),
  `restage`, and prune. A previously placed file that is not part of
  the show's current placement is removed; a folder left empty by
  that removal is removed, up to the collection's library folder. A
  pruned show's placement is removed entirely.
- **Only what shakedown placed.** Removal consults the placement record
  only. A file found in a folder shakedown is cleaning that it did not
  place is left alone, its folder stays, and the file is reported:
  `status` (text) shows a count of such files per collection, and
  `status --json` lists their paths. `restage` follows the same rule:
  it no longer deletes the collection's whole library folder.
- **The first run after upgrading adopts existing links.** Before this
  increment, nothing recorded what shakedown placed. On the first
  staging operation per collection after the upgrade, the system walks
  that collection's library folder once and records as a show's
  placement every file that is the same file (hardlink) as a file in
  that show's archive copy. Normal cleanup then removes adopted files
  that are not at the show's current path — this clears duplicates like
  the Aquarium Rescue Unit shows linked into two folders, without any
  download. Files that cannot be identified this way (typically old
  copies whose archive file was since replaced, now with a single link)
  are reported as not placed by shakedown, for the operator to delete.
  (Operator decision, 2026-11-10.)
- **An upstream rename is a metadata change.** When a show's metadata
  changes so that its folder renders differently, the show moves and
  the old folder is removed. An identifier that vanishes upstream and
  reappears under a new identifier is two shows: the old one follows
  the retention rules (`disappeared`, keeps its folder unless pruned —
  §spec:item-lifecycle) and the new one is staged as new.
- **Failure behavior.** A removal that fails (permission, I/O) leaves
  that file recorded as placed, marks the show's staging as a per-item
  issue in the run, and is retried on the next staging operation; it
  never aborts the run.

**Decision and constraint.** Cleanup is driven by the placement record
(§spec:library-ownership), because §req:quality-attributes forbids
deleting a library file shakedown did not place, and only a record can
tell a stale shakedown link (its archive file replaced, link count 1)
from a user's file. The upgrade adoption uses the hardlink relation —
the one ownership signal the filesystem itself can decide — rather than
guessing from names.

**Alternatives rejected.** *Re-render the current template to find
what to unstage* (shipped) — misses every old path, which is the bug.
*Remove any library file whose link count is 1* — would delete files
users or other tools put there. *One-time full rebuild of each
collection's folder on upgrade* — clears every leftover but also any
foreign file in those folders; the operator chose adoption. *Keep
`restage` as a full wipe* — contradicts "never deletes a file it did
not place."

**Tradeoffs.** Each staging operation compares a show's previous
placement with its new one, and the first post-upgrade run walks each
library folder once (directory entries and inode numbers only; no
hashing). Old copies whose archive file was already replaced before the
upgrade need one manual delete, guided by `status`.

**User-level test path.** Sync a collection; change `library_layout`;
sync without restaging; every show is present only under the new layout
and `find <library> -type f -links 1` lists nothing shakedown placed.
Enable `prune_disappeared`, remove a show upstream, sync; its archive
and library copies are gone and its disk space is freed. On a library
upgraded from a version that linked a show into two folders, run one
sync; the old folder is gone without any download.

## Tag filling §spec:tag-filling

*Status: complete*

Includes choosing the writer by the file's real format
(§spec:real-format) and M4A files (§spec:m4a-tags).

A collection may opt in (`fill_tags`, default off, §spec:configuration)
to having shakedown fill in missing basic tags from the source metadata
it already holds, so shows group and display correctly in a tag-driven
player such as Navidrome (§req:success-criteria #25, §req:constraints:
the bounded gap-filling exception; tagged bytes are recorded state).

Observable behavior:

- **What is filled.** For each audio file in a format the system can
  tag (FLAC, MP3, Ogg Vorbis, and M4A per §spec:m4a-tags — judged by the
  file's real format, §spec:real-format; other files, including SHN,
  are left untouched), the system fills only tags that are absent — a tag whose
  values are all empty or whitespace counts as absent (operator
  decision, 2026-11-10):
  - artist and album artist ← the show's creator;
  - album ← `<date> – <venue>` (the show's title when venue or date
    is missing; the identifier when the title is missing too — never
    `unknown`). When two or more
    shows that shakedown tags would end up with the same album artist,
    album, and date — which a tag-driven player merges into one album —
    each of them gets a space and `[<identifier>]` appended, by the same
    every-clasher rule as library folders (§spec:library-ownership).
    (Operator decision, 2026-11-10.)
  - date ← the show's date; venue ← the show's venue, in the format's
    custom venue field;
  - track title and track number ← the per-file values the source lists
    for that file, when it lists them; otherwise left absent.

  Archive.org MP3s carry a malformed link frame (a LINK frame written
  like a text frame, holding the item's URL) that the tagging library
  cannot save as ID3v2.3. When the system fills tags in such a file, it
  repairs that frame into the ID3 source-webpage frame (WOAS) holding
  the same URL, or drops it when it holds no URL; the tag stays ID3v2.3
  and every other frame is kept (operator decision, 2026-11-16: leave
  the file better than it was found). This is the one frame shakedown
  changes that it did not write.
  A file whose existing tags the tagging library still cannot write back
  as they are is left exactly as it is, with a warning
  in the run log; the show still completes and is not retried for that
  file (§spec:per-item-isolation; operator decision, 2026-11-16: the
  one-time catch-up after the v11 upgrade, §spec:state, replaces
  retrying on every sync). A disk fault
  while writing still fails the show for a retry.
- **Never overwrite someone else's tag.** A tag already present —
  written by the uploader, the source, or the user's own tool — is never
  changed. The one case where shakedown rewrites a tag is a value it
  wrote itself whose computed value has changed (an album gaining or
  losing its identifier label as clashes come and go, or a metadata
  refresh changing venue or date); it does so only when the file still
  holds exactly the value shakedown recorded writing. If anything else
  changed it since, the tag is left alone.
- **When it happens.** A newly fetched show is tagged before it enters
  the archive, so its tagged bytes arrive atomically with it. Shows
  already mirrored are tagged once, on the first sync after the
  collection opts in. After that, a show is revisited only when its
  computed tag values change (clash state, metadata refresh) or after a
  reconform. A crash mid-tagging never leaves a half-written file in the
  archive or library: before writing into a mirrored file, the system
  keeps a complete backup of it beside the archive (same filesystem,
  outside the library) and deletes the backup once the write is done;
  every writing run (sync, refresh, single-show runs, `restage`) first
  restores any leftover backup over its file. Plain `verify` holds no
  writer lock and does not restore, so an interrupted file shows as
  drift until the next writing run. (Operator constraints, 2026-11-10:
  no corrupt file survives a crash, and no link anywhere keeps stale
  bytes or a leftover copy — which rules out writing a tagged copy and
  swapping it in, since hardlinks shakedown did not place would keep
  the old bytes.)
- **Recorded state, no false drift.** The source's manifest (what the
  source reported) stays the basis of change detection, unchanged
  (§spec:sync-identity): tagging never causes a re-download, and a sync
  never re-reads or re-hashes files of shows already tagged. For each
  file it changed, the system records the post-tag checksum and the tag
  values it wrote. `verify --deep` compares each file against its
  post-tag checksum when one is recorded (the source checksum
  otherwise), so shakedown's own tagging is not drift while any later
  change by another tool is. `verify --deep --reconform` restores the
  source's exact bytes and then, on a `fill_tags` collection, fills the
  gaps again (§spec:verify-drift).
- **Turning it off** stops further filling; files already tagged keep
  their tags and their recorded post-tag checksums, so `verify --deep`
  still reports no drift for them. A reconform on a collection with
  `fill_tags` off restores source bytes without refilling.
- Tag filling changes files in place through the shared inode, so the
  archive copy carries the filled tags too; it adds no copies
  (§req:quality-attributes: storage efficiency).

**Decision and constraint.** Fill-only-absent, from metadata already
held, with the post-tag checksum recorded beside — never inside — the
source manifest. The constraints: Navidrome is the primary listening
path and only ~33–50% of sampled Grateful Dead / Aquarium Rescue Unit
files carry tags (§req:priorities #26); the manifest is the
change-detection key, so anything shakedown writes must stay out of it
or every tagged show would look changed upstream; and REQUIREMENTS
bounds the exception to gap-filling. The album label follows the folder
rule because Navidrome groups by album artist + album + date, so two
tapes of one show with identical tags would merge into one album. Tag
reading and writing go through an established, maintained audio tagging
library; shakedown never implements a tag format itself
(principles: boring edges, don't rebuild).

**Alternatives rejected.** *Copy-on-tag* (library gets tagged copies,
archive stays pristine) — doubles storage, contradicting
§req:quality-attributes. *Leave tagging to Beets* — Beets is no longer
assumed in the stack (§req:constraints). *Overwrite existing tags with
source values* — destroys the user's and uploader's tag work. *Album =
archive.org title* — long, and tapes of one show still merge. *Always
append the identifier to the album* — rejected by the operator in favor
of labelling only on a clash.

**Tradeoffs.** The first sync after opting in reads and rewrites every
untagged file of the collection once (hours on a large collection), and
a clash change rewrites the album tag of the affected shows. Bytes in
the archive are no longer the source's bytes for tagged files; operators
who need byte fidelity leave `fill_tags` off or reconform.

**User-level test path.** Enable `fill_tags` on a collection with
untagged files and two recordings of one show; sync; Navidrome lists
the shows by artist and date, the two recordings as two albums labelled
by identifier, and files that had tags keep them unchanged. Run sync and
`verify --deep` again: no download, no drift. Retag one file with
another tool; `verify --deep` reports exactly that file.

## Audio known by its content §spec:real-format

*Status: complete*

Compatibility: from "deployed — single operator install, no external
consumers" (§req:constraints): every surface this feature touches is
free to break — library file names, state database (moves forward by
one additive migration), `status` text and JSON, `GET /api/attention`,
and the dashboard. The archive is frozen in one respect: no file in it
is renamed, and the stored shows are corrected without downloading
their audio (§req:constraints: never force re-downloading the bulk of
the archive). (Operator decisions, 2026-11-23.)

The system decides what format an audio file is from its content, not
its name, and acts on the real format everywhere a format matters: the
library file name, the tag writer, and the best-copy choice
(§req:success-criteria #34; §req:user-stories: a mislabelled show still
plays right; §req:constraints: the bounded exception for a library copy's
real extension).

Observable behavior:

- **Which files are checked.** Every audio file whose name ends in
  `.flac`, `.mp3`, `.ogg`, or `.m4a` (any letter case), from any
  source, whether it is staged or kept archive-only. Other audio (SHN,
  WAV, APE, …) is taken by its name as today; mislabels among those are
  out of scope (§req:constraints: mislabelled audio scope).
- **What a check finds.** One of four recognised formats, each with its
  audio class: FLAC (lossless), MP3 (lossy), Ogg Vorbis (lossy), or M4A
  — an MP4 audio container — holding AAC (lossy) or Apple Lossless
  (lossless). Anything else is *unidentified*: a WAV named `.flac`, a
  truncated upload, an Ogg or MP4 file holding another codec.
- **Checked once, when the file arrives.** A downloaded file is checked
  in the fetch's temp folder, after the source's checksum has passed
  and before tag filling, so the show enters the archive with its real
  formats known. The check reads only the parts of the file that
  identify it (the start, and for M4A the track description, which may
  sit at the end); it never reads a whole file. Shows already in the
  archive are checked once by the catch-up in §spec:real-format-catch-up.
- **The result is recorded state.** Per checked file, the system records
  the real format and class it found. The record belongs to the file as
  the manifest records it (name, size, source checksum): a file the
  source changes is downloaded and checked again. The record stays out
  of the change-detection identity (§spec:sync-identity), so a check
  never makes a show look changed upstream and never causes a download.
  Restage, sync's staging, and reconform read the record and never
  re-open a file to decide its format.
- **The archive keeps the source's name.** `1-01 St. Stephen.flac`
  stays `1-01 St. Stephen.flac` in the archive. Manifests, drift,
  reconform, and tag records keep using the source's name.
- **The library carries the real extension.** When a file's real format
  differs from what its name says, its library hardlink is named with
  the file's stem plus the real format's extension, lowercase:
  `.flac`, `.mp3`, `.ogg`, `.m4a` (`1-01 St. Stephen.flac` → `1-01 St.
  Stephen.m4a`). A file whose name already fits its content keeps its
  exact name, letter case included. If the new name would equal
  another library file of the same show, the misnamed file keeps its
  source name instead, so no show file replaces another; the same
  archive state and config always produce the same names
  (§spec:library-ownership). The placement record holds the name
  actually placed, so cleanup removes an old `.flac` link when the
  `.m4a` link replaces it (§spec:library-cleanup).
- **Tags go through the real format's writer.** Tag filling opens each
  file with the writer for its recorded real format, so an M4A named
  `.flac` is tagged as M4A (§spec:m4a-tags). A recognised file never
  produces the "cannot be read as FLAC" warning.
- **Unidentified files stay as they are, visibly.** An unidentified file
  is archived and staged under its source name, ranked by its listing as
  today, and never tagged, so it causes no tag warning. It is listed in
  `status` and the dashboard (§spec:unidentified-audio).
- **Log, not screen, for recognised mislabels.** The run log records one
  line per file whose real format differs from its name. Nothing on the
  dashboard or in `status` mentions a mislabel shakedown has handled: the
  library name is the visible result (principle: UI copy is product).

**Decision and constraint.** Check content once, at fetch, record the
result beside the manifest, and rename only the library link. The
constraints: the archive mirrors the source and its names are the
change-detection key (§spec:sync-identity), so it cannot rename;
Navidrome and other players read the extension, so the library must
carry the real one; restage must stay under five minutes for ~14k items
and never read every file (§req:quality-attributes: restage speed), so
the format must be known from recorded state; and the fetch temp folder
is the one point where every new file passes, already readable, before
tagging. The format is identified through the established audio tagging
library's own format readers, never from the file name and never by a
hand-written parser (principles: boring edges, don't rebuild). This is a
bounded exception to §spec:library-staging's "filenames always match the
archive": only the extension changes, only when the content proves it
wrong, and the stem still correlates the two trees.

**Alternatives rejected.** *Rename in the archive too* — breaks the
archive as a mirror of the source and every name-keyed record
(manifests, drift, reconform, tag records); the operator ruled it out.
*Keep the `.flac` name and only tag correctly* — players still treat the
file as FLAC. *Check during restage* — reads every audio file on every
restage, breaking the five-minute bar. *Check only files whose tagging
failed* — misses mislabels on collections without `fill_tags` and MP3s
named `.ogg` that happen to tag. *Report the mismatch instead of fixing
it* — the operator wants a fix (§req:priorities #37). *Trust the
source's format field* — it repeated the uploader's mistake in all
three observed shows. *Identify by a file-type library outside the
tagging library* — adds a dependency for an answer the tagging library
already gives, and the tag writer must agree with the check anyway.

**Tradeoffs.** A library tool that keys on paths (Navidrome play counts,
stars) sees a remove plus an add for each renamed file, once. Checksum
and fingerprint files in the library (`.md5`, `.ffp`) still name the
source's file names: they describe the archive copy, not the library
link. An Ogg Opus file named `.ogg` is listed as unidentified even
though players may read it — accepted as rare and harmless, since it is
still staged. Navidrome already reads these files by their content: on
the operator's install (scan of 2026-11-21, read 2026-11-23) it lists
all 59 tracks of the three shows with the uploader's tags and the right
bitrates, but records their suffix as `flac`, so clients may be served
the FLAC media type. The visible gains are the right suffix and media
type, and the filled album artist and venue. The operator confirms
playback after the change (§req:success-criteria #34).

**User-level test path.** Sync a fixture show whose three tracks are
small M4A (AAC) files named `.flac`: the archive holds the three `.flac`
names, the library holds `.m4a` files with the same stems, the run log
has no "cannot be read as FLAC" warning, and `restage` rebuilds the same
`.m4a` names without opening the files. Add a show with an MP3 named
`.ogg` and a FLAC named `.mp3`: the library holds `.mp3` and `.flac`
links. Sync again: no download, no change.

## Best copy by real format §spec:real-format-ranking

*Status: complete*

The best-copy choice (§spec:quality-selection) uses a file's real
format and class once it is known, so a copy listed as lossless that
turns out lossy is ranked as lossy, and a better copy is fetched when
the source offers one (§req:success-criteria #26, #34; §req:user-stories:
the best copy, judged by what it really is).

Observable behavior:

- **Known formats replace listed ones.** Ranking works from the
  listing as before, except that a file with a recorded check (and an
  unchanged name, size, and checksum) counts by its recorded real
  format and class. A copy whose checked files are not all lossless
  ranks as lossy; among lossless copies, the real format sets the order
  (FLAC, Apple Lossless, WAV, AIFF, then SHN), as §spec:quality-selection
  states. A lossy copy still ranks by the source's listed bitrate when
  every candidate lists one, and by size per track otherwise.
- **Unchecked files count as listed.** Only downloaded files have been
  checked. A copy the system never downloaded ranks by its listing, so a
  lossless file named `.mp3` that was never selected stays unknown.
- **A check that changes the ranking re-runs the choice in the same
  run.** When a fetch's checks change a show's ranking, the show's
  selection is made again with the real formats before the show enters
  the archive, and any newly chosen files are downloaded and checked in
  that same fetch. Each file is downloaded at most once per run, so the
  re-ranking ends after at most as many rounds as the show has copies.
  The show enters the archive once, with one handoff.
- **Demoted copies stay.** A copy that loses its place by its real format
  stays in the archive and leaves the library, as any file the rule no
  longer selects does (§spec:quality-selection: files the rule no longer
  selects stay).
- **Stored shows re-rank on the same rule.** When the catch-up
  (§spec:real-format-catch-up) changes a stored show's ranking, that show
  is looked up in the same sync, even on an incremental collection
  whose change signal says unchanged, and only files not already in the
  archive are downloaded.
- **Refused lossless keeps its meaning.** "Lossless refused by
  archive.org; best lossy copy mirrored instead" still means the source
  refused every lossless file. A served copy listed as lossless that
  proves lossy does not produce that reason.
- **The three Dead & Company shows keep their copy.** Their originals
  — AAC in M4A for `deadco2022-06-13` and `deadco2022-06-14`, 320 kbps
  MP3 for `deadco2022-06-18.deadco2022-06-18`, all named `.flac` — are
  re-ranked as lossy. archive.org lists no bitrate for them, so they rank
  by size per track and still beat archive.org's VBR MP3 derivatives
  (about 1.6 to 2.3 times their size per track). Nothing is downloaded.

**Decision and constraint.** Recorded checks override the listing, and a
changed ranking re-selects inside the same fetch. The constraints: the
product's goal is the best version that truly exists
(§req:problem-statement), so the listing's word cannot win over the bytes; the
recorded check must feed every later lookup, or the next sync would rank
the listing again and stage the demoted copy back; and a show entering
the archive once keeps one handoff and one library update per show.

**Alternatives rejected.** *Record the real class without re-ranking* —
keeps a lossy copy when the source has a lossless one, against criterion
26. *Re-rank on the next sync* — the library holds the wrong copy for a
schedule interval and the show sends two handoffs. *Download every
candidate copy to check it before choosing* — multiplies downloads for a
rare case, against the no-bulk-download constraint.

**Tradeoffs.** A mislabelled lossless-listed copy costs one extra
download round when a better copy exists, and the demoted copy keeps its
archive space. Files never downloaded keep their listed class.

**User-level test path.** With a fixture source listing a show's "Flac"
set (really AAC) and a real FLAC set under another format name, plus an
MP3 derivative: sync; the run downloads the AAC set, finds it lossy,
downloads the FLAC set in the same run, and the library holds the FLAC
files while the archive also keeps the AAC set. Sync again: no download.
With only the AAC set and the MP3 derivative: the AAC set is kept and
staged as `.m4a`; the MP3 is not downloaded.

## M4A tag filling §spec:m4a-tags

*Status: complete*

On a `fill_tags` collection, M4A files — honestly named `.m4a` files and
files the check found to be M4A — have their missing tags filled like
FLAC, MP3, and Ogg Vorbis files (§req:success-criteria #25, #34;
§req:user-stories: my real .m4a shows browse too; §req:constraints:
tag filling covers M4A).

Observable behavior:

- **Same rules, one more format.** Everything §spec:tag-filling states
  holds for M4A: only absent tags are filled; nothing another tool or
  the uploader wrote is changed; shakedown rewrites only a value it
  recorded writing; the post-tag checksum and written values are
  recorded; crash backups and `verify --deep` work the same.
- **Where the values go.** Artist, album artist, album, date, track
  title, and track number go in the iTunes-style atoms players read
  (`©ART`, `aART`, `©alb`, `©day`, `©nam`, `trkn`); venue goes in the
  iTunes freeform field named `VENUE`, the same name the Vorbis comment
  and the ID3 user text field use. `trkn` holds only numbers, so the
  source's `01` is written as track 1 (`3/12` as 3 of 12), and a listed
  value with no number leaves the track number absent.
- **The writer follows the recorded format.** A file the check found to
  be M4A is tagged as M4A whatever its name; a file named `.m4a` whose
  content is FLAC is tagged as FLAC.
- **Already-mirrored M4A files are tagged once.** On the first sync
  after the upgrade, a `fill_tags` collection's shows that hold M4A
  files are revisited once, as when a collection first opts in; files
  already carrying every tag are only read.
- **Uploader tags stay.** The two Shoreline Amphitheatre shows
  (`deadco2022-06-13`, `deadco2022-06-14`) already carry the uploader's
  iTunes tags (artist "Dead and Company", album, date, title, track,
  disc); filling adds only album artist and venue. Their artist keeps
  the uploader's spelling, so a tag-driven player lists their tracks
  under "Dead and Company" while the filled album artist, the show's
  creator "Dead & Company", groups the albums with the collection's
  other shows.

**Decision and constraint.** M4A joins the tagged formats through the
same maintained tagging library, with venue in a freeform field of the
same name as in the other formats. The constraints: Navidrome groups
by album artist, album, and date, so untagged `.m4a` shows browse badly
(§req:user-stories); §req:constraints bounds tagging to filling absent
basic tags; and one venue field name across formats keeps the tag a
library tool reads predictable.

**Alternatives rejected.** *Tag only mislabelled M4A files* — leaves
every honest `.m4a` upload untagged, the wider form of the same gap; the
operator chose both. *Leave M4A untagged and only rename* — the three
shows would lack album artist and venue. *Put venue in the comment
atom* — that field belongs to the uploader and would be overwritten
or skipped.

**Tradeoffs.** The first sync after the upgrade reads every M4A file of
each `fill_tags` collection once and rewrites those with gaps. Writing
tags may reorder an M4A file's boxes; the bytes then differ from the
source's, as for any tagged file, and reconform restores them.

**User-level test path.** On a `fill_tags` collection, sync a fixture
show of genuine `.m4a` files without tags and one whose `.m4a` files
carry an artist: the first gets artist, album artist, album, date,
venue, title, and track number; the second keeps its artist and gains
the rest. `verify --deep` reports no drift. Sync the M4A-named-`.flac`
fixture: its `.m4a` library files carry the filled tags.

## Stored shows checked once §spec:real-format-catch-up

*Status: complete*

Shows already in the archive get their audio checked once, without any
download, so mislabelled files already stored are renamed in the
library, tagged, and ranked by their real format
(§req:success-criteria #34; §req:user-stories: the shows already stored
fixed without downloading them again; §req:quality-attributes: restage
speed).

Observable behavior:

- **Automatic, on the first locked run.** The first sync after the
  upgrade checks every audio file in the checked set
  (§spec:real-format) that has no recorded check, reading only the
  identifying parts of each file. It checks every stored show once the
  listing is done, before any show is looked up or its copy chosen, so
  the run shows the work in the Discovering phase; a stored show the
  source no longer lists is checked too. The
  operator runs nothing. The same catch-up checks a database rebuilt by
  `reconcile`, which starts with no checks: unlike tag records, a check
  is recoverable from the files themselves (§spec:state).
- **Then the show is acted on as if just fetched.** A show whose check
  finds a file whose format differs from its name gets its library link
  renamed at the end of that sync's staging, has its tag pass run again
  on a `fill_tags` collection, and is re-ranked
  (§spec:real-format-ranking). No audio is downloaded for a show whose
  ranking stays the same.
- **Interruptible.** Each show's checks are recorded as they finish, so a
  stopped or killed run resumes with the shows not yet checked. A file
  that cannot be read (I/O error, missing) is left unchecked and tried
  again on the next locked run; the show is not failed for it.
- **Bounded cost.** About 14k shows of about 15 audio files each is
  about 200k files, each opened once and read for a few kilobytes: on
  the operator's NAS roughly 15–40 minutes, once. Later syncs and every
  restage read no file to know its format.
- **The three Dead & Company shows.** After the first sync, the 38
  library files of `deadco2022-06-13` and `deadco2022-06-14` are `.m4a`
  and the 21 of `deadco2022-06-18.deadco2022-06-18` are `.mp3`; album
  artist and venue are filled where absent (their collection fills
  tags); each keeps its copy; and nothing was downloaded. A later sync
  and `verify --deep` report no download and no drift;
  `verify --deep --reconform` restores the source bytes, leaves the
  library names `.m4a` and `.mp3`, and fills the tags again.

**Decision and constraint.** One automatic catch-up over every stored
file in the checked set, rather than a command or a narrower guess. The
constraints: the operator must not have to notice the uploader's mistake
(§req:user-stories); a mislabel is decidable only by reading the file,
and no recorded state narrows the search (untaggable results were never
recorded, and collections without `fill_tags` never tried); restage
must stay under five minutes, so the reading happens once; and the same
mechanism must restore checks after a `reconcile`. Precedents: the
one-time library adoption walk (§spec:library-cleanup), the v11 tag
catch-up, and the v12 figure backfill (§spec:state).

**Alternatives rejected.** *An operator-run command* — the operator must
know to run it, and must run it again after every `reconcile`. *Check
only `fill_tags` collections, or only `.flac` files* — misses mislabels
the check exists to find. *Check inside `verify --deep`*, which reads
every byte anyway — `verify` is never scheduled and holds no writer
lock, so the fix would wait on the operator and could not record its
result. *Check on every restage* — breaks the restage bar.

**Tradeoffs.** The first sync after the upgrade runs longer, once.
Shows whose re-ranking finds a better copy download it in that sync —
expected to be a small share, and the point of criterion 26.

**User-level test path.** Build an archive and database at the previous
schema holding the M4A-named-`.flac` fixture show, tagged-collection
config, and an honest FLAC show. Upgrade and sync with the network
blocked for audio: the fixture's library files become `.m4a` with album
artist and venue filled, the FLAC show is untouched, and no audio
request is made. Kill the sync midway on a larger fixture and sync
again: it resumes. Run `reconcile`, then sync: the names stay `.m4a`.

## Unidentified audio in status and the dashboard §spec:unidentified-audio

*Status: complete*

A file whose content matches none of the recognised formats
(§spec:real-format) is listed in `status` and on the dashboard's Needs
attention list, never only in a log (§req:success-criteria #31, #34;
§req:user-stories: tell me what you cannot identify).

Observable behavior:

- **`status`.** Per collection, `status --json` carries
  `unidentified_files`: the complete list of `{identifier, file}`, by
  show, `file` being the source's name. It covers the files staged
  under their name and never tagged: shows with an archive copy, and a
  record that still describes the manifest's file (a record left from
  other bytes counts for nothing). `status` text prints a count and the
  first 10. A database from before format records lists none; `status`
  never migrates it.
- **`GET /api/attention`.** The entry kind `unidentified_files`, one per
  collection with any, shaped like `unplaced_files`
  (§spec:needs-attention): the affected shows, the number of files as
  its count, the first 10 `{identifier, file}` pairs, and no `since`.
- **The page words the kind.** The Needs attention row reuses the
  existing entry row; its title and next step come from the kind's word
  entry (the files are kept as they are and shakedown does nothing more
  with them), and it shows the files as `<show ID>/<file>`, as unplaced
  files show their paths.
- **When it clears.** The entry for a file goes when the file is no
  longer unidentified: the source changes it and the new download is
  recognised, or the show is pruned. Like `separations`, it can stay
  until the source changes.

**UI readiness.** One more row of an existing list, from the existing
row component and kit tokens; no new screen and no glossary term. The
operator approved the row in the `long_content` render (2026-11-23).

**Decision and constraint.** An attention entry per collection, shaped
like `unplaced_files`. The constraints: the operator chose "needing
attention, never only a log" (§req:success-criteria #34); the attention
list groups by collection and kind (§spec:needs-attention); and the
closest existing kind — files shakedown keeps but cannot act on — is
`unplaced_files`, whose shape the page already renders.

**Alternatives rejected.** *A library health fact* — health holds facts
that need no look from the operator; an unidentified file may be a
corrupt upload worth a look, and the operator chose attention. *One
entry per file* — buries other entries (§spec:needs-attention: one entry
per collection per kind). *Leave it to the log* — the observed failure.

**Tradeoffs.** An exotic but playable file (an Ogg Opus named `.ogg`)
keeps an entry until its source changes, as a clash keeps `separations`;
the dashboard cannot dismiss it.

**User-level test path.** Sync a fixture show holding a WAV file named
`.flac`: the show is mirrored, the file is staged under its name and not
tagged, `status` lists it under the collection, and the dashboard's
Needs attention shows one entry naming the show and file. Replace the
file upstream with a real FLAC and sync: the entry is gone.

## Verification and drift §spec:verify-drift

*Status: complete*

Including reconform as single-show runs (§spec:single-show-runs),
comparison against post-tag checksums, refilling tags after a reconform
with the writer for the recorded real format (§spec:tag-filling), and
keeping a reconformed file's real-extension library name
(§spec:real-format).

`shakedown verify` is the explicit, operator-invoked integrity tool.
It is never scheduled by default (§req:constraints).

- Without `--deep`: existence-only — expected archive paths are
  checked against recorded manifests; missing files are reported.
- With `--deep`: on-disk files are hashed and compared against
  recorded manifests — against the recorded post-tag checksum for a
  file shakedown tagged (§spec:tag-filling) — reporting **drift**. Drift is informational, not
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
  (§req:success-criteria #5). Each reconformed show is fetched as a
  single-show run (§spec:single-show-runs) and, on a `fill_tags`
  collection, has its missing tags filled again afterwards with the
  writer for the file's recorded real format; its library link keeps
  the real extension (§spec:real-format). Only the
  drifted files are downloaded: any the source still serves, a picked
  copy or an archive-only one alike. A drifted file the source no
  longer serves (dropped or refused) is reported in the run as not
  repaired and causes no other download.

## CLI surface §spec:cli

*Status: complete*

Shakedown is a one-shot CLI; every command runs to completion and
exits (§req:constraints). The command surface:

| Command | Behavior |
| --- | --- |
| `sync [--source S] [--collection C] [--dry-run] [--refresh-metadata]` | Run a sync now; defaults to all configured collections. What the scheduler invokes. `--refresh-metadata` re-resolves and rewrites `metadata.json` sidecars for preserve-opted collections without re-downloading media (§spec:metadata-preservation). |
| `status [--json]` | Last run per collection with its kind (sync, refetch, or reconform), item counts by status, archive size (from the recorded manifests — never a walk of the archive tree), restricted items with reasons, shows removed upstream and kept (each with the date it was first found missing, §spec:item-lifecycle), known drift, library separations in effect (§spec:library-ownership), library files shakedown did not place (§spec:library-cleanup), undelivered handoff notices (§spec:durable-handoff), shows not playable (§spec:quality-selection), and collections whose sync was rescheduled, with the time of the oldest skip (§spec:rescheduled-syncs). |
| `verify [--source S] [--collection C] [--deep] [--list] [--reconform] [--yes]` | Integrity check (§spec:verify-drift). |
| `restage [--source S] [--collection C]` | Rebuild the staging tree from the archive without downloading. |
| `reconcile` | Rebuild the state database from the archive tree plus current source manifests (§spec:state). |
| `trim-history` | Drop run error lines older than `keep_run_errors_days` now and compact the state database (§spec:run-error-retention). |
| `purge --source S --collection C [--archive]` | Delete the library files of a collection the config file no longer names, and with `--archive` its archive folder and shows' records (§spec:collection-removal). |
| `validate [--source S] [--collection C] [--live-handoff] [--json]` | Preflight a configured deployment before a large mirror begins, optionally exercising handoff targets with an explicit test payload (§spec:setup-readiness-validation). |
| `item show \| refetch <identifier>` | Single-item inspection and refetch; `refetch` is a single-show run (§spec:single-show-runs). |
| `serve [--host] [--port]` | Optional HTTP control plane (§spec:serve). |
| `schedule [--check]` | Long-running scheduler that starts each collection's scheduled sync; `--check` exits non-zero when its heartbeat is missing or older than three minutes (§spec:schedules). |

- Commands shall exit non-zero on failure so failures surface at the
  scheduler/container layer (§req:quality-attributes: operability).
- A `--source` or `--collection` that names nothing configured is a
  usage error (exit 2) listing the configured names — never a run that
  does nothing and exits 0 (§req:quality-attributes: scheduled runs are
  observable).
- Writing commands — `sync` (except `--dry-run`), `restage`,
  `reconcile`, `item refetch`, `verify --reconform`, `trim-history`, and
  `purge`,
  whether run from the CLI, the scheduler, or `serve` — hold one exclusive writer lock
  (a kernel `flock` on `<state_db>.lock`, beside the database file
  itself, so every spelling of one database shares it). A second writer exits non-zero
  at once, naming the lock, instead of racing the first over shared temp
  folders. The kernel releases the lock when the holder exits, even on a
  crash, so a stale lock file never blocks the next run. A
  `sync --collection` refused this way is also recorded as rescheduled
  and caught up when the running sync finishes (§spec:rescheduled-syncs).
- A writing command stopped by SIGTERM or SIGINT exits 143 or 130
  after recording its runs `stopped`; a second signal exits at once
  (§spec:graceful-stop).
- `status --json` emits machine-readable output for dashboards
  (Homepage/Homarr) in lieu of a web UI (§req:constraints).

**Why one-shot rather than a daemon.** The container is stateless
between runs — state lives in the on-disk database — so failures are
visible as container exit codes and scheduler logs rather than buried
inside a long-running process; testing is `shakedown <verb>` with no
daemon to stand up; and it matches how the *arr/homelab cron ecosystem
already works. Scheduling is deliberately someone else's job
(§spec:deployment).

## Single-show operations as runs §spec:single-show-runs

*Status: complete*

Includes filling tags after a fetch on a `fill_tags`
collection (§spec:tag-filling).

Refetching one show, or reconforming drifted shows, behaves like a sync
of exactly those shows (§req:success-criteria #23).

Observable behavior:

- **Looks up only the shows it acts on.** `item refetch <identifier>`
  finds the show's collection from the recorded state and looks up that
  one identifier with the core's per-item lookup
  (§spec:per-item-isolation) — a handful of upstream requests, never a
  walk of the collection. `verify --deep --reconform` does the same for
  each drifted show.
- **Polite.** Lookups and downloads draw from the shared source budget
  (§spec:source-budget), like a sync.
- **Recorded as a run.** Each operation records a run per affected
  collection, marked as a refetch or reconform, with the same counts,
  errors, and outcome reporting as a sync
  (§spec:recoverable-operation-reporting); `status` shows it as that
  collection's latest run.
- **Same staging and handoff as a sync.** The fetched show is staged
  with the ownership and cleanup rules (§spec:library-ownership,
  §spec:library-cleanup) and filled with tags on a `fill_tags`
  collection (§spec:tag-filling); the collection's handoff notice is
  sent exactly as a sync would send it, including any pending
  undelivered shows (§spec:durable-handoff).
- **Failure.** If the lookup fails or the show is not in the source's
  answer, the run records that show as failed with the reason and the
  command exits non-zero; the archive copy and its library placement are
  untouched. A show the source refuses (restricted, or its download
  refused) is recorded `unavailable` as a sync records it, and also
  counts as failed in that run, so the exit code, the run record, and
  the failure alert agree (operator decision, 2026-11-10). An identifier
  that names no recorded show is a usage error (exit 2).
- Both hold the writer lock, as today (§spec:cli).

**Decision and constraint.** Single-show operations reuse the sync
path at single-show scope instead of keeping their own fetch path,
because the separate path is exactly what made them scan whole
collections, bypass the budget, skip run records, and never notify the
library tool. The per-item lookup (§spec:per-item-isolation) is what
makes an O(1) lookup possible for every source.

**Alternatives rejected.** *Keep a separate refetch path and patch in
a budget, run record, and notice* — three fixes to keep in step with
sync forever. *Run a full collection sync filtered to one identifier* —
still lists the whole collection.

**Tradeoffs.** A reconform of many drifted shows records one run per
collection rather than one per show, so per-show detail lives in that
run's item list.

**User-level test path.** In a large collection, `item refetch` one
identifier: the source sees a few requests, `status` shows a refetch
run for that collection, and the handoff target receives a notice
naming the show. Reconform two drifted shows: one run recorded, one
notice sent, both shows' bytes restored.

## Configuration §spec:configuration

*Status: complete*

All configuration lives in a single YAML file at
`/data/shakedown-config/shakedown.yaml`, inside the one data mount
(§spec:deployment), editable from the dashboard
(§spec:dashboard-settings), through File Station, or with any text
editor — adding or changing a collection is a settings change or a YAML
edit, never a code change (§req:success-criteria #2). Until the
config-management increment the file was mounted read-only at
`/config/shakedown.yaml`; `--config` still points a run anywhere. See
[`shakedown.example.yaml`](shakedown.example.yaml) for the canonical
annotated example.

Structure: top-level roots (`archive_root`, `library_root`,
`state_db`) and global concurrency caps (`max_concurrent_downloads`,
`max_concurrent_collections`); a list of `sources` (name, plugin
`type`, optional auth) each containing `collections` (name, `query`,
`library_layout`, `prune_disappeared`, `incremental_discovery`,
`preserve_source_metadata`, `fill_tags`, `on_complete`, `schedule`);
global `notifications`; an optional `timezone` for schedules
(§spec:schedules); and `keep_run_errors_days`, how long runs keep their
error lines (§spec:run-error-retention). The ownership, handoff, and discovery increment adds
the per-collection `fill_tags` opt-in (§spec:tag-filling) and removes
`format_filters`, `exclude_filters`, and `fallback_format_filters`:
which files to mirror follows the product rule (§spec:quality-selection).

The discovery-performance increment added a per-source politeness ceiling
(`max_concurrent_requests`; sources on one host share the smallest — the
shared concurrency budget of §spec:source-budget) and a
per-collection `incremental_discovery` opt-in
(§spec:incremental-discovery). The mirror-integrity increment adds a
per-collection `preserve_source_metadata` opt-in
(§spec:metadata-preservation) and a load-time guard that flags a
`library_layout` structurally unable to distinguish items
(§spec:layout-collision-safety). All are validated at load time like
every other key (`extra="forbid"`, so an unknown or misspelled key fails
the run rather than being ignored).

- The system shall validate the full configuration at startup and
  refuse to run on errors, naming the offending key — a scheduled
  headless tool that limps along on half-parsed config fails at 3am
  with nobody watching.
- **Secrets never appear in the YAML** (§req:quality-attributes:
  security). Credential fields name environment variables (e.g.
  `email_env: IA_EMAIL`) rather than holding values, and credentials
  entered in the dashboard are stored in the state database
  (§spec:dashboard-credentials), so the config file is safe to back up,
  share, and commit.
- **Each collection's `schedule` lives in the file** (§spec:schedules).
  Until the config-management increment there was no `schedule:` field:
  timing lived in ofelia labels in the compose file, to keep one source
  of timing truth and Shakedown out of the scheduler business. The
  operator moved timing into the dashboard on 2026-11-24
  (§req:constraints); the file is still the one source of timing truth,
  now read by Shakedown's scheduler.

## State persistence and recovery §spec:state

*Status: complete*

Complete through schema v14.

The scheduler's heartbeat, the stale-scheduler notice record
(§spec:schedules), the config file's edit state (`config_edit`,
§spec:edit-lock), and the credentials entered in the dashboard
(`source_credentials`, one row per source, §spec:dashboard-credentials)
are rows in tables outside schema versioning:
each writer (the scheduler, `serve`, setup) creates its own table when
the database lacks it and leaves `schema_version` alone, so none ever
migrates and a previous release ignores them.

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
  layout templates (also the dict a `metadata.json` sidecar is
  regenerated from without the network, §spec:metadata-preservation),
  and discovery/download/verification timestamps.
  For collections using incremental discovery it also records the
  item's last-seen source change signal (§spec:incremental-discovery),
  the token a later sync matches against to skip a metadata fetch.
  Per run: its kind (a sync, catch-up, refetch, or reconform), counts, bytes
  downloaded, errors, and timing — the raw material for `status`.
- The ownership, handoff, and discovery increment adds: each show's
  library placement — its folder and the files shakedown placed there
  (§spec:library-ownership, §spec:library-cleanup); per file shakedown
  tagged, the post-tag checksum and the tag values it wrote
  (§spec:tag-filling); per show removed upstream, the date it was first
  found missing from a complete listing (§spec:item-lifecycle — a show
  already removed before the upgrade has no date: the only candidate,
  its last verification time, is rewritten by every `reconcile`); and
  per collection, the shows covered by handoff notices not yet
  delivered (§spec:durable-handoff). The schema moves
  forward by one migration on first start of the new version; nothing
  is re-downloaded (§req:constraints: deployment status). A database
  rebuilt by `reconcile` starts with no placement record and adopts the
  existing library links the same way an upgrade does
  (§spec:library-cleanup). A release that changes the quality rule
  (§spec:quality-selection) clears every stored change signal in its
  migration, so its first sync looks every show up and applies the new
  rule, even on an incremental collection; v2.0.0 (schema v6), v2.0.2
  (schema v7) and v2.0.3 (schema v8) do. Tag-filling records are not recoverable from
  disk: after a rebuild, `verify --deep` reports files shakedown had
  tagged as drift until they are reconformed — a database rebuild loses
  the record of which bytes were shakedown's, and guessing would hide
  other tools' changes.
- Per collection whose sync was skipped because the writer lock was
  held, the database records the time of its oldest skip — at most one
  entry per collection (§spec:rescheduled-syncs). Schema v9 adds it as
  an additive migration; a database rebuilt by `reconcile` starts with
  none.
- The dashboard increment adds, as schema v10 (an additive migration):
  per run and collection, an activity entry with its kind, phase,
  progress counters, heartbeat, and result, and — for a restage or
  reconcile — the operation outcome holding its errors
  (§spec:run-activity, §spec:run-history); per
  source and minute, network totals, and each network event
  (§spec:network-activity); per collection, growth snapshots taken at
  the end of each locked run, the first by the migration itself
  (§spec:library-stats). These live in tables of their own; runs keep
  them to 30 days and trimming never touches item, run, or placement
  records. A database rebuilt by `reconcile` starts with no history.
- Schema v11 clears, once, the tag-filling fingerprint of every show
  whose recorded files include an MP3: v2.2.0 marked such a show filled
  even when an MP3's tags could not be saved, and recorded nothing about
  the skipped files (§spec:tag-filling). The first sync after the
  upgrade revisits those shows; files already filled are only read.
  Nothing else changes, and nothing is re-downloaded.
- Schema v12 keeps each show's figures with the show: its archive
  bytes, its library bytes, and whether it is kept twice — an
  unplayable original beside a playable copy (§spec:library-stats
  "Stored figures", §spec:needs-attention). The migration backfills
  every show once from its recorded manifest; on a large database that
  is one full read (about a minute on a NAS) inside the first locked
  run after the upgrade. While it runs, SQLite's write-ahead log grows
  to about the size of the database, on the volume that holds it; a
  volume without that much free space fails the run, the migration
  rolls back whole, and the next run tries again. Additive: nothing
  else changes, and nothing is re-downloaded.
- Schema v13 records, per checked audio file, the real
  format and class found by its content (§spec:real-format), beside the
  recorded manifest and outside its change-detection identity. It is
  additive: it adds no check itself — the first sync after the upgrade
  checks stored files (§spec:real-format-catch-up) — and it clears the
  tag-filling fingerprint of every show holding a file named `.m4a`, so
  a `fill_tags` collection's M4A files are filled once
  (§spec:m4a-tags). A database rebuilt by `reconcile` starts with no
  checks, and the next sync makes them again from the files. Nothing is
  re-downloaded.
- Schema v14 records, per run, when its error detail was trimmed, and
  on a `trim` run's activity entry the runs it trimmed and the bytes the
  database shrank by (§spec:run-error-retention). Additive: the columns
  start empty and nothing else changes.
- Only a command that holds the writer lock migrates the database.
  Commands that take no lock — `status`, `item show`, `sync --dry-run`,
  `verify` without `--reconform`, `validate`, and `serve`'s
  `GET /status` and `GET /metrics` — open it at whatever schema version
  it has; on an older version they show what that version records (no
  rescheduled entries before v9; before v12, byte totals summed from
  every manifest, which is slow on a large database). A reader that migrated while a run of
  the previous release still writes would make that run refuse the
  database the next time it opens it. With no database yet, they create
  one at the current version.
- `shakedown reconcile` rebuilds the database from scratch by walking
  the archive tree and re-fetching **manifests only** (never content)
  from the source. Because archive paths mirror source identifiers
  (§spec:system-shape), the walk recovers each item's identity; a
  corrupted or reset database therefore never forces a re-download
  (§req:quality-attributes: durability). This is also the migration
  path from a pre-existing manual mirror. A known item missing from a
  complete source listing is marked `disappeared`; when the source
  cannot be reached, every known item keeps its prior status — an
  outage is not a takedown.

The one deliberately unrecoverable case: the user wipes the archive
tree itself. Then database and library are both invalidated and a full
re-download is required — the archive *is* the system of record, and
pretending otherwise would be false durability.

## Library handoff and notifications §spec:handoff

*Status: complete*

After a sync that staged at least one item for a collection (or that
has undelivered shows to resend, §spec:durable-handoff), the system
fires that collection's `on_complete` action — this is how new
shows flow into the library tool with nobody watching
(§req:user-stories). Two mechanisms, webhook as default and exec as
fallback (§req:constraints), both fired once per (collection, run) with
the `{source}`/`{collection}`/`{staging_root}` template fields available
in the URL or command. A command is split into its arguments as the file
writes it before the fields are filled in, so a value with a space or a
quote in it (a library path saved from the dashboard) stays inside its
one argument:

- **Webhook** (`on_complete.webhook: <url>`): HTTP POST of a versioned
  JSON body listing the staged shows with their archive and library
  paths. The payload contract is documented in `docs/handoff.md`.
- **Exec** (`on_complete.exec: <command>`): a configured command run
  inside the shakedown container with the same JSON on stdin. Exists
  because many library tools have a CLI but no HTTP listener; the
  command must be available in the shakedown image or reachable from it.

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
items. Delivery failures never fail the sync itself: the mirror's
integrity does not depend on a listener being up. How handoff delivery
is retried and surfaced is §spec:durable-handoff. Payloads contain
paths and counts only, never credentials.

## Durable handoff §spec:durable-handoff

*Status: complete*

A handoff notice is either delivered or visibly pending and resent; a
handoff only triggers the library tool's work and never waits for that
work to finish; and every handoff recipe the project ships works when
followed literally
(§req:success-criteria #21, §req:quality-attributes: durable handoff,
§req:constraints: library handoff).

Observable behavior:

- **A trigger, not a wait.** A handoff tells the library tool that new
  shows are ready; the tool's own work (an import, a scan) runs on its
  own time. A receiver is expected to acknowledge within minutes.
  Shakedown waits up to five minutes for a webhook's answer or an exec
  command's exit; past that, the notice is undelivered (reason:
  "no answer within 5 minutes") and the command is stopped. What the
  library tool does after acknowledging — whether its import succeeded —
  is outside shakedown and not tracked. (Operator decision, 2026-11-10.)
- **Delivered means success.** An exec that exits 0, or a webhook
  answered 2xx, delivers the notice. Anything else — a non-zero exit, a
  failure to start the command, a connection failure, a non-2xx answer —
  leaves it undelivered, with the reason recorded (for exec, the exit
  status and the last lines of its error output).
- **Undelivered shows are kept and resent.** The system records, per
  collection, the shows covered by notices not yet delivered. The next
  run of that collection — a sync or a single-show run
  (§spec:single-show-runs), whether or not it staged anything new —
  sends one notice whose `staged` list holds the newly staged shows plus
  every pending one, at their current library paths. A delivered notice
  clears the shows it covered. A show pruned meanwhile
  leaves the pending list. Removing a collection's `on_complete` drops
  its pending list; changing the target sends the pending shows to the
  new target.
- **Visible in status.** `shakedown status` (text) shows, per
  collection, "handoff not delivered: N shows since \<time>" with the
  last reason; `status --json` and `GET /status` carry the same, with
  the pending identifiers.
- **Does not fail the run.** An undelivered notice does not change the
  run's outcome or exit code, by itself.
- **Payload.** The envelope keeps its shape with `payload_version: 2`:
  `staged` now means "staged and not yet delivered," so it can include
  shows from earlier runs. Each entry keeps identifier, archive path,
  and library path.
- **Failure notifications are not queued.** An `on_failure` webhook
  that cannot be delivered is shown in that run's status but not
  resent: it describes that run's state, which `status` already shows,
  and a late alert about a run since superseded is noise.
- **Shipped recipe.** The example config and docs ship no handoff to
  Beets: stock Beets offers no HTTP route that imports, and `beet` is
  not in the shakedown image, so neither a webhook nor an exec to Beets
  can work as shipped. The documented listening path is Navidrome
  reading shakedown's library directly: Navidrome's own folder watcher
  (on by default) picks up new shows with no handoff configured. The
  example compose stack points Navidrome at the library tree. Webhook
  and exec remain generic features, documented with the payload
  contract; every example target in the shipped config either is a
  service in the example compose stack or is marked as a placeholder
  for the operator's own receiver. (Operator decision, 2026-11-10.)

**Decision and constraint.** A per-collection pending list, resent
merged into the next notice, because §req:quality-attributes requires
a notice to be delivered or visibly pending and never silently lost,
and library imports are batch operations that gain nothing from
replaying old notices one by one. A handoff is a trigger with a
bounded wait, because a webhook-style receiver acknowledges quickly and
does the slow work afterwards; the old 30-second limit was the defect
only because it was applied to a command doing the whole import inline,
which a trigger never does. Navidrome's watcher is the recipe because it is the stock
mechanism the operator already uses (principle: don't rebuild what
others maintain); the Beets webhook in the old example was observed to
404 on the NAS.

**Alternatives rejected.** *No time limit, so an inline import can
finish* — a stuck receiver would hold the run and the writer lock
indefinitely, and waiting on the library tool's own work is not
shakedown's job. *Track whether the downstream import succeeded* — out
of scope; shakedown reports only whether the trigger was acknowledged. *Replay each failed notice separately* — floods
the receiver after an outage. *Fail the run on undelivered notices* —
the mirror's integrity doesn't depend on the listener
(§spec:handoff), and every scheduled run would fail while Beets is
down. *Ship Beets inside the shakedown image* or *a receiver sidecar
container* — both make shakedown maintain a Beets integration; the
operator chose neither.

**Tradeoffs.** A command that does a whole import inline and takes
longer than five minutes is stopped and counted undelivered; such a
command should start the import and return. Resent notices can repeat a show a receiver
partly processed; receivers must tolerate a show they already have
(`beet import` does).

**User-level test path.** Configure a webhook receiver that answers
after a few seconds; sync; `status` shows no pending notice. Configure
one that never answers; sync; after five minutes the notice is
undelivered with the reason shown, and the run ends. Point the webhook at an unreachable host; sync; `status`
shows the undelivered notice with its shows and reason, and the run
exits 0. Fix the target; sync again with nothing new upstream; the
receiver gets one notice listing the pending shows, and `status` clears.
Follow the shipped docs on a fresh compose stack: a newly synced show
appears in Navidrome with no handoff configured.

## Serve control plane §spec:serve

*Status: complete*

`shakedown serve` exposes a small HTTP API for ad-hoc triggers and
monitoring without `docker exec` (§req:priorities #10), serves the web
dashboard, and watches the scheduler (§spec:schedules). It runs as a
long-lived container beside the scheduler's. It never schedules: the
separate `shakedown schedule` container does, and until the
config-management increment ofelia did (§req:constraints, superseded
2026-11-24).

| Endpoint | Behavior |
| --- | --- |
| `GET /healthz` | Liveness |
| `GET /status` | JSON equivalent of `shakedown status`, under `collections`, beside `scheduler` (§spec:schedules) |
| `GET /metrics` | Prometheus metrics |
| `POST /sync?source=&collection=` | Start an ad-hoc sync and answer at once (write gate; §spec:api-sync-runs) |
| `POST /verify` | Start a verify run and answer at once (write gate; §spec:api-sync-runs) |
| `POST /validate` | Run setup readiness validation (write gate; live handoff validation takes the token only) |
| `GET /` | The web dashboard (§spec:dashboard) |
| `GET /api/activity` | Live runs and network now (§spec:run-activity, §spec:network-activity) |
| `GET /api/network` | Per-source network history (§spec:network-activity) |
| `GET /api/runs` | Runs of the last 30 days (§spec:run-history) |
| `GET /api/library` | Library, database, disk, and growth (§spec:library-stats) |
| `GET /api/attention` | Needs attention and library health (§spec:needs-attention) |
| `GET /api/health` | Release, config validity, last validation (§spec:dashboard-health) |
| `GET /api/config` | The config file's settings, edit state, credential status, next syncs, unconfigured collections (§spec:dashboard-settings) |
| `PUT /api/config` | Save settings (write gate; §spec:dashboard-settings, §spec:edit-lock) |
| `POST /api/config/check-paths` | Path checks without saving (write gate; §spec:dashboard-settings) |
| `POST /api/config/override` | Turn on the edit override (write gate; §spec:edit-lock) |
| `GET /api/setup`, `POST /api/setup` | First-run setup (write gate on `POST`; §spec:first-run-setup) |
| `PUT /api/credentials`, `DELETE /api/credentials` | Store or forget a source's credentials (write gate; §spec:dashboard-credentials) |
| `POST /api/collections/purge` | Delete an unconfigured collection's files (write gate; §spec:collection-removal) |
| `POST /api/history/trim` | Trim old run error detail now and compact the database (write gate; §spec:run-error-retention) |

`POST /sync`, `/verify`, and `/validate` answer `404`, naming the
configured sources or collections, when `source` or `collection` names
nothing configured.

Security posture (this endpoint accepts requests that cause network
fetches and disk writes): every mutating endpoint passes the write gate
of §spec:dashboard-run-controls — the bearer token supplied via
environment variable, or a same-origin request from the dashboard's own
page. Until the config-management increment the token was the only way
in and, unset, disabled every mutating endpoint, so a reverse-proxy
misconfiguration could not turn into an unauthenticated write API; the
operator chose the proxy as the one gate for dashboard writes on
2026-11-24 (§req:constraints: no login of its own). Read endpoints (`/healthz`, `/status`,
`/metrics`) are unauthenticated: they expose only operational
metadata (paths, counts, timings — no credentials), and health/metrics
scrapers generally can't send tokens. The dashboard page and its
`/api/*` endpoints are read endpoints under the same posture; the
operator's reverse proxy decides who opens them (§spec:dashboard).
`serve` is required for the dashboard (§req:success-criteria #28–#33)
and for nothing else: scheduled syncs need the scheduler, not `serve`
(§spec:schedules), and every sync runs and records its progress the
same with `serve` stopped.

## API-triggered runs §spec:api-sync-runs

*Status: complete*

A sync triggered through `serve` runs with the configuration as it is
at the moment of the request, and never overlaps another run
(§req:success-criteria #24, §req:quality-attributes: config edits take
effect without restarts; §req:constraints: a triggered sync is an
ordinary run).

Observable behavior:

- **Fresh run, current config.** `POST /sync` starts a new, separate
  shakedown run — the same one-shot run the scheduler launches — which
  reads the config file at start. A config edit therefore takes effect
  on the next trigger without restarting `serve`.
- **Answers at once.** When the new run has taken the writer lock,
  `serve` answers `202 Accepted` with the scope it started:
  `{"started": "sync" | "verify", "source", "collection"}`. Each
  collection's run record opens when that collection starts
  (§spec:sync-workflow "Waiting collections"); progress and the result
  are read from `GET /status` like any run's, where each collection's
  latest run carries its id. The run tells `serve` each record as it
  opens, so the run record always closes, even if the run crashes or is
  killed (§spec:failure-behavior). Until 2026-11-20 the answer listed
  one run id per collection (operator decision, 2026-11-10); that needed
  every record open before any collection began, which made collections
  waiting for a slot read as stuck runs, so the ids now come from
  `GET /status` only (operator decision, 2026-11-20).
- **Refuses immediately when busy.** If another run holds the writer
  lock, the request is refused with `409 Conflict` and the message "a
  run is in progress; try again when it finishes". Nothing is queued
  and nothing is rescheduled: the caller is there to retry
  (§spec:rescheduled-syncs).
- **A triggered sync catches up like any sync.** When its own work
  finishes, it runs the collections rescheduled at that moment
  (§spec:rescheduled-syncs). The catch-ups show in `GET /status` as each
  collection's latest run, and `serve` closes their records too if the
  run is killed.
- `POST /verify` follows the same pattern (fresh run, `202`, `409` when
  a reconform cannot take the lock). `POST /validate`, `GET /status`,
  and `GET /metrics` read the config as it is at the request.
- **Config errors are answers, not crashes.** If the edited config is
  invalid, each endpoint that needs it answers `503` with the config
  error naming the offending key (§spec:configuration; operator
  decision, 2026-11-10); `serve` keeps running
  so the operator can fix the file and retry. The write gate and
  unknown-scope `404` behave as before (§spec:serve).
- **The run counter keeps its meaning.** `serve` watches each sync it
  started and counts it in `shakedown_sync_runs_total` as `ok`,
  `stopped` (it exited 143 or 130, §spec:graceful-stop), or `fail` when
  it exits; a refused (`409`) trigger and a verify are not
  counted (operator decision, 2026-11-10).

**Decision and constraint.** A triggered sync is a fresh one-shot run,
not an in-process call, because §req:constraints makes runs one-shot
and stateless with the scheduler-launched run as the model, and a
separate run picks up the current config and isolates a crash from the
control plane for free. `serve` answers at once because a large sync
takes hours, and an HTTP request held that long times out in proxies
and dashboard buttons. (Operator decision, 2026-11-10.)

**Alternatives rejected.** *Reload the config in-process per request* —
meets the config requirement but keeps long syncs inside the
long-lived control plane, where a crash or leak takes `serve` down.
*Hold the request until the sync ends* — times out for any real sync.
*Queue a second request behind the running one* — REQUIREMENTS asks
for a clean refusal, and a queue hides that a run was already going.

**Tradeoffs.** The trigger's result is no longer in the HTTP response;
callers poll `GET /status`. The `serve` container must be able to start
a shakedown run (it already has the image, config, and data mounts).

**User-level test path.** With `serve` running, change a collection's
`library_layout` in the config and `POST /sync` for it: the answer is
`202` at once and the run stages under the new layout. Start a long
sync, then `POST /sync` again: `409` with the in-progress message. Break
the config file: endpoints answer with the config error and `serve`
stays up.

## Rescheduled syncs §spec:rescheduled-syncs

*Status: complete*

Shipped as an additive state-database migration (schema v9) and a new
per-collection `status` field; CLI exit codes, config keys, and the HTTP
API are unchanged (deployed on one operator's NAS, no external
consumers; §req:constraints).

A single-collection sync that cannot start because another run holds
the writer lock is remembered, once per collection, and caught up when
the running sync finishes, oldest skip first — so a days-long run
delays other collections' scheduled syncs but never cancels them
(§req:success-criteria #1, #27; §req:quality-attributes: scheduled runs
are observable, truthful visible state; §req:constraints: one-shot CLI,
external scheduler; §req:priorities #30).

Observable behavior:

- **What is rescheduled.** A `sync` scoped with `--collection` (with or
  without `--source`), started from the scheduler or typed by the
  operator, that finds the writer lock held records each (source,
  collection) it covers as rescheduled. It logs, as a warning, that the
  collection was rescheduled because another run holds the writer lock,
  naming the collection and the time of its oldest skip. It still exits
  1, so the tick fails visibly at the scheduler, as today.
- **One entry per collection.** The entry is keyed by (source,
  collection) and keeps the time of the oldest skip. A second skip of
  the same collection neither adds an entry nor moves the time.
- **What is not rescheduled.** A sync with no `--collection` (all
  collections, or every collection of one `--source`), an API
  `POST /sync` (still `409` at once, §spec:api-sync-runs),
  `sync --refresh-metadata`, `restage`, `reconcile`,
  `verify --reconform`, and `item refetch` refuse exactly as today and
  leave no entry. `sync --dry-run` never takes the lock.
- **Recording never needs the lock and never migrates.** The skipped
  sync writes its entry to the state database without the writer lock,
  waiting on the database the bounded time any writer waits. If the
  entry cannot be written — the database is busy past that wait,
  unreadable, or still at an older schema version because a run of the
  previous release is writing — nothing is recorded, the log says why,
  and the tick exits 1 exactly as before this feature.
- **A finishing sync catches up.** When any sync that holds the lock
  finishes its own work — any scope, started by the scheduler, the
  operator, or `serve`, whatever its result; not
  `sync --refresh-metadata` — it then, still holding the lock:
  1. clears the entries of the collections its own work finished
     (see Clearing);
  2. reads the config file as it is now;
  3. takes every entry outstanding at that moment, dropping (and
     logging) any whose source or collection the current config no
     longer names;
  4. syncs the remaining collections as one multi-collection sync,
     started oldest skip first, under the same
     `max_concurrent_collections` cap and shared source budget as any
     multi-collection sync (§spec:sync-workflow, §spec:source-budget);
  5. exits.
  Entries recorded after step 3 wait for the next sync to finish. If
  the current config is invalid at step 2, the catch-up does not run,
  every entry is kept, the config error is logged, and the run exits
  non-zero. Restage, reconcile, `verify --reconform`, and
  `item refetch` never catch up: they exit when their own work is done.
- **A catch-up is an ordinary sync.** Each caught-up collection gets
  its own run record (kind `sync`) and outcome, stages, sends its
  handoff, and fires its failure notification like any sync; its log
  lines mark it as a catch-up and name its skip time.
- **Clearing.** A sync clears a collection's entry when it finishes
  that collection with a complete enumeration — completed, or completed
  with item issues — whether it was the collection's own tick, a manual
  sync, an all-collections sync, an API sync, or a catch-up. A catch-up
  clears its entry whatever its result; a stale or crashed catch-up
  shows in `status` like any failed sync, and the collection's next tick
  retries it. A sync that is not a catch-up and ends stale or crashed
  keeps the entry, so the next finishing sync catches it up — which can
  be that same run: its catch-up retries the collection once. A run
  killed before it finishes a collection clears nothing; neither does a
  run stopped on request, catch-up or not, and a stopped sync runs no
  catch-up (§spec:graceful-stop). A finishing
  run clears its collection's entry even when that skip was recorded
  while the run was going.
- **Exit codes.** The skipped tick exits 1. A finishing sync exits
  non-zero when its own work or any of its catch-ups failed — the rule
  every multi-collection sync already follows (§spec:cli).
- **Visible in status.** `shakedown status` shows, per collection with
  an entry, that its sync was rescheduled, the time of its oldest skip,
  and that it runs as a catch-up sync (`rescheduled: first skipped
  <time>; runs as a catch-up sync`, worded like Needs attention's entry,
  approved 2026-11-20, so it stays true once its catch-up round has
  begun). `status --json` and
  `GET /status` carry `rescheduled: {"since": <time>}` per collection,
  or `null`, with the time formatted like every other time in `status`.
  An entry for a collection no longer configured is not shown. A
  caught-up collection still waiting for its slot shows that it waits
  (`waiting: since <time>; starts when a slot frees in the current
  sync`, and `waiting: {"kind", "since"}` in JSON) beside its
  rescheduled line, and no recovery warning: it has no run yet
  (§spec:sync-workflow "Waiting collections").
- **API-triggered runs.** A sync started by `POST /sync` catches up like
  any sync; catch-ups appear in `GET /status` as each collection's
  latest run. If that run is killed during a catch-up, `serve` closes
  the catch-up's run record as it does the run's own
  (§spec:api-sync-runs); the entry is still there, so the next
  finishing sync runs that collection again.
- **Rebuilds.** A database rebuilt by `reconcile` after it was lost or
  reset starts with no entries; each collection's next tick runs as
  usual. `reconcile` over an existing database leaves its entries in
  place, as it does placement records.

**Decision and constraint.** Record the skip and let the next lock
holder catch it up, because §req:success-criteria #27 requires a long
run to delay rather than cancel other collections while
§req:constraints keeps one writer at a time and keeps shakedown out of
the scheduler business: no timing of its own, no runs started on its
own initiative — a catch-up only extends a run something else started.
(Operator decision, 2026-11-11, §req:priorities #30.)
Entries live in the state database because it already serializes
concurrent writers, makes "one entry per collection" a key constraint
rather than a convention, and is backed up with the archive
(§spec:state). Recording never migrates because a run of an older
release refuses a newer schema version the next time it opens the
database: a skipped tick that migrated would break the very run it was
waiting for, and upgrading mid-run is exactly when a days-long sync is
likely to be going. The catch-up reads the config afresh because the
finishing run may have loaded its config days earlier, and a collection
added since would otherwise be dropped as unknown
(§req:quality-attributes: config edits take effect without restarts).
A complete enumeration with item issues clears the entry because the
next sync retries failed items anyway, and because a days-long
collection that skipped its own tick would otherwise catch itself up
at once and hold the lock for days more. The catch-up stops at the
entries outstanding when it starts so every run ends: one that kept
draining new skips could run forever while ticks keep arriving.
Catch-ups use the ordinary multi-collection path because that path
already bounds concurrency and shares the source request budget;
starting them oldest first is the ordering rule.

**Alternatives rejected.** *A lock per collection, so collections sync
side by side* — removes the starvation at its source, but the
archive.org request budget is shared only within one process, so two
processes would double the request rate, and restage, reconcile, and
refetch would still need a global lock; the operator chose catch-up
(§req:priorities #30). *Wait for the lock with a time bound* — a
multi-day run outlives any sensible bound, and the waiting container
holds the tick open the whole time. *A sidecar file* — needs its own
locking and atomic writes, which the state database already provides.
*A queue of every skip* — duplicates pile up; one entry per collection
with its oldest time is all the ordering rule needs. *Catch up one
collection per finishing run* — a collection behind two others waits
weeks. *Catch up serially* — leaves the second collection worker idle
for no ordering gain the start order does not already give. *Exit 0 on
a recorded skip* — §req:success-criteria #27 keeps the tick failing at
the scheduler. *A distinct exit code for a skip* — ofelia only tells
zero from non-zero; the log already says "rescheduled". *Reschedule API
refusals* — §req:success-criteria #24 asks for a clean refusal, and the
caller is there to retry. *A separate run kind for catch-ups* —
staleness in `status` reads the latest sync, and a catch-up is one.
*A rescheduled metric* — not required; `GET /status` carries the
entries for dashboards.

**Tradeoffs.** A skip recorded while a catch-up runs, or in the moment
after the holder takes its list, waits for the next sync to finish —
up to the next tick of any collection. The finishing job's container
runs longer, and its exit code at the scheduler includes other
collections' catch-up failures; the log names which collection failed.
The first skip after upgrading to this release, while the previous
release's run still writes, is not recorded. A run killed mid-catch-up
leaves that catch-up's run record open until the collection runs
again.

**User-level test path.** Start a long sync of one collection. While it
runs, start `sync --collection B`, then `sync --collection C`, then
`sync --collection B` again: each exits 1 and logs "rescheduled";
`status` shows B and C rescheduled, B keeping its first skip time, and
`status --json` carries the same `since` times. `POST /sync` for a
third collection answers `409` and adds no entry; so does `restage`.
Let the long sync finish: B syncs, then C (B started first), both show
as their latest runs, and `status` shows no rescheduled entries
afterwards. Remove C from the config while it waits: the catch-up skips
C and drops its entry. Start a skipped tick with the previous release
still writing: it exits 1, records nothing, and the running sync
finishes normally.

## Web dashboard §spec:dashboard

*Status: in progress*
The config-management increment lifts the read-only rule for explicit actions (§spec:dashboard-run-controls); that part is not built yet.

Compatibility, from deployment status "deployed, no outside consumers"
(§req:constraints, operator-confirmed 2026-11-12): HTTP API — free; CLI
and `status` output — free; config — free; dashboard pages and their
JSON — free; state database — moves forward by one additive migration
and never forces a re-download.

`shakedown serve` serves one read-only web dashboard at its root path
(`GET /`), usable on a phone and on a desktop browser
(§req:success-criteria #28–#33; §req:constraints: UI line, read-only
dashboard, dashboard lives in serve, no login of its own). The page is
a client of the JSON endpoints specified in §spec:run-activity,
§spec:network-activity, §spec:run-history, §spec:library-stats,
§spec:needs-attention, and §spec:dashboard-health; it shows nothing
those endpoints do not supply. Visual design — component kit, layout,
glossary, screen states — is owned by the Design task and recorded in
§spec:ui-kit once approved; this section fixes only behavior.

Observable behavior:

- **Looking changes nothing.** Only an explicit action changes
  anything: saving settings (§spec:dashboard-settings), turning on the
  edit override (§spec:edit-lock), entering credentials
  (§spec:dashboard-credentials), finishing setup
  (§spec:first-run-setup), Sync now and Validate
  (§spec:dashboard-run-controls), and deleting an unconfigured
  collection's files (§spec:collection-removal). Until the
  config-management increment the page had no such control
  (§req:constraints, superseded 2026-11-24). Every dashboard read
  endpoint is a `GET`. Answering one never starts a run,
  never sends a request to a source, never writes to the archive, the
  library, or the state database, and never takes the writer lock. Its
  only inputs are the state database, the config file, and the free
  space of the data volume.
- **Live where it matters, light everywhere.** The page refreshes the
  live view (§spec:run-activity) every few seconds and the other views
  about every 30 seconds, and stops refreshing while it is hidden (a
  background tab, a locked phone). `serve` computes each endpoint's
  answer at most once per refresh interval and hands the same answer to
  every viewer, so ten open pages cost the NAS what one does
  (§req:quality-attributes: live, but light). A viewer never waits on a
  recompute: once an answer's interval passes, `serve` keeps handing it
  out for one more interval while a single refresh computes the next
  behind it. An answer older than two intervals, or one whose refresh
  failed, is not handed out again: the next viewer waits for the
  recompute (sharing one already running) and receives its answer or
  its error.
- **Old data is labelled, never passed off as current.** Every answer
  carries `generated_at`, the time `serve` computed it. When a refresh
  fails or no answer has arrived for twice its refresh interval, the
  page says it cannot reach shakedown and shows how old the data on
  screen is. Ages count from when an answer arrived, by the browser's
  clock: `serve` hands out an answer up to two intervals old, and a
  server clock that differs from the browser's must never read as an
  outage (`web/tests/freshness.spec.ts`). A run that stopped reporting is shown as ended,
  never as running (§spec:run-activity).
- **No login of its own; same posture as `GET /status`.** The page and
  its endpoints are unauthenticated, like the existing read endpoints;
  the operator's reverse proxy (for example Authelia) decides who may
  open them. They expose operational data only — counts, identifiers,
  paths, timings, error text — never credentials or secrets
  (§req:quality-attributes: security of secrets). The settings resource
  shows the config file's own values as the file holds them, webhook
  URLs included, since an editor must show what it edits and anyone who
  can open the page can already replace them (operator decision,
  2026-11-24). Writes pass the write gate of
  §spec:dashboard-run-controls.
- **Works behind a proxy, at any path.** The page addresses its assets
  and endpoints by relative URL, so it works at a host's root or under a
  path prefix the proxy adds, with no setting.
- **Compressed.** `serve` gzips any answer or asset over 1 KB for a
  client that asks (`Accept-Encoding: gzip`). A proxy that passes
  `Content-Encoding` through, as nginx-proxy-manager does, hands the
  browser the compressed answer. Answers carry no secrets, so
  compression exposes nothing an attacker could guess at.
- **Self-contained.** Every asset the page needs ships inside the
  shakedown image and is served by `serve`. The browser fetches nothing
  from a third party (no CDN, fonts, or analytics), so the page works on
  a LAN with no internet and tells no outside party it was opened.
  `serve` sends no cross-origin (CORS) allowances.
- **Source text is text.** Show identifiers, titles, folder names, and
  error messages come from upstream metadata and servers; the page
  renders every such string as plain text, never as markup. A crafted
  title on archive.org cannot run script in the operator's browser.
- **Codes, not copy.** Endpoints return enumerated codes (a run's
  `kind`, `phase`, `result`; an attention entry's `kind`) and raw
  values; the page owns every word a person reads. The one exception is
  `safe_next_action`, which `status` already reports as text and the
  page shows as `status` does.
- **When the config is broken.** Each endpoint reads the config as it is
  at the request (§spec:api-sync-runs). When the file is invalid, the
  dashboard keeps answering from the last config this `serve` loaded
  successfully and reports the error through §spec:dashboard-health; a
  `serve` that has never loaded a valid config answers `503` with the
  config error naming the key, as the other endpoints do.

**Decision and constraint.** The dashboard is a static client served by
the always-on `serve` process, reading JSON endpoints by polling. The
operator placed it in `serve` and accepted running `serve` all the time
(§req:constraints). Polling, not a push stream, because the page must
work behind any reverse proxy and push streams (server-sent events,
WebSockets) are buffered or cut by common proxy defaults unless the
operator tunes the proxy; a few-second poll of a shared, cached answer
meets "within a few seconds" at negligible cost. A JSON API under the
page, rather than server-rendered pages, because the screens are
designed separately against a stated contract, and the same endpoints
serve Homepage-style widgets or scripts with no extra work.

**Alternatives rejected.** *Server-sent events or WebSockets* — fresher
by a second or two, but fragile behind Authelia/nginx defaults and a
long-lived connection per open device. *A separate dashboard container*
— another image and process to run for no gain; `serve` already has the
mounts and the config. *Grafana on the Prometheus metrics* — shows
counters, not a run's progress, the attention list, or history
`status` holds, and adds two more services. *Loading the kit from a
CDN* — breaks on a LAN without internet and leaks each page view.

Answers refresh behind the viewer because a recompute inside the
request made every viewer wait for it: on a cold, busy NAS the page
showed placeholders for a minute (v2.2.2). Two intervals bound the age
of what `serve` hands out, so the page's own staleness rule stays
true.

**Tradeoffs.** Up to a few seconds of lag on the live view; any view
may show data up to two intervals old. The page
needs `serve` running; with `serve` down there is no dashboard, but
nothing is lost (§spec:run-activity, §spec:network-activity record from
the runs themselves). The unauthenticated page relies on the operator's
proxy; anyone who reaches `serve`'s port directly sees operational data,
the same exposure `GET /status` has today.

**User-level test path.** Open the dashboard through the proxy on a phone and a
desktop: the dashboard loads with no request leaving the LAN (browser
network panel). Browsing every tab without pressing an action makes only
`GET` requests. Put it behind a proxy under `/shakedown/`: it still
loads. Stop `serve` with the page open: within a minute the page says it
cannot reach shakedown and shows the age of what it shows. Give a test
show a title containing `<script>`: it renders as literal text. Break
`shakedown.yaml`: the page stays up and shows the config error.

## Live run progress §spec:run-activity

*Status: complete*

Every run reports its progress while it runs, and the dashboard shows
it within seconds (§req:success-criteria #28, #33;
§req:user-stories: is the long sync stuck?; §req:quality-attributes:
the dashboard tells the same truth, live but light, nothing lost while
the dashboard is down; §req:priorities #32).

Observable behavior:

- **Every kind of run reports.** A sync (scheduled, manual, or started
  through `serve`), a catch-up sync (§spec:rescheduled-syncs), a
  single-show refetch or reconform (§spec:single-show-runs), a restage,
  a verify, and a reconcile each record one activity entry per
  collection it works on. The run itself records it, into the state
  database, so a run started by hand while `serve` is down shows the
  moment `serve` starts.
- **What an entry says.** Collection; kind (`sync`, `catch_up`,
  `refetch`, `reconform`, `restage`, `verify`, `reconcile`, `trim`,
  `purge`); whether
  `serve` started it; start time; current phase (`starting`, `listing`,
  `discovering`, `downloading`, `tagging`, `staging`, `notifying`,
  `verifying`, `finishing`); shows listed, shows to look up and looked
  up; shows to fetch, done, and failed; the shows being worked on now;
  bytes downloaded so far; current speed; and an estimated time left.
  Kinds without a phase or counter leave it null (a restage lists
  nothing; a trim has neither, belongs to no collection, and reports the
  runs it trimmed and the bytes freed instead, §spec:run-error-retention;
  a purge has neither and reports the bytes it freed,
  §spec:collection-removal). A sync's lookups and downloads overlap
  (§spec:discovery-pipeline), so both counters show at once and the
  phase reads `discovering` until the listing's lookups are done.
- **Honest totals.** Shows to fetch, and their bytes, are only known once
  enumeration ends; until then they are null and so is the estimate.
  After that, time left is remaining bytes over the speed of the last
  ten minutes, or remaining shows over the pace so far when the source
  lists no sizes; it is null while speed is zero. The page shows null as
  "estimating", never as a guess.
- **Is it stuck?** Each entry carries `last_progress_at` — the last
  time any counter or byte count moved, or a stored show's audio was
  checked (§spec:real-format-catch-up) — next to `last_heartbeat_at`.
  "Alive but no progress for 40 minutes" and "stopped reporting" are
  different facts and the page shows them differently. The API carries
  no verdict on progress; the page marks a running run with no progress
  for 5 minutes (§spec:ui-kit).
- **Waiting collections.** A collection waiting for a slot in a
  multi-collection sync (§spec:sync-workflow "Waiting collections") is
  not a run yet: it has a waiting entry — collection, kind, whether
  `serve` started the run, and since when it waits — with no run
  record, phase, or counters, kept alive by its run's heartbeat. When
  it takes a slot the entry becomes that collection's running entry,
  started then, with its run record. If its run ends first (stopped,
  crashed, or killed), it never started and leaves no trace: its run
  removes the entry, or the next run that takes the writer lock does
  as it starts (only a lock holder has waiting entries); until then a
  reader stops listing it once its heartbeat is stale. Waiting entries are never in `runs`, the
  run history, or Needs attention; `status` shows a waiting line for the
  collection (§spec:rescheduled-syncs "Visible in status").
- **Ends are never missed.** A running entry updates its heartbeat every
  few seconds. When a run finishes it records its result, using the
  outcome codes `status` already uses (`completed`,
  `completed_with_item_issues`, `failed_before_completion`,
  `stale_enumeration`), or `stopped` for a run stopped on request
  (§spec:graceful-stop). An entry whose heartbeat is more than about 20
  seconds old is reported as `failed_before_completion` with end reason
  `stopped_reporting` — killed, crashed, or the host went down. The live
  view keeps each ended entry for ten minutes so its result and reason
  are seen; after that it appears only in §spec:run-history.
- **`status` agrees, during and after.** A sync's or single-show run's
  counts in its run record (new, updated, failed, bytes downloaded)
  move at the same cadence as the activity entry, so `status` and the
  dashboard show the same numbers mid-run. While a run still reports,
  `status` shows no recovery warning for its operation in progress —
  the run is working, and Needs attention already left it out — and
  shows one once its heartbeat is stale. A run record left open by a
  killed run is reported by `status` as ended unexpectedly as soon as
  its heartbeat goes stale, and the next run that takes the writer lock
  closes it with the time of its last heartbeat and the error "run ended
  unexpectedly (lost contact)", together with any outcome it left
  in progress. Before this, a killed scheduled run's record stayed open
  forever — only runs `serve` started were closed (§spec:api-sync-runs) —
  contradicting §spec:failure-behavior ("the run record is always
  closed, even on a crash").
- **Recording never costs a run anything.** Progress is written in small
  updates every few seconds, not per event, from outside the run's own
  work. A write that cannot land (database busy) is skipped; the next
  one carries cumulative totals, so nothing drifts. A recording failure
  never fails, slows, or changes a run (§req:quality-attributes:
  recording must not weaken correctness, idempotency, politeness, or
  per-item isolation). A killed run loses at most the last few seconds
  of its progress.

API: `GET /api/activity` (unauthenticated; refreshed every few seconds)
answers `{generated_at, runs: [...], waiting: [...], network: [...]}`.
`network` is specified in §spec:network-activity. Each `runs` entry:

| Field | Meaning |
| --- | --- |
| `activity_id` | The entry's id |
| `run_id` | The run record's id when the kind has one (sync, catch_up, refetch, reconform), else null — the id `status` and `POST /sync` report |
| `source`, `collection` | The collection the entry covers; null for a `trim` |
| `kind` | `sync`, `catch_up`, `refetch`, `reconform`, `restage`, `verify`, `reconcile`, `trim`, `purge` |
| `started_by_serve` | true when `serve` launched the run |
| `started_at`, `finished_at` | ISO times; `finished_at` null while running |
| `last_heartbeat_at`, `last_progress_at` | ISO times |
| `state` | `running` or `ended` |
| `result` | null while running, else an outcome code above |
| `end_reason` | null, or `stopped_reporting` |
| `phase` | a phase code above, or null |
| `items_listed`, `items_to_look_up`, `items_looked_up` | counts or null |
| `items_to_fetch`, `items_done`, `items_failed` | counts; `items_to_fetch` null until known |
| `current_items` | identifiers being worked on now |
| `bytes_downloaded`, `bytes_to_fetch` | bytes; `bytes_to_fetch` null until known |
| `speed_bytes_per_second` | over the last ~10 seconds |
| `eta_seconds` | null when not computable |
| `runs_trimmed`, `bytes_freed` | a `trim`'s runs trimmed and bytes the database shrank by, set as it ends; a `purge`'s bytes freed in `bytes_freed`; null for other kinds |

`runs` holds running entries and entries that ended in the last ten
minutes, running first, then newest first.

`waiting` holds the collections waiting for a slot in a run that still
reports, in the order they start. Each entry:

| Field | Meaning |
| --- | --- |
| `activity_id` | The entry's id; the same id carries its running entry once it starts |
| `source`, `collection` | The collection that waits |
| `kind` | `sync` or `catch_up` |
| `started_by_serve` | true when `serve` launched the run |
| `since` | ISO time it began waiting |

**Decision and constraint.** Runs record their own progress, into the
state database, on a timer — because runs live in short-lived
containers `serve` neither starts nor sees, and the history must be
complete while `serve` is down (§req:quality-attributes: nothing lost
while the dashboard is down). Liveness is a heartbeat because the
writer lock cannot be tested without taking it (a reader that briefly
took it would make a starting sync exit as busy), verify and validate
run without it, and a SIGKILL'd run cannot write its own end. One
database, not a separate history file (operator decision, 2026-11-12):
it already runs in WAL mode with readers beside a writer, `verify`
already writes to it without the lock, and the risks a second file
would avoid are hypothetical at one operator's scale; the history lives
in tables of its own, so trimming it never touches the records that
drive sync identity (§req:quality-attributes: bounded history).

**Alternatives rejected.** *Runs push progress to `serve`* — lost
whenever `serve` is down; breaks the requirement outright. *A progress
file per run* — the 30-day history needs queries, and readers race
partial writes. *A separate history database* — isolates writes no one
showed to conflict, at the cost of a second schema and connection path
(operator decision, 2026-11-12). *Parsing the ofelia log* — ofelia's log
is outside shakedown's volume and its format is not ours. *Liveness from
the writer lock* — untestable without taking it; misses verify.

**Tradeoffs.** A killed run shows as ended up to ~20 seconds late. A run
on the previous release (upgrade mid-run) does not heartbeat, so its
open record reads as ended unexpectedly while it still works; it closes
itself normally when done. `verify` and `validate` take no lock and never
migrate (§spec:state), so right after an upgrade they record no activity
until the first locked run has migrated the database. History starts
the day this release is installed.

**User-level test path.** Start a long sync of a large collection with
`serve` stopped; start `serve`: the dashboard shows the run with its
phase, counts climbing on a phone and a desktop without reload, the
current shows, bytes, speed, and — once enumeration ends — a time left.
Compare its counts with `status --json`'s `last_run`: they match. Kill
the sync container (`docker kill`): within ~20 seconds the run shows as
ended with reason "stopped reporting"; `status` reports it ended
unexpectedly; the next sync closes the record. Run `restage` and
`verify`: each appears with its kind and progress.

## Network activity §spec:network-activity

*Status: complete*

The dashboard shows, per source, download speed and requests in flight
now; download speed over the past 30 days; and every rate-limit hit,
server error, retry, and back-off with when it happened
(§req:success-criteria #29; §req:user-stories: is the source throttling
me?; §req:quality-attributes: source-neutral observability, nothing
lost while the dashboard is down, bounded history;
§req:priorities #34).

Observable behavior:

- **Measured by the core, the same for every source.** The core counts
  bytes by watching the files a fetch writes into the core-owned temp
  folder of each show it is fetching (§spec:source-plugins: plugins only
  write there). It counts requests in flight from the work it holds
  under the source's request budget (§spec:source-budget), plus a
  listing while it runs. It records network events from what it already
  sees: each lookup and fetch attempt's failure and the cause the plugin
  reports so the core can decide on retries (rate-limited, server error,
  timeout, connection error, refused), the `Retry-After` it honors, and
  its own retries and back-off waits. A new plugin that follows the
  authoring contract appears on the dashboard with no dashboard-specific
  work. The one request a plugin may retry itself — the collection
  listing (§spec:failure-behavior) — reports each of its own retries to
  the core; the IA plugin does.
- **"Bytes downloaded" means bytes fetched.** A run's
  `bytes_downloaded` — in its run record, `status`, and the dashboard —
  is the bytes this run fetched from the source, including a file
  fetched again after a failed checksum, and excluding files kept from
  an earlier interrupted attempt. The speed graph sums to the same
  total. Before this, the IA plugin counted kept files as downloaded;
  the core's measurement replaced that count.
- **Grouped by source block.** "Source" is a configured source block
  (`status`'s `source`), so two blocks of the same type show
  separately.
- **Recorded by the run, kept 30 days.** Runs record per-minute totals
  (bytes, peak requests in flight, rate-limit hits, server errors,
  timeouts and connection errors, retries, back-off seconds) and each
  event individually, in the same few-second writes as
  §spec:run-activity. A run that ends trims history older than 30 days.
  `serve` only reads.

API: `GET /api/activity`'s `network` lists, per source with a run in
progress, `{source, speed_bytes_per_second, requests_in_flight,
last_event}` (`last_event` as below, or null).

`GET /api/network?days=N` (unauthenticated; `N` 1–30, default 30;
outside that range `422`) answers `{generated_at, bucket_seconds,
sources: [...]}`. Each source: `{source, series: [{t, bytes,
peak_requests_in_flight, rate_limited, server_errors,
timeouts, connection_errors, retries, backoff_seconds}], events: [...],
events_total}`. `bucket_seconds` widens with the window so a series
keeps every minute for one day (60) and beyond that holds at most 720
points, using the narrowest of 120, 300, 600, 900, 1800, or 3600 (3600
for 30). `events` holds
the newest 500 in the window, newest first: `{at, kind, http_status,
collection, identifier, wait_seconds, detail}` where `kind` is
`rate_limited`, `server_error`, `timeout`, `connection_error`,
`refused`, `retry`, or `backoff`, and absent values are null. A source
with no activity in the window has an empty series.

**Decision and constraint.** Measure at the seam the core already owns —
temp folders, the request budget, and attempt outcomes — because the
plugins make their own HTTP requests through different libraries and
there is no single place every request passes (§req:quality-attributes:
source-neutral; §req:constraints: source-agnostic by design). Per-minute
totals plus individual events give "when it happened" to the minute and
keep 30 days to tens of thousands of small rows per source.

**Alternatives rejected.** *A core-supplied HTTP client every plugin
must use* — exact per-request counts, but rewrites both plugins and
forces the IA plugin off the `internetarchive` package
(§spec:source-plugins: "those problems are solved"). *Hooking HTTP
libraries process-wide* — sees only the libraries we patch, and breaks
silently when they change. *Plugins report bytes as they stream* —
dashboard-specific work for every plugin author, which the requirement
rules out. *Keep the plugin's own byte count* — counts kept files and
disagrees with the graph.

**Tradeoffs.** Speed is measured as bytes landing on disk, sampled
every second or two, so a file deleted between samples (a failed
checksum) can undercount by its partial size. "Requests in flight"
counts a show's fetch as one request; within a show the plugins fetch
files one at a time, so this matches the wire. Network failures inside a
plugin that the plugin swallows without reporting are invisible, as
they are to the core's retries today.

**User-level test path.** Stop `serve`. Run a sync against a source
answering some requests with `429` and `Retry-After` (the fake archive
in the test suite, or a throttled real run). Start `serve`: the 30-day
view shows that sync's speed curve, its rate-limit hits and back-off at
the minutes they happened, and the run's bytes equal the sum under its
curve and `status`'s `bytes_downloaded`. Add a third source block using
the test plugin: it appears with no dashboard change.

## Run history §spec:run-history

*Status: complete*

The dashboard lists the last 30 days of runs and, per collection, when
it last synced successfully (§req:success-criteria #30;
§req:user-stories: the morning check; operator decision 2026-11-12:
last good sync age).

Observable behavior:

- **Every run, one list.** Syncs, catch-ups, refetches, reconforms,
  restages, verifies, reconciles, trims, and purges of the last 30 days, newest first,
  each with kind, collection, start, duration, result, shows discovered,
  new, updated, and failed, bytes downloaded, its errors, and an
  undelivered failure notice. Runs that have a run record (sync,
  catch_up, refetch, reconform) take every figure from it — the record
  `status` reports — so the two always match. Runs before this release
  appear from their run records; restages, verifies, and reconciles
  appear from the release that records them on.
- **Last good sync.** Per collection, the end time of its latest sync or
  catch-up whose result is `completed` or `completed_with_item_issues`,
  or null. A collection whose schedule silently stopped firing shows
  its age growing; the dashboard shows the age, not a verdict.

API: `GET /api/runs?days=N` (unauthenticated; `N` 1–30, default 30;
otherwise `422`) answers `{generated_at, runs: [...]}`. Each run:
`{run_id, activity_id, kind, source, collection, started_at,
finished_at, duration_seconds, result, end_reason, items_discovered,
items_new, items_updated, items_failed, bytes_downloaded, errors,
failure_notice_error, detail_trimmed, runs_trimmed, bytes_freed}`;
`run_id` or `activity_id` is null when the run has none, and
`duration_seconds` and `finished_at` are null while it runs.
`detail_trimmed` is true once the run's error lines were dropped
(§spec:run-error-retention); `status` reports it on each `last_run` too.
A `trim` has null `source` and `collection`, its runs trimmed and bytes
freed in `runs_trimmed` and `bytes_freed` (null for other kinds), and
its compaction failure, if any, in `errors`. A `purge` has its bytes
freed in `bytes_freed` and the deletes that failed in `errors`. `last_successful_sync_at` is part of each collection in
§spec:library-stats.

**Decision and constraint.** Read sync figures from the run records
because the dashboard must match `status` exactly
(§req:quality-attributes: the dashboard tells the same truth); the
activity entries add the kinds that have no run record. Kind uses
§spec:run-activity's codes, so `catch_up` shows here as it does live.

**Alternatives rejected.** *Give restage, verify, and reconcile run
records* — changes what `status` calls the last run of a collection.
*Flag collections with no good sync in N days* — N has no objective
value; the age is the fact.

**Tradeoffs.** Pre-upgrade restages and verifies are missing from the
list; their outcomes still show through `status`'s recovery field.

**User-level test path.** After a night with two scheduled syncs, a
verify, and a failed catch-up, the list shows all four with matching
figures in `status --json`. Set one collection to manual only: its
last-good-sync age keeps growing day by day.

## Library, database, and disk §spec:library-stats

*Status: complete*

The dashboard shows, per collection and in total, what the mirror holds,
how much disk it uses and has left, how it grew over 30 days, and what
was mirrored most recently (§req:success-criteria #30;
§req:user-stories: how big is it all?, the morning check; operator
decisions 2026-11-12: recently added shows, no disk-full estimate;
§req:priorities #35).

Observable behavior:

- **Counts and bytes that match `status`.** Per collection: show counts
  by status (`status`'s `counts`), restricted shows (`status`'s
  `restricted`), and disappeared shows; archive bytes (`status`'s
  `bytes_on_disk`); and library bytes — the size of the files the
  library stages. Library files are hardlinks to archive files
  (§spec:system-shape), so library bytes use no disk of their own; the
  figure says how much of the archive the library presents. Totals sum
  the collections.
- **Stored figures.** Each show's archive bytes, library bytes, and
  whether it is kept twice (§spec:needs-attention library health) are
  computed from its recorded manifest whenever the show is written, and
  kept with it (§spec:state, schema v12). `status`, this view, and the
  growth snapshots sum them from an index and never read a manifest, so
  the answer costs the same on a cold page cache while a sync writes to
  the same pool. A show whose write carries no manifest keeps its
  recorded one and its figures with it. A reader of a database before
  v12 sums the manifests instead, as earlier releases did.
- **Disk.** Total and free bytes of the filesystem holding the archive,
  read from the operating system at the request — no walk of the tree.
- **Growth.** Per day for the last 30 days, per collection and in total:
  complete shows and archive bytes. Every run that holds the writer lock
  records a snapshot at its end — the only runs that change these
  figures — and the migration that installs this release records the
  first one. A day with no run carries the previous day's figures.
- **Recently mirrored.** The 20 shows most recently downloaded (new or
  re-fetched), newest first, with collection, identifier, the source's
  title when it lists one, and when.

API: `GET /api/library` (unauthenticated) answers `{generated_at, disk:
{total_bytes, free_bytes}, totals: {counts, restricted,
archive_bytes, library_bytes}, collections: [{source, collection,
counts, restricted, archive_bytes, library_bytes,
last_successful_sync_at}], growth: [{date, source, collection,
complete, archive_bytes}], recently_mirrored: [{source, collection,
identifier, title, downloaded_at}]}`. `counts` has `status`'s keys;
`restricted` is a count; `growth` has one row per collection per day,
and the page sums them for totals.

**Decision and constraint.** Every figure `status` also reports comes
from the same computation `status` uses (§req:success-criteria #30:
"matches it exactly"). Growth is snapshotted by the runs because only
runs change it and the history must be complete with `serve` down;
deriving it backwards from download dates cannot see prunes,
replacements, or removals. Free space from the filesystem because
nothing else knows it.

Byte figures are stored per show because summing every recorded
manifest read the whole state database — the manifests sit behind
each show's source metadata — and took about a minute on the
operator's NAS with a cold cache during a sync (v2.2.2). Per show,
because every write already passes through one place, so the figures
cannot drift from the manifests and need no end-of-run step.

**Alternatives rejected.** *Walking the archive tree for bytes* — tens
of seconds on a NAS per refresh (§spec:state notes the same for
`status`). *Running per-collection totals adjusted on each write* — a
missed adjustment drifts forever. *Totals recorded at the end of each
locked run* (like the growth snapshots) — stale while a sync runs, and
lost when a run is killed. *`serve` snapshotting growth on a timer* — misses days when
`serve` is down. *A "new shows only" list* — the record does not
distinguish a first download from a re-fetch; a re-fetched show is also
news to the morning check. *A disk-full estimate* (free bytes over
average daily growth) — the operator does not need the forecast
(operator decision, 2026-11-12); free space and the growth graph
answer "how big is it all?".

**Tradeoffs.** Growth starts at the upgrade; there is no backfill.

**User-level test path.** Compare every count and byte figure with
`status --json`: identical. Compare free space with `df` on the NAS.
Run a sync that adds shows: the next day's growth point rises by those
shows, and they lead the recently-mirrored list.

## Needs attention §spec:needs-attention

*Status: complete*

The dashboard has one list of what needs the operator, naming the
collection or show affected and the next available action, and a
library-health summary (§req:success-criteria #31; §req:user-stories:
the morning check, how big is it all?; §req:priorities #33).

Observable behavior:

- **One entry per collection per kind**, carrying the affected shows,
  so 300 failed shows are one entry, not 300. Kinds and the facts each
  carries:

  | `kind` | Source of truth | Carries |
  | --- | --- | --- |
  | `failed_shows` | shows in status `failed` | identifiers |
  | `recovery` | `status`'s `recovery`, unless it explains another entry | operation, outcome, phase, affected item/path, `safe_next_action` |
  | `ended_unexpectedly` | a run record or activity entry ended `stopped_reporting` in the last 30 days, not yet followed by a good run of that kind | run id, when |
  | `stale` | `status`'s `stale` | — |
  | `rescheduled` | `status`'s `rescheduled` | `since` |
  | `handoff_pending` | `status`'s `handoff_pending` | shows, since, last reason |
  | `failure_notice_undelivered` | latest run's `failure_notice_error` | the error |
  | `separations` | `status`'s `separations` | identifiers and folders |
  | `unplaced_files` | `status`'s `unplaced_files` | paths |
  | `unidentified_files` | `status`'s `unidentified_files` (§spec:unidentified-audio) | identifiers, `{identifier, file}` pairs |
  | `scheduler_stale` | the scheduler's heartbeat (§spec:schedules); one entry, about no collection | `since`: the last heartbeat |
  | `tick_overdue` | the collection's schedule and its run records (§spec:schedules) | `since`: the missed scheduled time |

  A good run is one whose result is `completed` or
  `completed_with_item_issues`, the same as a good sync in
  §spec:run-history. A catch-up counts as a sync. `ended_unexpectedly`
  carries `detail.runs: [{run_id, activity_id, kind, finished_at}]`, so
  runs without a run record (restage, verify, reconcile) are named too.
  `recovery` lists its `affected_item` in `identifiers`.

  A recovery that explains another entry rides on that entry as
  `detail.recovery` (the same object) instead of listing the same
  problem twice: a sync, refetch, or reconform that finished
  `completed_with_item_issues` explains `failed_shows`, and an operation
  still `in_progress` whose run ended `stopped_reporting` explains
  `ended_unexpectedly`. A recovery still `in_progress` while a run of
  that operation is still reporting is the run going, shown by the
  live view (§spec:run-activity), so it is no entry. Any other
  recovery is its own entry.

  Each entry has `since` when the source records one: when the problem
  began (the oldest skip, the first undelivered notice, the end of the
  failed operation or run). Failed shows, stale collections,
  separations, and unplaced files record no time, so their `since` is
  null. The next action
  for each kind is fixed by the kind (for example, failed shows are
  retried by the next sync; a pending handoff is resent on the next
  run), so the page words it; `recovery` carries `status`'s
  `safe_next_action` text.
- **Library health**, per collection: restricted shows with reason,
  disappeared shows with the day first found missing, drift found by
  the last `verify --deep` (file count), shows with no playable copy
  with reason (`status`'s `not_playable`), and shows kept as an
  unplayable original plus a playable copy (identifiers): complete shows
  whose recorded manifest keeps SHN or unrecognised audio archive-only
  beside a staged playable copy. A lossy copy left from older filters
  beside staged FLAC is not one of them. These are
  facts, not to-dos, so they sit apart from the attention list.
- **Matches `status`.** Each entry and health figure comes from the same
  computation `status` uses for the same thing.
- **Empty is a state.** With nothing to attend to, `entries` is empty and
  the page says so plainly.

API: `GET /api/attention` (unauthenticated) answers `{generated_at,
entries: [{kind, source, collection, since, identifiers, count,
detail}], health: [{source, collection, restricted: [{identifier,
reason}], restricted_count, removed_upstream: [{identifier,
removed_on}], removed_upstream_count, drift_files, not_playable:
[{identifier, reason}], not_playable_count, playable_copies:
[identifier], playable_copies_count}]}`. `detail` holds the kind's
extra facts under `status`'s own field names; `identifiers` is empty
for collection-wide kinds; `source` and `collection` are null for
`scheduler_stale`, which comes first. Counts are complete; every list carries its
first 10 shows or files, in order: `identifiers`, `detail.separations`,
`detail.unplaced_files`, `detail.unidentified_files`, and the health lists. An entry's `count` is
how many shows it affects (files, for `unplaced_files` and
`unidentified_files`); each health
list's `*_count` is how many shows it has. The page shows the first
few and how many more. `status --json` keeps its lists complete.

**Decision and constraint.** Group by collection and kind because a
long list of identical entries hides the one that matters
(§req:user-stories: "a ten-second glance"). Reuse `status`'s
computation because a disagreement is a defect
(§req:quality-attributes: the dashboard tells the same truth). The
action is a code the page words, not server text, so wording stays with
the screens (§spec:dashboard: codes, not copy).

Lists are capped because the answer carried every restricted show:
about 1 MB every 30 seconds per open page on the operator's mirror,
for a page that shows three (v2.2.2). Ten matches what `status`
prints.

**Alternatives rejected.** *One entry per show* — buries collection-wide
problems under show noise. *A full-list endpoint beside the capped
answer* — nothing reads it; `status --json` has the complete lists. *Health facts in the attention list* — a
restricted show needs nothing from the operator; mixing them makes the
list never empty.

**Tradeoffs.** `ended_unexpectedly` clears on the next good run of that
kind, not on acknowledgement — the dashboard cannot acknowledge, being
read-only.

**User-level test path.** Make one show fail, block a handoff target,
skip a collection's tick behind a long sync, and drop a stray file into
a library folder: the list shows four entries naming each collection
and show, matching `status`, the failed show's entry carrying the
sync's recovery. Resolve each: its entry leaves. Kill a sync: one
`ended_unexpectedly` entry carrying its unfinished recovery, gone after
the next good sync. With a healthy mirror, the list says nothing needs
attention.

## Release and health §spec:dashboard-health

*Status: complete*

The dashboard shows which shakedown is running, whether its config is
valid right now, and the last readiness validation
(§req:success-criteria #32; §req:priorities #36).

Observable behavior:

- **Release.** The release of the `serve` process answering.
- **Config now.** Whether `shakedown.yaml` loads at this request; when
  it does not, the config error naming the offending key
  (§spec:configuration), and the time of the last config this `serve`
  loaded successfully, which the other views are answering from
  (§spec:dashboard).
- **Last validation.** Per collection, the outcome and time of the
  latest readiness validation (`shakedown validate` or `POST
  /validate`), from the outcomes validation already records
  (§spec:recoverable-operation-reporting); null when none is recorded.

API: `GET /api/health` (unauthenticated) answers `{generated_at,
release, config: {valid, error, last_loaded_at}, validation: [{source,
collection, status, at}], scheduler: {state, last_seen, release}}` where
`status` is an outcome code and absent values are null; `scheduler` is
§spec:schedules's, not drawn until Design draws it.

**Decision and constraint.** Report the validation already recorded
rather than run one, because visiting the dashboard must not touch
sources or handoff targets (§req:success-criteria #33). The release is
`serve`'s own because that is the process the operator is looking at.

**Alternatives rejected.** *Run validation on page load* — makes network
requests to sources; forbidden. *Warn when the last run used another
release* — offered and declined (operator decision, 2026-11-12).

**Tradeoffs.** A validation that could not open the database recorded
nothing and does not show (§spec:recoverable-operation-reporting).

**User-level test path.** Upgrade the image: the release shown changes.
Introduce a typo in a config key: the page shows the error naming it,
and the other views keep showing data. Run `shakedown validate`: the
result and time appear per collection.

## Dashboard UI kit §spec:ui-kit

*Status: complete*

The visual kit, terms, and design authority for the read-only web
dashboard (§spec:dashboard). §spec:dashboard and the endpoint sections
fix behavior and data; this section fixes how the page looks and what it
calls things. Every screen is built only from the tokens and components
recorded here. A value the kit lacks is a kit change, made here first.

**Platform and track.** Web, track A (conventional): a familiar
admin-dashboard look from a standard component kit (§req:constraints: UI
line). Light and dark themes follow the device setting (operator
decision, 2026-11-12).

**Base system.** shadcn/ui (new-york style, components copied into
`web/src/components/ui/`) on Tailwind CSS 4, React 19, TypeScript, built
by Vite (operator decision, 2026-11-12). Charts are shadcn charts on
Recharts. Icons are lucide-react. The page is a static build: `npm run
build` in `web/` writes `web/dist/`, which the image's Node build stage
produces and `serve` serves. Node never runs in the runtime image.
Vite's `base: "./"` keeps every asset URL relative (§spec:dashboard:
works behind a proxy). Fonts are bundled from `@fontsource-variable`
packages; nothing loads from a third party (§spec:dashboard:
self-contained). Every web dependency is pinned to an exact version in
`web/package.json` and `web/package-lock.json`.

**Tokens.** CSS variables in `web/src/index.css`, mapped into Tailwind
through `@theme inline`; this file is the single source of truth.
Components use Tailwind utilities that resolve to these variables, never
literal colors, sizes, radii, shadows, or fonts.

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--background`, `--foreground` | shadcn neutral | shadcn neutral | page |
| `--card`, `--card-foreground` | shadcn neutral | shadcn neutral | cards |
| `--muted`, `--muted-foreground` | shadcn neutral | shadcn neutral | labels, secondary text, tracks |
| `--border`, `--input`, `--ring` | shadcn neutral | shadcn neutral | dividers, focus |
| `--primary`, `--primary-foreground` | `oklch(0.546 0.245 262.881)` on `oklch(0.985 0 0)` | `oklch(0.707 0.165 254.624)` on `oklch(0.145 0 0)` | progress, chart series 1, focus ring (`--ring` = primary) |
| `--destructive` | `oklch(0.505 0.213 27.518)` | `oklch(0.704 0.191 22.216)` | failed, lost contact, config error |
| `--success` (kit addition) | `oklch(0.448 0.119 151.328)` | `oklch(0.792 0.209 151.711)` | completed, healthy |
| `--warning` (kit addition) | `oklch(0.473 0.137 46.201)` | `oklch(0.828 0.189 84.429)` | no progress, delayed, old data |
| `--chart-1` … `--chart-5` | `--chart-1` = primary; shadcn 2–5 | shadcn | chart series |
| `--radius` | `0.5rem` | — | `rounded-sm/md/lg/xl` derive from it |
| `--density` | `0.225rem` (compact) | — | Tailwind `--spacing`: every `p-*`, `gap-*`, `m-*` step |
| `--font-sans`, `--font-mono` | Inter Variable; Geist Mono Variable (both bundled) | — | text; mono for show IDs only |

`--success` and `--warning` exist because a monitoring page needs "fine"
and "look at this" beside shadcn's "destructive". In the light theme the
three status tones sit one step darker than Tailwind's 600 shades so
12 px badge text on its tint passes WCAG AA (at least 4.5:1); checked
2026-11-12.

**Type scale.** Page title `text-xl` semibold. Figures (stat values)
`text-2xl` semibold, `tabular-nums`. Card titles `text-base` semibold.
Body `text-base`. Labels and secondary text `text-sm` muted-foreground.
Show IDs `font-mono text-xs`. Every number that updates in place uses
`tabular-nums` so it does not jitter.

**Spacing and radius.** Tailwind's spacing scale multiplied by
`--density`; radius steps from `--radius`. No arbitrary values
(`p-[13px]`).

**Target sizes.** 390×844 (phone), 768×1024 (tablet), 1440×900
(desktop). Every screen state renders at all three in both themes.

**Components.** From shadcn/ui: Card (with CardHeader, CardTitle,
CardDescription, CardAction, CardContent), Badge, Progress, Table,
Separator, Alert, Tooltip, Skeleton, Tabs, Chart. Kit additions:

- Badge variants `success`, `warning`, `danger`: tinted background, text
  in the tone. Used for run results and run states.
- Alert variant `warning`: page-wide notices (data is old, cannot reach
  Shakedown). `destructive` carries a broken config.
- Skeleton is the loading state of any card whose first answer has not
  arrived.
- CardTitle renders an `h2` (kit change), so screen readers can jump
  between cards. A figure inside a card is plain text, never a CardTitle.

**Usage rules.**

- Badge tones carry outcomes and states only: `success` for Completed,
  `warning` for No progress, delayed, or partly failed, `danger` for
  Failed and Lost contact. A run's phase uses `secondary` (neutral);
  it is not an outcome. Stopped is an outcome but uses `secondary` too:
  a run someone stopped is not a problem (operator decision,
  2026-11-16).
- Color never carries meaning alone; every badge and dot has its words.
- Sizes use decimal units: 1 GB = 10^9 bytes, 1 TB = 10^12 (operator
  decision, 2026-11-12), formatted by `web/src/lib/format.ts`.
- A chart has an `aria-label` naming what it plots and its window, and
  the current value it summarises is also on screen as text.
- Show IDs and other source text render as plain React text (never
  `dangerouslySetInnerHTML`), in `font-mono text-xs`.
- The page's content sits in one `main` landmark.
- Below 1024 px wide, a table of more than three columns becomes
  stacked rows (one list item per row, the key fields on two lines);
  from 1024 px it is a Table.
- Show IDs, paths, and server text wrap with `wrap-anywhere` (they
  break inside a word only when they must); they are never truncated,
  because a phone cannot hover to read the rest.
- A list shows its first few items and "and N more"; there are no
  expanders.

**Operator decisions on the screens (2026-11-12).**

- Running now lists only running runs. An ended run shows in Recent
  runs and on the Runs tab; a killed one also in Needs attention. The
  ended entries `/api/activity` keeps for ten minutes are not shown.
- **No-progress rule.** The page marks a running run with no progress
  for 5 minutes: when now minus `last_progress_at` exceeds 5 minutes, the
  run's row in Running now gets a `warning` Badge "No progress for
  \<time>" beside its phase. The API carries no verdict on progress
  (§spec:run-activity).
- **A run that stopped reporting** shows, as soon as `/api/attention`
  lists it, as a Needs attention entry "Lost contact with \<kind> at
  \<time>" with its next step, and in Recent runs and on the Runs tab with
  a `danger` Badge "Lost contact" (renamed from "Stopped reporting",
  operator decision, 2026-11-16, so that "Stopped" means one thing).
- **A run stopped on request** (§spec:graceful-stop) shows in Recent
  runs and on the Runs tab with a `secondary` Badge "Stopped". It is not
  in Needs attention.
- The page shows no disk-full estimate (operator decision); the API no
  longer computes one (§spec:library-stats).
- On desktop (1024 px and up) Running now sits left of Needs attention;
  below 1024 px Needs attention comes first.

The kit sheet (`web/src/KitSheet.tsx`, `?view=kit`) shows every token
and component state. The operator approved it on 2026-11-12 (round 2);
its goldens are `web/tests/__screenshots__/kit-<size>-<theme>.png`.
The representative screen the token sets were compared on was replaced
by the designed page.

**Direction.** Track A: no bespoke direction. Three token sets were
compared on one representative screen (round 1, 2026-11-12): A neutral
(black primary, 10 px radius, standard density, Geist), B blue (8 px,
compact, Inter), C teal (4 px, roomy, Inter). The operator chose B
(2026-11-12): more fits on a phone, and the blue accent marks progress
and the speed chart clearly. A was rejected because its black accent
barely separates progress from text; C because its roomy spacing makes
the phone page longest.

**Design authority.** No design sources existed before this kit
(design state none), so no conflicts were settled. Entries are added
here as `Design authority: <area> — <winner> — <reason> — <date>`.

### Terms

UI copy uses only these words for these things (one name per thing).
Spec and code names in the last column never appear on screen.

| Term | Meaning | Never call it | Replaces (internal) |
| --- | --- | --- | --- |
| Shakedown | The app this page belongs to. | the server, serve | `serve`, shakedown |
| Show | One concert recording as the source lists it. | item, recording, entry | item |
| Show ID | The source's identifier for a show. | item ID | `identifier` |
| Collection | A set of shows Shakedown mirrors from one source under one name. | library, set | collection |
| Source | A site Shakedown mirrors from, as named in the config. | plugin, provider | source block, plugin |
| Archive | Shakedown's own copy of every downloaded file. | mirror, store, disk | archive tree, `archive_bytes`, `bytes_on_disk` |
| Library | The folders your music tools read. Its files are links into the archive and take no extra space. | staging, archive | library tree, `library_bytes` |
| Free space | Space left on the drive that holds the archive. | available, disk free | `disk.free_bytes` |
| Run | One piece of work Shakedown does on one collection. | job, task, activity | activity entry, run record |
| Sync | A run that checks the source and downloads new and changed shows. | update, refresh | `sync` |
| Catch-up sync | A sync that waited for another run to finish, then ran. | retry, rescheduled sync | `catch_up` |
| Refetch | A run that downloads one show again. | redownload | `refetch` |
| Reconform | A run that puts one show's files back the way Shakedown recorded them. | repair, reset | `reconform` |
| Restage | A run that rebuilds the library folders from the archive. | rebuild, relink | `restage` |
| Verify | A run that checks archive files against what Shakedown recorded. | scan, audit | `verify` |
| Reconcile | A run that rebuilds Shakedown's records from the archive. | rescan, recover | `reconcile` |
| Validation | A check that the config, sources, and handoff targets work, run with `shakedown validate`. | test, preflight | readiness validation |
| Release | The Shakedown version answering this page. | version, build | `release` |
| Running | A run that is working and reporting. | active, in progress | `state: running` |
| Waiting | A collection whose run starts when a slot frees in the current sync. | queued, pending, scheduled | `waiting`, waiting entry |
| No progress for *time* | A running run whose counts have not moved for that long. | stuck, stalled, hung | `last_progress_at` |
| Lost contact | A run that stopped sending updates: killed, crashed, or the host went down. | stopped reporting, crashed, ended unexpectedly | `end_reason: stopped_reporting`, ended unexpectedly |
| Stopped | A run someone stopped before it finished (`docker stop`, Ctrl-C); the next run continues where it left off. | interrupted, cancelled, aborted | `stopped`, stopped on request |
| Completed | The run finished and every show it worked on succeeded. | done, succeeded, OK | `completed` |
| Completed, some shows failed | The run finished; some shows failed and the next sync retries them. | partial, partly failed | `completed_with_item_issues` |
| Failed | The run ended before it finished. | error, aborted | `failed_before_completion` |
| Listing incomplete | The source's list of shows did not load, so nothing was removed. | stale | `stale_enumeration`, stale |
| Estimating | A total or time left that is not known yet. | unknown, calculating, — | null `items_to_fetch`, `bytes_to_fetch`, `eta_seconds` |
| Time left | Estimated time until the run finishes. | ETA | `eta_seconds` |
| Mirrored | A show whose files are all in the archive. | complete, downloaded, done | `complete` |
| Pending | A show waiting to be downloaded. | queued, to do | `pending` |
| Failed (show) | A show whose download failed; the next sync retries it. | errored, broken | `failed` |
| Restricted | A show the source does not let Shakedown download. | unavailable, blocked, locked | `unavailable`, `restricted` |
| Disappeared | A show the source no longer lists. Shakedown keeps its copy. | removed, deleted, gone | `disappeared`, removed upstream |
| Needs attention | Things waiting on the operator, one entry per collection and kind. | issues, alerts, problems | attention entries |
| Library health | Facts about the library that need no action. | warnings | health |
| Handoff notice | The message that tells your music tools new shows arrived. | notification, webhook, ping | handoff |
| Failure notice | The message Shakedown sends when a run fails. | alert, error email | failure notice |
| Requests in flight | Requests to a source that are open right now. | connections, active requests | `requests_in_flight` |
| Rate limited | The source asked Shakedown to slow down (HTTP 429). | throttled, blocked | `rate_limited` |
| Server error | The source answered with an error (HTTP 5xx). | failure, outage | `server_error` |
| Back-off | A wait Shakedown adds before trying again. | pause, delay, cooldown | `backoff`, `Retry-After` |
| Retry | Another attempt at a request that failed. | redo | `retry` |
| Drift | Archive files that changed after Shakedown recorded them. | corruption, mismatch | drift, `drift_files` |
| Phases | Starting · Listing · Discovering · Downloading · Tagging · Updating library · Sending notices · Verifying · Finishing | — | `phase` codes; `staging` → Updating library, `notifying` → Sending notices |
| Timed out | A request got no answer in time. | slow, hung | `timeout` |
| Connection failed | Shakedown could not connect to the source. | offline, network error | `connection_error` |
| Refused | The source refused the request. | denied, blocked | `refused` |
| Passed | The last validation found no problems. | OK, valid, green | validation `completed` |
| Passed with problems | The last validation finished but found problems. | warnings, partial | validation `completed_with_item_issues` |
| Did not finish | The last validation stopped before it finished. | interrupted, aborted | validation `in_progress` |
| Never validated | No validation is recorded for the collection. | unknown, untested | validation `status: null` |
| No playable copy | A show with no audio file a player can read. | broken, unplayable | `not_playable` |
| Original plus a playable copy | A show kept as its unplayable original beside a staged playable copy. | duplicate, converted | `playable_copies` |
| Last completed sync | The end of a collection's latest sync that finished Completed or Completed, some shows failed. | last good sync, last success | `last_successful_sync_at` |
| Valid (config) | shakedown.yaml loads right now. | OK, fine, good | `health.config.valid: true` |
| Has an error (config) | shakedown.yaml does not load right now; the error names the key. | broken, invalid, failed | `health.config.valid: false` |
| Next step | The action `status` suggests for an unfinished or failed operation, shown as the server words it. | fix, action, hint | `safe_next_action` |
| Updated (shows) | Shows a run fetched again because they changed upstream. | changed, refreshed | `items_updated` |
| Updated \<time> | When the newest data on screen was computed. | refreshed, synced, as of | `generated_at` |

"Updated" carries two meanings, kept on purpose (operator decision,
2026-11-12): as a count of shows on Runs and Overview, and as the data
time in the header. No other term may take a second meaning.

### Screens

Each approved screen lists its handoff spec, its goldens
(`web/tests/__screenshots__/<scenario>-<tab>-<size>-<theme>.png`, from
`web/tests/screens.spec.ts`), and the round it was approved in. The page
is one route (`GET /`) with four tabs.

| Screen | Handoff spec | Golden scenarios | Approved |
| --- | --- | --- | --- |
| Page shell and page-wide states | `docs/ui/handoff/shell.md` | unreachable, config-broken, no-config, loading; never-reached | round 2, 2026-11-12; never-reached 2026-11-13 |
| Overview tab | `docs/ui/handoff/overview.md` | default, quiet, first-install, long-content, waiting | round 2, 2026-11-12; waiting rows 2026-11-20; unidentified files row 2026-11-23 |
| Runs tab | `docs/ui/handoff/runs.md` | default, first-install, long-content; loading | round 1, 2026-11-12; loading 2026-11-13 |
| Sources tab | `docs/ui/handoff/sources.md` | default, first-install, long-content; loading | round 1, 2026-11-12; loading 2026-11-13 |
| Library tab | `docs/ui/handoff/library.md` | default, first-install, long-content, loading | round 1, 2026-11-12 |

### Harness

- `npm run render -- <outDir> [sets] [view]` in `web/` builds the page
  and captures every size × theme with headless Chromium (Playwright,
  same version as the mise-managed one) into a review folder outside
  git.
- `npm run test:screens` runs the Playwright screenshot tests
  (`web/tests/*.spec.ts`, config `web/playwright.config.ts`): one project
  per size × theme, `maxDiffPixels: 0`, `threshold: 0.05`, animations and
  caret off, UTC clock, `en-US` locale. Behaviour tests with no
  screenshot (`freshness.spec.ts`, `behaviour.spec.ts`) run in one
  project only. Baselines are committed only for
  approved screens and the approved kit sheet. A font or renderer
  difference is a finding, never a reason to loosen the threshold.
  Every baseline was rendered on Linux x86-64 (Rocky Linux 9.8) with
  Playwright 1.65.0's Chromium and the bundled fonts. The screenshot
  tests run on a developer's machine before a UI change merges, not in
  CI (operator decision, 2026-11-12): CI checks types and the build
  only.
- Fixtures (`web/src/fixtures/`) hold answers in the exact shape of the
  `/api/*` endpoints (`web/src/api/types.ts`), with a fixed clock.
  `web/src/fixtures/scenarios.ts` names each scenario and how the server
  behaves in it (answers, 503 with the config error, stops answering, or
  hangs). `web/scripts/fixture-page.mjs` serves a scenario to the real
  page by intercepting its `./api/*` requests, fixes the clock, and holds
  the answers until the bundled fonts load, so chart labels measure the
  same every time.
- Handoff specs live in `docs/ui/handoff/`, one per screen, from
  `docs/ui/handoff/_template.md`.

## Dashboard page delivery §spec:dashboard-page

*Status: complete*

How the approved page (§spec:ui-kit) gets from `web/` to the operator's
browser: built into the image, served by `serve` at its root, and
checked in CI (§req:constraints: UI line, the dashboard lives in serve,
no Node at runtime; §req:success-criteria #28–#33; §req:quality-attributes:
phone and desktop alike).

Observable behavior:

- **`serve` answers the page at `/`.** `GET /` returns the built
  `index.html`; its scripts, styles, and fonts are served under the same
  root from the build's `assets/` folder. The page's own requests
  (`./api/*`) and the existing routes (`/api/*`, `/healthz`, `/status`,
  `/metrics`, the mutating endpoints) keep precedence: the page's files
  are matched last. Every page file is a `GET` with no authentication,
  like `GET /status` (§spec:dashboard). `index.html` is sent with
  `Cache-Control: no-cache` so an upgrade shows the new page at once;
  the content-hashed files under `assets/` may be cached.
- **Works under a path prefix.** The build addresses every asset and
  endpoint relatively (Vite `base: "./"`), so the page works at
  `http://host:8080/` and behind a proxy at `/shakedown/` with no
  setting (§spec:dashboard).
- **Built into the image, never at runtime.** The Dockerfile gains a
  Node build stage (Node 26, `npm ci` from `web/package-lock.json`, then
  `npm run build`). Its `web/dist/` output is copied into the Python
  package (`shakedown/dashboard_web/`) before the package is installed,
  so it ships as package data and `serve` finds it with
  `importlib.resources`. The runtime image contains no Node.
- **A source checkout without a build still serves the API.** When
  `dashboard_web/` holds no `index.html`, `GET /` answers `404` and every
  other route works as before; tests and development runs of `serve`
  need no Node.
- **CI checks the page builds.** A `web` job runs `npm ci`, `npm run
  typecheck`, and `npm run build`; the image build depends on it like
  it depends on the Python tests. The screenshot tests are not part of
  CI: they run on a developer's machine before a UI change merges
  (operator decision, 2026-11-12; §spec:ui-kit Harness).

**Decision and constraint.** Package data, not a separate directory in
the image, because `serve` then finds the page the same way in the image
and in any installed wheel, with no path setting. A static mount matched
after the API routes, because the page is one route with hash tabs
(`#runs`) and needs no server-side routing. `no-cache` on `index.html`
because a stale page against a newer API would draw wrong figures after
an upgrade.

**Alternatives rejected.** *Serving from a path set in config* — one more
setting for every operator, for a location only the image decides.
*Building the page when `serve` starts* — puts Node in the runtime
image, which §req:constraints rules out. *A catch-all route returning
`index.html` for every unknown path* — the page has one route, and a
catch-all would answer API typos with HTML.

**Tradeoffs.** A `serve` started from a source checkout shows no page
until `npm run build` has run and its output is copied in.

**User-level test path.** Build the image and run `serve`: `GET /`
returns the page, and the browser's network panel shows only requests to
the same host. Put it behind a proxy under `/shakedown/`: the page loads
and polls `./api/*` there. `GET /status` and `GET /api/health` answer as
before. Upgrade the image with the page open and reload: the new page
loads.

## Settings in the dashboard §spec:dashboard-settings

*Status: in progress*
The API is built: `GET /api/config` (with `unconfigured`, §spec:collection-removal), `PUT /api/config`, and `POST /api/config/check-paths`; the Settings screens wait on the Design task.

Compatibility, from deployment status "deployed, one install, no
outside consumers" (§req:constraints, operator-confirmed 2026-11-24):
config file shape — free; compose layout — free; HTTP API — free; CLI —
free; dashboard pages and their JSON — free; state database — additive
migration only; archived media — frozen (an upgrade never forces a
re-download). This line governs every section of the config-management
increment: §spec:dashboard-settings, §spec:edit-lock, §spec:schedules,
§spec:dashboard-credentials, §spec:first-run-setup,
§spec:dashboard-run-controls, §spec:collection-removal, and
§spec:run-error-retention.

The dashboard reads and writes `shakedown.yaml`. Every setting the file
holds — roots, concurrency caps, sources with their request ceiling,
collections with every option and their schedule, the timezone, and
failure notifications — can be viewed and changed from a phone or a
desktop. A save is checked by the same rules the next run applies, and
the file stays the one source of truth whoever edits it
(§req:success-criteria #2, #36, #37; §req:quality-attributes: the config
file stays the truth, a save tells the truth; §req:constraints: the YAML
file stays the source of truth, config management in the dashboard,
path checks warn, never block).

Observable behavior:

- **One config resource.** `GET /api/config` answers what the file says
  now, read at the request (a hand edit appears on the next refresh):
  - `state`: `missing` (no file; §spec:first-run-setup), `unreadable`
    (the file is not valid YAML, or cannot be read), `invalid` (it
    parses but a run would refuse it), or `valid`;
  - `path`: the config file's path inside the container;
  - `revision`: an opaque token for the exact file content, or `null`
    when there is no file;
  - `edit`: `editable`, `locked`, or `override` (§spec:edit-lock);
  - `document`: the file's settings as JSON, keyed exactly as the YAML
    is (`archive_root`, `sources[].collections[].library_layout`, …),
    including keys the file leaves out at their effective default, or
    `null` when the file is missing or unreadable;
  - `errors`: for an `invalid` or `unreadable` file, a list of
    `{loc, code, message}`, where `loc` is the path to the offending
    setting as a list of keys and indexes
    (`["sources", 0, "collections", 1, "query"]`) and `message` is the
    developer-facing error text the run would log (it never echoes a
    value from the file, §spec:configuration);
  - `credentials`: per source, `environment`, `dashboard`, or `none`
    (§spec:dashboard-credentials) — never a credential value;
  - `next_syncs`: per collection, `{source, collection, next_at}` with
    `next_at` the next scheduled sync, formatted like every other time
    the dashboard receives, or `null` for a manual-only collection
    (§spec:schedules);
  - `source_types`: per installed source plugin, its `type`, the layout
    fields and filters its `library_layout` accepts, and whether it
    takes credentials — so the editor can explain the options without
    hard-coding them;
  - `unconfigured`: collections that still have shows or files but are
    no longer in the file (§spec:collection-removal);
  - `generated_at`, like every dashboard answer.
  Opening it never writes anything (§req:success-criteria #33).
- **Save is a whole-document replace, checked first.** `PUT /api/config`
  takes `{revision, document}`. The document is checked by the same
  validation every run applies at startup — unknown keys, types and
  ranges, duplicate collection names, an unknown source type, a layout
  with an unknown field or filter or none at all, plus the schedule and
  timezone rules of §spec:schedules. Answers:
  - `200 {revision, warnings}` — written; `warnings` lists, each
    `{loc, code}`: the path-check results (below) with `path_moved`, and
    `layout_not_unique` for a layout the run also only warns about
    (§spec:layout-collision-safety);
  - `422 {errors}` — refused, same `{loc, code, message}` shape as
    above, every error found, not only the first; the file is untouched;
  - `409 {code: "changed"}` — the file's revision is no longer the one
    the editor loaded (someone edited it since); nothing is written and
    the editor reloads;
  - `409 {code: "locked"}` — the edit lock is on (§spec:edit-lock);
  - `409 {code: "busy"}` — the save points `state_db` at another
    database while a run holds the writer lock: that run would keep the
    old database's lock while the next took the new one's, two writers
    on one archive. Nothing is written; save again once the run ends;
  - `403 {code: "cross_site"}` — the write gate refused the request
    (§spec:dashboard-run-controls);
  - `500 {code: "write_failed", path}` — the file or its folder cannot
    be written (for example, a read-only mount); the file is untouched;
  - `409 {code: "missing"}` or `409 {code: "unreadable"}` — there is no
    file, or it is not valid YAML: no editor exists for either (below),
    and the override answers the same.
  A document a save accepts is one the next run accepts, and the reverse
  (§req:quality-attributes: a save tells the truth): both paths call one
  validation, never two copies of it.
  Codes: an error's `code` is pydantic's own error type for a type,
  range, or unknown-key rule (`extra_forbidden`, `missing`,
  `greater_than_equal`, …), and for the rules of our own:
  `schedule_invalid`, `timezone_unknown`, `duplicate_name`,
  `source_type_unknown`, `layout_unknown_filter`,
  `layout_unknown_field`, `layout_no_field`, and `exec_file_only`. An
  unreadable file has one error at `loc: []`: `yaml_invalid`,
  `root_not_mapping`, or `read_failed`. Path-check warnings are
  `not_writable` (at the setting whose folder fails) and
  `hardlink_failed` (at `library_root`).
- **Paths warn, never block.** When a save changes `archive_root`,
  `library_root`, or `state_db`, the system runs the path checks of
  §spec:setup-readiness-validation on the new values — each folder
  writable, a real test hardlink from archive to library — and returns
  each failure as a warning with that check's code; the save still goes
  through. A changed path also returns the warning `path_moved`:
  Shakedown moves nothing, so a run against a new, empty archive or state
  database downloads again what the old one held. `POST
  /api/config/check-paths` with `{archive_root, library_root, state_db}`
  (`state_db` optional, defaulting beside the archive) runs the same
  checks without saving and answers `200 {warnings}`, for an editor or
  setup that wants the answer before Save. Runs still refuse to start on a path that
  fails (§spec:system-shape).
- **Hand edits survive.** Writing changes only the settings that
  changed. Comments, key order, quoting, and blank lines elsewhere in
  the file are kept, so a hand-written or tool-deployed file stays
  readable after a dashboard edit. The new content replaces the file
  atomically — a reader, a run, or a crash never sees a half-written
  file — and keeps its permissions.
- **Collection and source names are fixed once created.** The editor
  sets a name when it adds a source or collection and never renames one:
  a collection's archived shows live under its source and collection
  names (§spec:system-shape), so a rename would leave them behind and
  download them again. To rename, a user removes the collection and adds
  a new one, and §spec:collection-removal says what happens to the
  files. A rename by hand in the file behaves as it always has.
- **Takes effect at the next run.** A save changes nothing in a run
  already going; every run reads the file when it starts
  (§spec:api-sync-runs), and schedules apply from the next tick
  (§spec:schedules). Nothing restarts.
- **The command handoff stays in the file.** The dashboard shows a
  collection's `on_complete: exec` command but cannot set, change, or
  remove it, and a save whose document adds or changes an `exec` value
  is refused with code `exec_file_only`. While any `exec` command uses
  `{staging_root}`, `library_root` stays in the file too: a save that
  changes it is refused with `exec_file_only` on `library_root`, because
  a command that hands the path to a shell would run whatever the
  dashboard saved as the path (operator decision, 2026-11-24). Webhook
  handoffs and the failure-notification webhook are editable (operator
  decision, 2026-11-24).
- **A broken file.** An `invalid` file is shown in the editor with its
  errors against the settings they name, and a save that fixes them
  succeeds. An `unreadable` file (bad YAML) has no editor: the dashboard
  shows the error, and the file is fixed by hand.

**Decision and constraint.** The dashboard edits the YAML file itself,
because §req:constraints keeps the file the source of truth so
infrastructure-as-code deployments keep working, and every run already
reads it at start. The API carries the file's own structure as JSON
(boring edge: the shape an operator already knows from the example
file), and a save replaces the whole document under a revision token
— optimistic concurrency, the standard answer to "never silently
overwrite an edit made since I opened the page"
(§req:quality-attributes: the config file stays the truth). The save
runs the run's own validation because §req:success-criteria #36 demands
that save and run agree, and two validators drift. Comments are kept
because the file is also edited by hand, and a dashboard save that
stripped a user's notes would be a silent loss. The command handoff is
file-only because the dashboard has no login (§req:constraints: no
login of its own) and a command runs any program on the NAS: with it
off the page, a proxy mistake can change settings but cannot run
programs (operator decision, 2026-11-24). Names are fixed because the
archive is keyed by them and §req:constraints forbids an upgrade or edit
path that re-downloads the archive.

**Alternatives rejected.** *Keep settings in the state database and
generate the file* — breaks hand edits and IaC, which §req:constraints
keeps. *Per-setting endpoints (PATCH a collection)* — more surface and
more partial-failure cases for no user-visible gain; one validated
document is how the run sees it anyway. *Rewrite the file cleanly from
the model* — simpler, but loses every comment in a hand-written file.
*Block saves whose paths fail the check* — §req:constraints says warn,
never block: the operator stays in charge of their storage. *Make the
command handoff editable* — every option in the browser, as
criterion #37 first read, at the cost of turning a proxy mistake into running any
program on the NAS; the operator chose webhooks only. *Rename in place
by moving the archive folder* — a move, a database rewrite, and a
library restage behind one text field; not needed by any requirement.

**Tradeoffs.** A save conflict makes the user reload and redo their
change when the file moved under them. Comment preservation adds a
YAML round-trip dependency. Users who want a command handoff edit the
file. Renaming a collection costs a remove and an add.

**User-level test path.** Open Settings on a phone; change a
collection's layout to `Shows/All`, with no source field: Save is
refused with the layout error naming that setting, and the file is
unchanged. A layout with no identifier field, such as `{year}/{venue}`,
saves with the `layout_not_unique` warning, as the run warns. Fix it and save: `shakedown.yaml` holds the new layout, its
comments are still there, and the next sync stages under it. Edit the
file by hand with Settings open and save again: the save is refused as
changed. Point `library_root` at a folder on another filesystem: the
save goes through with the hardlink warning and the path-moved warning.
Add `exec:` to a collection's handoff through the API: refused with
`exec_file_only`.

## Edit lock §spec:edit-lock

*Status: in progress*
The edit state, `POST /api/config/override`, the locked save, and setup's new `editable` file (`POST /api/setup`) are built; the dashboard's read-only view and override confirmation wait on the Design task.

A `shakedown.yaml` that Shakedown's setup wrote is editable from the
dashboard from the start. A file that already existed — written by hand
or deployed from git or an infrastructure-as-code tool — starts locked:
the dashboard shows every setting but cannot change the file until the
operator turns on an override (§req:success-criteria #39;
§req:constraints: edit lock; §req:user-stories: keep my
infrastructure-as-code honest).

Observable behavior:

- **Three states.** `GET /api/config` reports `edit`:
  - `editable` — §spec:first-run-setup wrote this file;
  - `locked` — the file came from outside and no override is on;
  - `override` — the operator turned the override on.
- **Locked means read-only for the file.** While `locked`,
  `PUT /api/config` answers `409 {code: "locked"}` and the dashboard
  shows the settings without editing controls. What the file does not
  hold stays available: credentials (§spec:dashboard-credentials),
  Sync now and Validate (§spec:dashboard-run-controls), and deleting the
  files of unconfigured collections (§spec:collection-removal).
- **Turning on the override.** `POST /api/config/override` sets the
  state to `override` and answers `200 {edit: "override"}`; it is
  idempotent. The dashboard confirms first, warning that a later edit to
  the file from its source (a hand edit, a git deploy) replaces changes
  made in the dashboard. The override stays on; the dashboard offers no
  way back to `locked`.
- **Hand edits keep working in every state.** A hand edit to an
  `editable` or `override` file never locks it, and the next run and the
  next page refresh use it.
- **Where the state lives.** The edit state is recorded in the state
  database the config names, beside the credentials. A file with no
  record there is `locked`. When a dashboard save points `state_db` at a
  new database, the edit state and stored credentials move with it,
  once the file is written; the same database named another way (a
  link, a relative path) moves nothing. The old database's writer lock
  is then marked retired: a run that started before the save, and so
  read the old file, stops as busy rather than writing beside a run on
  the new database; one started after it may take the lock.
  Pointing the file at a different database by hand — or rebuilding the
  database with `reconcile` — starts it `locked` again; the operator
  turns the override back on.

**Decision and constraint.** Origin decides the lock, recorded by
Shakedown, because §req:constraints asks exactly that: Shakedown cannot
know whether a pre-existing file will be maintained from its source or
from the dashboard from now on, so it starts safe and lets the operator
say. The record lives in the state database because Shakedown alone
writes it, it already holds per-install state, and the operator chose
it for credentials (§spec:dashboard-credentials).

**Alternatives rejected.** *A marker key in the file* — an IaC copy of
a setup-written file would carry the marker and unlock itself on every
host, and IaC users would have to manage a key that means nothing to
the run. *A content hash, locking on any outside change* — locks the
file after every hand edit, while §req:success-criteria #39 keeps hand
edits working. *A lock-off action* — not required; the operator chose
an override that stays on.

**Tradeoffs.** A rebuilt or relocated database relocks the file, which
costs one more click. An override user whose IaC redeploys the file
loses dashboard changes, as the warning says.

**User-level test path.** Start `serve` with a hand-written file:
Settings shows every value with no Save, and `PUT /api/config` answers
`locked`. Turn on the override: the warning shows, Save appears, and a
change saves. Restart `serve`: still `override`. On a fresh stack, finish
setup: Settings is editable without any override.

## Schedules and the scheduler §spec:schedules

*Status: in progress*
The `schedule` and `timezone` keys, the scheduler with its heartbeat and `--check`, and serve's reporting (health, `/status`, Needs attention, the stale notice) are built; the dashboard schedule editor and preset read-back wait on Design, and `next_syncs` in `GET /api/config` on the config resource.

Each collection's schedule lives in `shakedown.yaml` and is set from the
dashboard. A long-running Shakedown scheduler, in its own container
beside `serve`, starts each scheduled sync; the compose stack carries no
third-party scheduler. A scheduler that stops is never silent: the
dashboard, `GET /status`, and the failure notification say so
(§req:success-criteria #1, #6, #38; §req:constraints: one-shot CLI,
external scheduler — superseded for scheduling, schedule input;
§req:quality-attributes: sane by default, schedules cost nothing while
idle, scheduled runs are observable and self-contained). This
supersedes the "no `schedule:` field" rule of §spec:configuration and
ofelia's role in §spec:deployment.

Observable behavior:

- **In the file.** A collection's `schedule` is a standard five-field
  cron expression (minute, hour, day of month, month, day of week). A
  collection with no `schedule` is manual only: it syncs only when
  someone starts it. A top-level `timezone` (an IANA name such as
  `Europe/Berlin`) says which clock schedules follow; without it they
  follow the container's local time (`TZ`, UTC by default). An
  expression that does not parse or never fires, or a timezone that
  does not exist,
  fails validation like any other bad key (§spec:configuration), so a
  save refuses it (§spec:dashboard-settings).
- **Presets and cron.** The dashboard offers Daily at a time, Weekly on
  a day at a time, Manual only, and a cron field. A preset is written as
  the cron expression it stands for (`30 3 * * *`, `0 4 * * 0`); Manual
  only removes the key. An expression of exactly a preset's form reads
  back as that preset; any other reads back as cron.
- **Staggered default (provisional).** A collection added from the
  dashboard or setup defaults to Weekly on Sunday at the first whole
  hour from 03:00 at which no other collection's schedule starts, so
  collections do not all start at once. The user may change it before
  saving. (Provisional default; the operator may change the cadence or
  start hour.)
- **Next sync.** `GET /api/config` carries each collection's next
  scheduled sync (`next_syncs`, §spec:dashboard-settings), computed from
  the file in the schedule's timezone by the same rules the scheduler
  uses.
- **The scheduler.** `shakedown schedule` is a long-running command
  that does nothing but start scheduled syncs. It listens on no port,
  reads the config file without writing it, and runs as its own
  container (`shakedown-scheduler`, §spec:deployment), so a crash, hang,
  restart, or update of `serve` never stops a tick.
- **A tick is a scheduled sync.** At each scheduled time, the scheduler
  starts the collection's sync as a fresh one-shot run that reads the
  file at start — the same `sync --collection` run a scheduler tick has
  always launched. When another run holds the writer lock, the tick is
  rescheduled and caught up exactly as §spec:rescheduled-syncs describes
  for a scheduler tick, never refused like an API trigger. Collections
  due in the same minute start in file order; the later ones find the
  lock held and are rescheduled. When a run the scheduler started dies
  without closing its run record, the scheduler closes it, as `serve`
  does for the runs it starts (§spec:api-sync-runs).
- **Changes apply at the next tick.** The scheduler notices a changed
  file — saved from the dashboard or edited by hand — within a minute
  and plans the next ticks from it, with no restart. A run already going
  is untouched. While the file is invalid, the scheduler keeps the
  schedules of the last valid file it read; a tick that fires then starts
  its run, which fails on the config error like any run on a broken
  file. That run exits before it opens the state database, so it leaves
  no run record; the failure shows as the config error in health
  (§spec:dashboard-health), in the scheduler's log, and, ten minutes on,
  as the collection's `tick_overdue` entry (operator decision,
  2026-11-24). A scheduler that starts on an invalid
  file has no schedules until the file is fixed, and reports that in its
  heartbeat.
- **Fails loudly, restarts itself.** Any fault in the scheduler's own
  planning ends the process with a non-zero exit, so the container's
  restart policy starts it again; it never keeps running with its
  planning stopped.
- **Heartbeat.** About once a minute the scheduler records a heartbeat
  in the state database: the time, its release, whether it holds a
  valid schedule, and the next tick it plans. The heartbeat is one
  record, replaced each time, never a history, so it does not grow.
  Recording never takes the writer lock and never migrates, like a
  rescheduled-sync entry (§spec:rescheduled-syncs); unlike that entry,
  the record sits outside schema versioning (§spec:state), so the
  scheduler can record it into a database a previous release left.
  `shakedown schedule --check` exits non-zero
  when the heartbeat is older than three minutes or missing; the compose
  example uses it as the scheduler container's health check, so Container
  Station shows the container unhealthy.
- **Nobody has to watch for it.** `serve`, independently of the
  scheduler, reads the heartbeat and the run records:
  - `GET /api/health` and `GET /status` carry `scheduler: {state,
    last_seen, release}`, `state` one of `running`, `stale` (heartbeat
    older than three minutes), `no_schedule` (running, but the file it
    holds has no valid schedule), or `never` (no heartbeat recorded), and
    `last_seen` formatted like every other time (§spec:dashboard-health);
  - Needs attention (§spec:needs-attention) gains `scheduler_stale`, one
    entry while the scheduler is `stale` or `never` and any collection
    has a schedule, about no collection (`source` and `collection`
    null, `since` the last heartbeat), and `tick_overdue`, one per
    collection whose latest scheduled time passed more than ten minutes
    ago with nothing covering it, `since` that time. A sync or catch-up
    of the collection covers the tick when it started at or after it or
    was still running at it, and so does any rescheduled entry for the
    collection: an entry keeps its oldest skip, so a tick skipped behind
    a days-long run leaves no newer trace (operator decision,
    2026-11-24). `scheduler_stale` clears when the scheduler is back;
    `tick_overdue` clears when the collection's next run starts;
  - when the scheduler turns `stale`, `serve` sends the configured
    failure notification (`notifications.on_failure`,
    §spec:handoff) once for that episode, naming the scheduler and its
    last heartbeat: the shared envelope with `event: scheduler.stale`,
    `source`, `collection`, `staging_root`, and `run` null,
    `scheduler: {last_seen, release}`, and one `errors` line
    ([docs/handoff.md](docs/handoff.md)). The episode is the heartbeat
    time the scheduler went stale on; it is recorded, so a restarted
    `serve` never sends it twice, and an undelivered notice is not
    retried.
  - `GET /status` answers `{scheduler, collections}`, `collections`
    being the `status --json` list, since a list cannot carry the
    field.
  An outside monitor (Uptime Kuma, Homepage) can watch the same field in
  `GET /status`.
- **Only while the scheduler runs.** With the scheduler stopped, no
  scheduled sync starts. A tick missed then is not caught up: the
  collection syncs at its next tick after the scheduler starts, and the
  miss shows as `tick_overdue` until then (§req:constraints). On a day
  the clock changes, a time that does not exist fires once at the next
  valid minute, and a time that occurs twice fires once.
- **Idle costs nothing.** Between ticks the scheduler waits on a timer,
  checks the file's modification time, and replaces its heartbeat,
  about once a minute; it adds no polling of sources and no work on the
  NAS.
- **No new history to trim.** What this increment stores is current
  state, not a log: the heartbeat, the edit state (§spec:edit-lock), and
  whether the stale-scheduler notification was sent for this episode are
  one record each; stored credentials are one record per source and go
  with the source. Scheduled syncs and purges write the run records and
  activity entries every run already writes, at the rate ofelia's ticks
  did; activity is kept 30 days and run records are kept, as before
  (§spec:state), their error lines as §spec:run-error-retention sets.
- **Manual runs are unchanged.** `shakedown sync` typed by hand or run
  in a one-off container works exactly as before, with or without the
  scheduler or `serve`.

**Decision and constraint.** Shakedown schedules because the operator
moved timing into the dashboard and out of the compose file
(§req:constraints, operator decision, 2026-11-24): one place to edit, no
redeploy, and no Docker socket for anything the dashboard writes. The
scheduler is its own container, not part of `serve`, because
§req:success-criteria #1 makes "the sync ran on schedule" the product's
core promise, and the web side is the part most exposed to hangs, leaks,
restarts, and outside requests; a scheduler with no port and a
read-only config is unaffected by all of them (operator decision,
2026-11-24). It is honest about what the split does not buy: a
compromised `serve` can still change schedules through the file, as the
dashboard is meant to. Liveness is checked from outside the scheduler —
by `serve` and by Docker's health check — because a process cannot report
its own death; the heartbeat is the decidable fact, and an overdue tick
is decided from the schedule and the run records `serve` already holds.
The schedule lives in the file because the file stays the source of
truth, so an IaC deployment sets schedules the same way it sets
everything else. Cron is the standard schedule format (boring edge), and
presets are written as cron so the file holds one form and a power user
can always read it. A tick launches the same one-shot run ofelia
launched, so runs stay stateless and crash-isolated and pick up the
current file; and a tick keeps scheduler semantics under the lock
because §req:success-criteria #27 requires a long run to delay, never
cancel, other collections' syncs. The timezone is a setting, not only an
environment variable, so a new user who sets "daily at 03:00" in setup
gets their own 03:00 without touching compose (§req:constraints: what
stays outside the dashboard).

**Alternatives rejected.** *Schedule inside `serve`, with the same
detection* — one container fewer, but a web hang or restart stops ticks,
and nothing can send an alert when the one process is down; the
operator chose the separate container. *Keep ofelia and have the
dashboard rewrite its labels or job file* — rejected in
§spec:deployment. *Run syncs inside the scheduler's process* — rejected
before (§spec:api-sync-runs): a crash or leak in a days-long sync would
take the scheduler down. *A structured schedule format (`every: weekly,
at: "03:00"`) instead of cron* — readable, but a custom format with its
own parser and docs; cron with preset read-back gives both. *Catch up
ticks missed while the scheduler was down* — out of scope
(§req:constraints); the miss is shown instead. *Fail `serve`'s
`/healthz` when the scheduler is stale* — Docker would mark the wrong
container unhealthy, and the dashboard must stay up to say what is
wrong. *Seconds in the expression, as ofelia used* — no requirement
needs it, and five fields is the cron people know.

**Tradeoffs.** One more long-running container. Scheduled syncs need the
scheduler running; dashboard actions need `serve`. Recreating the
scheduler stops the runs it started (§spec:graceful-stop), which resume
at their next run. A change takes up to a minute to reach the planner,
and a stopped scheduler takes up to three minutes to show as stale. Two
collections given the same minute by hand run one after the other.

**User-level test path.** Add a collection with the default schedule:
it lands on the first free Sunday hour and its next sync shows. Set it
to Daily at the next minute: within a minute of saving, at that minute,
it syncs and appears in the live view, with no restart and no compose
change. Start a long sync of another collection first: the tick is
rescheduled and runs when that sync finishes. Edit `schedule:` by hand
to a cron expression: the dashboard shows it as cron and the next sync
moves. Set a bad expression by hand: Settings shows the error on that
schedule, and the other collections keep their ticks. Restart `serve`
across a tick: the tick still runs. Stop the scheduler: within three
minutes the dashboard shows it stopped, Needs attention lists it, the
failure webhook fires once, and Container Station shows the container
unhealthy; a tick that passes meanwhile shows as overdue. Start it
again: the entries clear and the next tick runs.

## Credentials in the dashboard §spec:dashboard-credentials

*Status: in progress*
The API, storage, and run resolution are built; the credential entry form waits on Design.

A user can enter a source's archive.org credentials in the dashboard.
They never appear in `shakedown.yaml`, on any page, in any API answer,
or in any log. Credentials set as environment variables still work and
win (§req:success-criteria #40; §req:quality-attributes: credentials
stay out of sight; §req:constraints: credentials, dashboard or
environment; §req:user-stories: credentials without a redeploy).

Observable behavior:

- **Write-only.** `PUT /api/credentials` with `{source, email,
  password}` stores them for that source and answers
  `200 {source, credentials, check}`: `credentials` is the source's
  status after the change, and `check` is the source's verdict on the
  login — `accepted`, `rejected`, or `unchecked` when the source could
  not be reached. Rejected credentials are still stored, so a user can
  retry the check through Validate. `DELETE /api/credentials?source=`
  forgets them. Unknown source, or a source type that takes no
  credentials: `404` (`{code: "unknown_source"}` or
  `{code: "takes_no_credentials"}`). Values travel only in the request
  body, never in a URL. A body that is not exactly those three non-empty
  strings answers `422 {code: "invalid_body"}`, never the field-by-field
  detail, which would quote what was entered. Values are trimmed, as
  environment values are. Both endpoints pass the write gate
  (§spec:dashboard-run-controls).
- **Status, never values.** `GET /api/config` reports, per source,
  `environment` (its environment variables are set and in effect),
  `dashboard` (stored credentials are in effect), or `none`. No answer
  returns a stored value or a hint of one.
- **Which wins.** A run uses a source's environment variables when both
  are set: those `auth` names in the file, or the source type's standard
  names (`IA_EMAIL`, `IA_PASSWORD`) when the file names none; a name the
  file leaves out falls back to the standard one. Otherwise it uses the
  source's stored credentials; otherwise none. `auth` stays optional in
  the file, and setup never writes it. The `ia` source logs in to
  archive.org once per run with that email and password; a login the
  source rejects or cannot answer is logged, without values, and the run
  continues without it. Validate asks the source about the login in
  effect and fails on a rejected or unchecked one.
- **Where they live.** Stored credentials are kept in the state database
  the config names, in plain text, protected as that file is (operator
  decision, 2026-11-24), one row per source in a table outside schema
  versioning (§spec:state): `serve` writes it and never holds the writer
  lock that migrates. A save that moves the state database carries them
  with the edit state (§spec:edit-lock) and leaves none in the old one.
  Removing a source from the file forgets its stored credentials at that
  save, as does any save after a hand edit removed one; a save that
  fails to write the file forgets nothing.
- **Never logged.** No log line, error message, validation result,
  export, or `status` output contains a stored or entered credential.
- **Not under the edit lock.** Credentials are not in the file, so the
  edit lock (§spec:edit-lock) does not cover them.

**Decision and constraint.** The state database, because Shakedown alone
writes it, it sits beside the archive and is backed up with it, and it
is outside the config folder people back up, share, and commit
(§req:quality-attributes: credentials stay out of sight). Environment
variables win so a deployment that already sets them keeps working
unchanged and an IaC deployment keeps secrets in its own secret store
(§req:constraints). The login is checked at entry, without blocking,
because a user entering credentials wants to know they work, and only
the source can say whether a login is accepted.

**Alternatives rejected.** *A secrets file beside `shakedown.yaml`* —
deployable by IaC, but it sits in the folder people commit; the
operator chose the database. *Encrypt them with a key on the same NAS*
— anyone who can read the database can read the key; protection in name
only. *Refuse credentials the source rejects* — a source outage would
block entry; the verdict is shown instead.

**Tradeoffs.** A backup of the state database holds the credentials. A
rebuilt database forgets them, and the user enters them again.

**User-level test path.** Enter credentials for the `ia` source: the
dashboard shows them as set from the dashboard, a sync of a
download-restricted collection downloads its files, and a search of
`shakedown.yaml`, every `/api/*` answer, and the `serve` and run logs
finds neither value. Set `IA_EMAIL` and `IA_PASSWORD` on the container:
the dashboard shows environment in effect. Enter a wrong password: the
answer says rejected.

## First-run setup §spec:first-run-setup

*Status: in progress*
The API is built: `serve` starts with no file, `GET /api/setup`, and `POST /api/setup`; the setup flow on screen waits on the Design task.

A homelab user starts the compose example with no `shakedown.yaml`.
Opening the dashboard leads them through setup in the browser — paths
with a hardlink check, optional archive.org credentials, a first
collection, and its schedule — and when they finish, `shakedown.yaml`
exists and a first sync starts. They never open, write, or upload a YAML
file (§req:success-criteria #6, #35; §req:user-stories: set up without
YAML; §req:constraints: what stays outside the dashboard).

Observable behavior:

- **`serve` starts without a config.** With no file at the config path,
  `serve` starts, the dashboard opens, and `GET /api/config` answers
  `state: "missing"`. The page leads to setup instead of the no-config
  state (§spec:ui-kit). The read endpoints that need a config answer as
  they do today with none.
- **Defaults that work on the example stack.** `GET /api/setup` answers
  the defaults setup offers: `archive_root` `/data/archive`,
  `library_root` `/data/library`, `state_db`
  `/data/archive/.shakedown/state.db`, the source types available
  (`source_types`, as in §spec:dashboard-settings), and the default
  first schedule (§spec:schedules) as `schedule`, with `generated_at`
  like every dashboard answer. The page fills `timezone` from the
  browser.
- **Path check before finishing.** Setup runs the path checks through
  `POST /api/config/check-paths` (§spec:dashboard-settings); a failure is
  a warning, and the user may continue. The check creates the archive and
  library folders when they are missing, as validation does today.
- **Finishing.** `POST /api/setup` takes `{document, credentials}`:
  `document` as in `PUT /api/config`, with exactly one source holding
  exactly one collection; `credentials` optional, `{email, password}` for
  that source. The system checks the document like any save, creates the
  config folder if needed, writes the file, records it as `editable`
  (§spec:edit-lock), stores the credentials, and starts the collection's
  first sync as Sync now does (§spec:dashboard-run-controls). Answers:
  - `201 {revision, warnings, started}` — written; `started` is the
    scope the sync started (`{source, collection}`), or `null` with a
    `reason` code when it could not start; the file stays written;
  - `422 {errors}` — refused, nothing written. Besides the save's
    codes, a document without exactly one source holding exactly one
    collection is `setup_one_collection`, at `["sources"]` or
    `["sources", 0, "collections"]`;
  - `422 {code: "invalid_body"}` — a body of another shape, or
    credentials that are not two non-empty strings; never the
    field-by-field detail, which would quote them. Credentials for a
    source type that takes none answer
    `404 {code: "takes_no_credentials"}`, as in
    §spec:dashboard-credentials;
  - `409 {code: "exists"}` — a file appeared at the path since setup
    began; nothing is overwritten and the page moves to Settings;
  - `403` and `500 write_failed` as in `PUT /api/config`. The file is
    written before the edit state and the credentials, so a file that
    fails to write leaves no `editable` record to unlock a file written
    later by hand; a state database that fails after it answers
    `write_failed` naming the database, and the file stays.
  `reason` is `busy` (another run holds the writer lock), `failed` (the
  run could not be launched), or `exited_early` (it exited before it
  started). Warnings are the path checks' failures and
  `layout_not_unique`; setup moves nothing, so there is no
  `path_moved`.
- **The written file.** It holds only what setup set, plus a header
  comment saying the file is editable by hand or in the dashboard, and
  pointing at `shakedown.example.yaml` for the full option list.
- **More later.** More sources and collections are added in Settings
  after setup.

**Decision and constraint.** Setup writes the same file Settings edits,
because §req:constraints keeps the file the truth. It asks for one
collection and starts its sync, because §req:success-criteria #35 ends
at "a first sync starts," and more belongs in Settings. Setup is only
offered while no file exists, so it can never overwrite a file
(§req:quality-attributes: the config file stays the truth). The file
lives inside the single data mount (§spec:deployment), so a new user
creates no folder and mounts nothing extra.

**Alternatives rejected.** *A CLI `init` that writes a starter file* —
still a YAML step for the user, against #35. *Start with an empty
config and no first sync* — the user would finish setup and see
nothing happen. *Sync every collection at bring-up* — §spec:deployment
rules out unscoped work at `up`; the first sync here is an explicit
action.

**Tradeoffs.** Setup covers one source and one collection; anything
more is a second visit to Settings. A stack that never finishes setup
runs the scheduler with no schedules.

**User-level test path.** On a clean stack from the compose example,
open the dashboard on a phone: setup appears. Accept the default paths;
the hardlink check passes. Skip credentials, add `phish` with query
`collection:Phish`, keep the default schedule, finish: the live view
shows the first sync of `phish`, `shakedown.yaml` exists with that
collection and schedule, and Settings is editable. Repeat with the
library on another filesystem: the warning shows and setup still
finishes.

## Run controls in the dashboard §spec:dashboard-run-controls

*Status: in progress*
The write gate is built; the Sync now and Validate buttons wait on Design.

From the dashboard a user can start a sync of one collection and run
setup validation, and watch the result in the live view. These, and
every other write the dashboard makes, pass one write gate: no login, a
refusal for requests from other websites, and the bearer token kept for
scripts (§req:success-criteria #33, #41; §req:constraints: no login of
its own, config management in the dashboard; §req:user-stories: sync it
now).

Observable behavior:

- **Sync now.** The dashboard starts a collection's sync through
  `POST /sync?source=&collection=` and gets that endpoint's answers
  unchanged (§spec:api-sync-runs): `202` at once with the scope, `409`
  with "a run is in progress" when another run holds the writer lock
  (refused at once, never queued or rescheduled), `404` for an unknown
  scope, `503` with the config error. The run uses the file as it is at
  that moment and shows in the live view like any run.
- **Validate.** The dashboard runs `POST /validate` (non-live handoff
  checks only) and shows its result; the result is recorded and shows
  as the last validation in health (§spec:dashboard-health). Live
  handoff validation stays token-only and is not offered in the
  dashboard: it can run the file's configured command.
- **The write gate.** Every endpoint that writes or starts work —
  `POST /sync`, `POST /verify`, `POST /validate`, `PUT /api/config`,
  `POST /api/config/override`, `POST /api/config/check-paths`,
  `POST /api/setup`, `PUT` and `DELETE /api/credentials`,
  `POST /api/collections/purge`, `POST /api/history/trim` — accepts a
  request that either:
  - carries the configured bearer token (`SHAKEDOWN_API_TOKEN`), as
    scripts and `curl` do today; or
  - comes from the dashboard's own page: the browser marks it
    same-origin, and it is a JSON request a cross-site form cannot send.
  Anything else answers `403 {code: "cross_site"}`. With no token
  configured, only the dashboard writes; the token stays optional
  (operator decision, 2026-11-24). Live handoff validation accepts the
  token only.
- **Looking still changes nothing.** Every `GET` stays free of writes,
  runs, and source requests (§spec:dashboard); only these explicit
  actions change anything.

**Decision and constraint.** The reverse proxy stays the only gate on
who opens the dashboard (§req:constraints: no login of its own,
operator decision, 2026-11-24), so dashboard writes carry no token. The
same-origin rule closes the one hole that posture leaves open in a
browser: another website the operator visits while signed in to the
proxy cannot fire writes through the operator's browser (cross-site
request forgery). The browser's fetch metadata and a JSON body are the
standard defense, decided from the request itself. DNS-rebinding attacks
mostly fail on the example layout, because `serve` listens on loopback
and the proxy routes by host name; the command handoff staying in the
file (§spec:dashboard-settings) caps what is left. The token stays for
scripts so `curl` triggers keep working unchanged (operator decision,
2026-11-24). Sync now reuses `POST /sync` because
§req:success-criteria #41 asks for the same rules as an API trigger.

**Alternatives rejected.** *Retire the token, same-origin only* —
breaks `curl` triggers for no gain. *Token everywhere, stored in the
browser* — a login in all but name, which §req:constraints rules out.
*No same-origin check* — leaves every write open to any website the
operator visits. *A Host allowlist against DNS rebinding* — one more
bootstrap setting for an attack the example layout already blunts.

**Tradeoffs.** Anyone who passes the proxy can change settings, start
runs, and delete an unconfigured collection's files — the posture the
operator chose. The same-origin rule stops browsers, not scripts: a
script that reaches `serve` directly can forge the headers, so the proxy
and the loopback-only port remain the real gate. Browsers send fetch
metadata only to HTTPS and localhost origins, so dashboard writes work
through the HTTPS proxy or at `http://127.0.0.1`, and are refused from a
page opened over plain HTTP at another address.

**User-level test path.** Tap Sync now on a collection: it starts and
shows in the live view. Tap it again while that sync runs: the
dashboard shows that a run is in progress. Tap Validate: the result
shows, and health shows it as the last validation. From a page on
another origin, POST to `/sync` and `PUT /api/config` in the browser:
both refused. `curl -XPOST` `/sync` with the token: `202`; without it
and without the dashboard's headers: `403`.

## Removing a collection §spec:collection-removal

*Status: in progress*
The unconfigured list, `POST /api/collections/purge`, and `shakedown purge` are built; the remove and delete confirmations and the unconfigured list on screen wait on Design.

Removing a collection stops syncing it. By default its archived shows
and library folders stay. The dashboard says what happens before the
user confirms, and offers to delete the collection's library folders,
or its library folders and archived shows. A collection removed with
its files kept stays listed, so its files can be deleted, or the
collection added back, later (§req:success-criteria #37; operator
decision, 2026-11-24).

Observable behavior:

- **Remove = leave the file.** Removing a collection in Settings saves
  the file without it (`PUT /api/config`, §spec:dashboard-settings).
  From then on it has no schedule and cannot be synced.
- **Unconfigured collections.** `GET /api/config` lists, in
  `unconfigured`, every collection the state database still holds shows
  or library files for that the file no longer names: `{source,
  collection, shows, archive_bytes, library_bytes}`. A collection
  dropped from the file by hand or by an IaC deploy appears there too.
  `shows` and `archive_bytes` count the shows with an archive copy, as
  `status` does; `library_bytes` counts those of them the library still
  holds, so it reads 0 after a library delete. The list is empty when the
  file is not `valid` or no state database exists yet.
  Adding a collection with the same source and name brings it back: its
  archived shows are reused and nothing downloads again.
- **Two deletes.** `POST /api/collections/purge` with `{source,
  collection, scope}` starts a fresh run that deletes the collection's
  files:
  - `scope: "library"` — deletes every library file and folder the
    placement records say Shakedown put there for this collection
    (§spec:library-ownership), and those records. Files a user added
    stay. The archive stays, so adding the collection back restages the
    library with no download.
  - `scope: "everything"` — does that and also deletes the collection's
    archive folder and its shows' records. Adding it back downloads
    everything again.
  Answers: `202 {started: "purge", source, collection, scope}`; `409`
  with "a run is in progress" when another run holds the writer lock;
  `409 {code: "configured"}` when the file still names the collection;
  `404` when nothing is held for it. The run shows in the live view and
  run history as kind `purge` (§spec:run-activity, §spec:run-history)
  and frees the space it reports. It also drops the collection's
  rescheduled entry and pending handoffs. The same run is
  `shakedown purge --source S --collection C [--archive]` on the CLI,
  which refuses a configured collection, or one with nothing held, as a
  usage error.
  - **What a purge deletes.** A file at a placed path that is not the
    file placed (its inode differs) is someone else's: it stays and is
    recorded as not placed by Shakedown (§spec:library-ownership).
    Folders left empty go, up to and including the collection's library
    folder; a folder holding a user's file stays. The archive goes show
    by show: a show's records go once its folder is gone, so a show whose
    folder could not be deleted keeps them, the collection stays listed,
    and a later purge retries it. A delete that fails is an error line
    naming the path, and the run ends `completed_with_item_issues`; it
    never stops at one file.
  - **The space it reports** (`bytes_freed` on its live-view and
    run-history entries) counts only files whose last link it removed.
    Library files are hardlinks into the archive (§spec:library-stats),
    so a library delete reports about 0 bytes, and an everything delete
    the archive's size.
  - **Read again under the lock.** The run reads the config file again
    once it holds the writer lock and deletes nothing if the file names
    the collection again, cannot be read, or nothing is held; the run then
    fails with that reason. A source or collection name that is not one
    folder name is refused the same way, and so is a source or collection
    folder that is a link, which would aim the deletes outside the
    collection. A placed file reached through a linked folder deeper in
    the library is kept and reported, and stays recorded.
- **Before confirming.** The remove and delete confirmations state what
  each choice deletes, that deletion cannot be undone, and the shows
  and space it covers, from the `unconfigured` figures or the
  collection's library figures (§spec:library-stats).
- **Remove and delete in one go.** When the user removes a collection
  and chooses a delete, the save happens first, then the purge starts.
  If the purge cannot start (a run is in progress), the collection is
  removed with its files kept and listed under `unconfigured`, where the
  user can delete them later.

**Decision and constraint.** Keep by default, because the archive took
days or weeks to download and §req:constraints treats it as the thing
an upgrade or edit must never cost; deletes are offered, explicit, and
labelled irreversible because the operator wants the disk back when
they choose to (operator decision, 2026-11-24). A removed collection
leaves the file rather than staying in it switched off, so the file
lists only what syncs and an IaC file needs no extra flag; the state
database already knows which collections hold files. A purge is a run
under the writer lock because it deletes what a running sync may be
writing, and runs are how Shakedown changes the archive. Library
deletion follows the placement records because they are the one
authority for "Shakedown put this file here."

**Alternatives rejected.** *Removed collections stay in the file with a
switch that stops syncing* — one click to re-enable, but IaC files must
carry the switch and the file keeps growing; the operator chose the
list. *Delete the files on remove, always* — irreversible by default.
*Never offer deletion* — leaves disk recovery to File Station, where a
user cannot tell which folders are safe to delete.

**Tradeoffs.** An unauthenticated page can delete an archive: the proxy
is the gate (§spec:dashboard-run-controls), and the confirmation names
the loss. A removed collection's settings are gone; adding it back
starts from defaults with the old name.

**User-level test path.** Remove `phish` and keep its files: the file
no longer names it, no tick runs it, and it is listed as unconfigured
with its shows and sizes. Delete its library: the purge shows in the
live view, its library folders are gone, a file you added to one of
them stays, and the archive is intact. Add `phish` back: the next sync
restages it with no download. Remove it again and delete everything:
its archive folder is gone and the list no longer shows it. Start a
purge while a sync runs: refused as a run in progress.

## Run error retention §spec:run-error-retention

*Status: in progress*
The setting, the automatic trim, `shakedown trim-history`, and `POST /api/history/trim` are built; the Settings field and the Trim now button wait on Design.

Each run keeps its per-show error lines for 180 days by default, a
setting the user can change in the dashboard; after that the run keeps
its result, counts, and dates, and drops the lines. A user can also trim
now from the dashboard, which takes the space back on disk
(§req:quality-attributes: bounded history; operator decision,
2026-11-24).

Observable behavior:

- **The setting.** `keep_run_errors_days` in `shakedown.yaml`, a whole
  number of days, 180 when absent. It is validated like any key
  (§spec:configuration): below 30 is refused, because the dashboard's
  run history shows the last 30 days with their errors
  (§spec:run-history). It is edited in Settings like every other global
  setting (§spec:dashboard-settings).
- **Automatic trim.** When a run that holds the writer lock ends, it
  clears the error lines of every run that started more than
  `keep_run_errors_days` ago, together with the error lines of their
  operation outcomes, as it already trims 30-day history (§spec:state).
  A trimmed run keeps its kind, collection, start and end, result, item
  counts, and bytes, and records that its detail was trimmed
  (`detail_trimmed` in `status` and §spec:run-history); `status` and the
  dashboard show those as before. A run whose activity entry is gone
  reads its result from one run-level line — that it crashed, or ended
  unexpectedly — so the trim keeps that line and drops the per-show
  lines. Staleness and sync identity never read error lines, so trimming
  never changes them, the result, or the last good sync.
- **Trim now.** `POST /api/history/trim` starts a fresh run, kind
  `trim`, under the writer lock. It applies the same rule at once, then
  compacts the state database so the freed space returns to the disk.
  It passes the write gate (§spec:dashboard-run-controls). Answers:
  `202 {started: "trim"}`; `409` with "a run is in progress" when another
  run holds the lock. The run shows in the live view and run history
  (§spec:run-activity, §spec:run-history) with the runs it trimmed and
  the bytes the database shrank by (`runs_trimmed`, `bytes_freed`). If the disk lacks room
  to compact (it needs about the database's size free), the trim still
  happens, the run ends failed with the compaction failure as its error
  line (the CLI exits 1; operator decision, 2026-11-24), and the database
  keeps its size until the next attempt; the write-ahead log the failed
  attempt grew is emptied, so a failed compaction never leaves the disk
  fuller. A reader that keeps the log from emptying fails the compaction
  the same way. Compacting holds the database's write lock, so the trim's
  heartbeat cannot land until it ends: the live view gives a running trim
  an hour before it reads as stopped reporting, and the next run that
  holds the writer lock closes a trim left open at once. The same run is
  `shakedown trim-history` on the CLI.

**Decision and constraint.** Run records are kept because last good
sync and staleness read them, but their error lines carry no such duty,
and a source that keeps failing writes one line per failed show every
run — the one part of the database that can grow by hundreds of
megabytes a year. The operator chose 180 days, changeable, with an
on-demand trim (2026-11-24). The automatic trim bounds growth with no
action; the on-demand trim is the way to take space back, which deleting
rows alone does not do. It runs under the writer lock because compacting
needs the database to itself.

**Alternatives rejected.** *Drop error lines after 30 days, fixed* —
simpler, but the operator wants a longer default and control. *Cap
lines per run* — bounds one bad run, but a run that fails every night
still adds up forever. *Trim whole run records* — loses last good sync
and staleness for quiet collections. *Compact on every automatic trim*
— rewrites the whole database after every run, for space a later run
reuses anyway.

**Tradeoffs.** One more setting. A trim now holds the writer lock while
it compacts, delaying a scheduled tick into a catch-up
(§spec:rescheduled-syncs). Error detail older than the setting is gone
for good.

**User-level test path.** Set `keep_run_errors_days` to 30 on a
database with a failed run from 40 days ago: after the next sync ends,
that run still shows its result and counts in `status` and run history,
without its error lines; a run from 20 days ago keeps them. Set it to 10:
Save is refused. Tap Trim now: a `trim` run appears in the live view and
reports the space freed, and the database file is smaller. Tap it while a
sync runs: refused as a run in progress.

## Deployment §spec:deployment

*Status: complete*

The deployment is a single Docker Compose stack, paste-able into QNAP
Container Station and operable entirely through web UIs — no SSH at any
step (§req:success-criteria #6). The canonical example is
[`docker-compose.example.yaml`](docker-compose.example.yaml).

**Two long-lived Shakedown containers, one image, one user.**
`shakedown-serve` serves the dashboard (§spec:dashboard) and the control
plane (§spec:serve), starts Sync now and API-triggered runs, and watches
the scheduler. `shakedown-scheduler` runs `shakedown schedule` and
starts each collection's scheduled syncs (§spec:schedules); it publishes
no port and its health check is `shakedown schedule --check`. Every run
is a fresh one-shot `shakedown` process started by one of the two
(§req:constraints: runs stay one-shot and stateless). A tick that finds
another run writing is rescheduled (§spec:rescheduled-syncs), so "the
schedule is configured" and "the sync ran" are the same event while the
scheduler is up (§req:success-criteria #1); a missed tick shows in the
dashboard (§spec:schedules).

The compose file holds only what must exist before `serve` can show a
page (§req:constraints: what stays outside the dashboard):

- the image (`SHAKEDOWN_IMAGE`) and container user (`SHAKEDOWN_USER`,
  the owner of the data share), the same for both containers;
- one volume, the data share mounted once as `/data`. Archive, library,
  and the config folder (`/data/shakedown-config/`, the path every
  command reads by default) live under it, so archive and library share
  a filesystem *and* a mount — Linux refuses hardlinks across separate
  mounts (§spec:system-shape) — and `serve` can write the config
  (§spec:dashboard-settings). The scheduler mounts the config folder
  again, read-only, over it, so no scheduled run can rewrite the config;
- serve's port, published on host loopback only, so the operator's
  reverse proxy is the one way in (operator decision, 2026-11-12);
- optionally `SHAKEDOWN_API_TOKEN` (§spec:dashboard-run-controls),
  archive.org credentials as environment variables
  (§spec:dashboard-credentials), and `TZ`, given to both containers.

Everything else is set in the dashboard (§spec:first-run-setup,
§spec:dashboard-settings). No third-party scheduler runs, and no
Shakedown container mounts the Docker socket. Library tools and
streaming servers (Navidrome, Beets, …) join the stack and read the
library tree; Shakedown places no requirements on them.

**First bring-up does no unscoped work.** A new stack syncs nothing on
`up`; setup's last step starts one sync of the collection it adds
(§spec:first-run-setup), and after that each collection syncs on its own
schedule. There is deliberately no "sync everything at startup"
(§req:quality-attributes).

**Manual triggers, no SSH needed.** Sync now in the dashboard or
`POST /sync` with the bearer token (§spec:dashboard-run-controls); as a
fallback, a one-off container from the same image with
`sync --collection <name>`, via Container Station's UI or `docker run`.
SSH stays supported as guidance, never required.

**Updates (operator decision, 2026-11-24).** Watchtower pulls a newer
`SHAKEDOWN_IMAGE` and recreates both Shakedown containers from it, so
`latest` keeps Shakedown updated as ofelia's per-tick pull did:

- **Shakedown only:** it updates only labelled containers, and only the
  two Shakedown containers are labelled.
- **At a fixed time:** once a day (provisional default: 12:00 in the
  container's timezone), so an update never lands at a random moment.
  A recreate stops the runs a container started and waits for them
  (§spec:graceful-stop), with the same 120 s as `stop_grace_period`; an
  interrupted sync resumes at its next run.
- **Through a socket proxy:** Watchtower reaches Docker only through a
  proxy that allows its container, image, and write calls — what a
  recreate and a pull need — and refuses the rest (exec, volumes,
  secrets, swarm, system). The proxy alone mounts the socket, read-only,
  on an internal network with no published port. linuxserver's proxy is
  used because, with exec off, it refuses creating an exec as well as
  starting one. Watchtower is the maintained fork; the original is
  archived.
- **Optional:** a user who pins a release tag or updates by hand deletes
  both services; nothing else depends on them.

**Moving an existing install over** (§req:constraints: deployment
status). The archive, library, and state database stay where they are,
so nothing downloads again. Replace the compose file (drop ofelia; add
the scheduler, Watchtower, and the proxy; give `serve` the single
`/data` mount). The existing `shakedown.yaml` already sits in
`/share/data/shakedown-config/`, which is `/data/shakedown-config/`
inside the containers; Shakedown did not write it, so dashboard edits
start locked (§spec:edit-lock). Schedules are re-entered by hand from
the old ofelia labels, dropping their leading seconds field; until then
each collection is manual only (operator decision, 2026-11-24: no
import). The README's Upgrading notes carry the steps.

**Why a Shakedown scheduler, rather than ofelia.** ofelia launched a
fresh container per tick from Docker labels, keeping timing in the
compose file with no scheduler code in Shakedown. Adding a collection
then meant two edits and a stack redeploy, and a schedule could not be
shown or set from the dashboard, so the operator moved timing into
Shakedown (§req:constraints, operator decision, 2026-11-24). Launching
a fresh one-shot process per tick keeps what ofelia gave: no run state
lives in the scheduler, and a tick never silently no-ops because a prior
run exited. Nothing the dashboard writes reaches a container that holds
the Docker socket: the only socket user is Watchtower, behind the proxy,
configured only in the compose file.

**Alternatives rejected.** *Keep ofelia and let the dashboard rewrite
its labels* — still needs the Docker socket and a redeploy per change.
*Generate an ofelia job file from the dashboard's schedules* (the
netresearch fork reloads its INI file) — switches to a fork, keeps two
scheduling layers in step, and lets an unauthenticated page write job
definitions for the container holding the Docker socket, where an
injected volume or command is root on the host (operator decision,
2026-11-24). *No automatic updates* — simplest, but the operator wants
`latest` to keep updating Shakedown. *Host cron* — needs SSH
(§req:success-criteria #6). *Keep the config mounted read-only and store
dashboard edits elsewhere* — breaks the file as the source of truth
(§req:constraints).

**Tradeoffs.** The scheduler is required for scheduled syncs, and a tick
missed while it is stopped is shown, not caught up (§spec:schedules). It
also shows unhealthy until a config exists, since its heartbeat lives in
the state database the config names. An update recreates both
containers and stops a running sync until its next run. The proxy
narrows what Watchtower can ask Docker for, not the privilege: creating
containers is still host-root-equivalent, so operators place the stack
accordingly. The stack runs three containers (scheduler, Watchtower,
proxy) where ofelia was one. Runs `serve` starts can write the config
folder, which the old read-only mount prevented; no run writes it, and
the command handoff that could is set only in the file
(§spec:dashboard-settings). Scheduled runs still cannot. Scheduled run
output goes to the scheduler container's log.

## Setup readiness validation §spec:setup-readiness-validation

*Status: complete*

A first-time operator can run a dedicated readiness validation before
the first large mirror. The validation reports one pass/fail result per
collection and names the specific setting, path, credential, source, or
handoff target that needs action. It never downloads a full collection
and never leaves behind archive or library items; a broken setup cannot
look like a clean no-op. (§req:success-criteria #16,
§req:user-stories: mirror a collection, add another collection, hand
off automatically)

Observable behavior:

- `shakedown validate` checks the same startup configuration contract as
  every command, then continues into setup-specific probes: archive,
  library, and state paths are writable where writes are required;
  a test file in the archive can be hardlinked into the library;
  configured sources and
  collections can be reached; required credential environment variables
  are present and accepted by the source when credentials are needed;
  layout templates are checked with the existing collision-risk guard;
  and configured handoff targets are checked according to the handoff
  validation mode below.
- Validation output is grouped by source and collection. Each failing
  check gives the plain-language consequence and the user action needed
  to continue, for example "archive path is not writable; fix the volume
  mount before syncing" or "IA credentials were rejected; update the
  environment variables in Container Station." `--json` emits the same
  structure for dashboards and the optional control plane.
- A validation failure exits non-zero. A validation pass means "this
  deployment is ready to attempt a real sync," not "the whole collection
  has been mirrored."
- The optional control plane exposes the same validation surface for
  no-SSH deployments. Live handoff tests accept the bearer token only
  (§spec:dashboard-run-controls).

**Decision and constraint.** Readiness is a dedicated validation flow,
not an expanded `sync --dry-run` and not an always-on deep startup gate.
The constraint is QNAP/no-SSH operability: a product director-level
operator needs one visible "is this deployment ready?" action before a
multi-hour mirror begins (§req:success-criteria #6, #16). `sync
--dry-run` remains the plan preview for an already-valid deployment,
while ordinary `status`, `restage`, and `reconcile` stay usable when an
external source or handoff receiver is temporarily down.

**Handoff validation and abuse case.** By default, handoff validation is
non-mutating: webhook URLs are parsed and safely probed when the
receiver supports a harmless check, configured commands are checked for
presence and basic executability, and required environment variables are
verified, but Shakedown does not import music, send production
notifications, or run arbitrary configured commands as part of a normal
readiness check. When the operator passes `--live-handoff`, Shakedown
sends a marked validation webhook or runs the configured command with a
marked test payload and records the result as a handoff readiness check.
This was chosen because the director accepted the higher-confidence
live test as an explicit action, while the default must not surprise a
new deployment by triggering duplicate imports or arbitrary side
effects. The blast radius is the existing handoff trust boundary: an
operator who configures `exec` has already granted Shakedown permission
to run that command, so validation must make the live execution visible,
explicit, and auditable rather than implicit.

**Alternatives rejected.** Expanding `sync --dry-run` was rejected
because it mixes setup diagnosis with sync planning; a new operator
would have to infer whether "nothing to do" means "valid and empty" or
"the source/path/handoff could not be reached." A mandatory deep startup
gate was rejected because it would make read-only recovery commands fail
when an unrelated external dependency is down, directly conflicting with
the recovery requirement (§req:success-criteria #17). Skipping handoff
validation was rejected because a setup could pass while the promised
library import path is broken (§req:user-stories: hand off
automatically).

**Tradeoffs.** A dedicated command adds one more surface to document,
and live handoff validation can still cause receiver-side effects if the
receiver ignores the marked test payload. The accepted tradeoff is that
the potentially mutating behavior is explicit and opt-in, while the
default readiness check remains safe to run repeatedly.

**User-level test path.** On a fresh config, run validation with a
missing archive path, a bad credential environment variable, an
unreachable source, and a valid minimal collection. The output identifies
each failing collection or setting and exits non-zero without
downloading a collection. Fix the configuration and rerun until it
passes; then run `validate --live-handoff` against a test webhook or
test command and see the marked validation event recorded by the
receiver.

## Recoverable operation reporting §spec:recoverable-operation-reporting

*Status: complete*

When `sync`, `restage`, `reconcile`, or `validate` fails partway
through, the next visible status tells the operator what completed, what
did not, which collection or item needs attention, and whether retrying
is safe. Local recordings remain intact unless the user explicitly
requested deletion, and a later successful retry clears the failure
state instead of leaving stale warnings. (§req:success-criteria #17,
§req:user-stories: recover from a lost database, reorganize the library)

Observable behavior:

- Every recoverable operation records a user-facing outcome for each
  affected source and collection: not started, in progress, completed,
  completed with item-level issues, failed before completion, or stale
  because source enumeration could not be trusted. The wording is
  action-oriented: "retry is safe," "fix configuration then rerun," or
  "deletion was requested; review before retrying."
- `shakedown status` and `status --json` surface the latest incomplete
  or failed operation ahead of ordinary counts, including the operation
  type, the phase that failed, the affected collection or item when
  known, and the next safe action. A successful later run for the same
  collection clears the warning when it has actually completed the
  affected work.
- Operations that make deletion decisions preserve the existing
  barriers: a failed or partial enumeration never prunes, and local
  recordings are retained by default (§spec:discovery-pipeline,
  §spec:item-lifecycle). Recovery reporting explains that preservation
  in status so an operator does not mistake "failed run" for "lost
  archive."
- Interrupted runs are not reported as success merely because the
  process exited. An unfinished operation remains visible until a retry
  completes it. A run stopped on request (SIGTERM, SIGINT) shows as
  stopped in its last run, not as a recovery warning: nothing needs
  recovering, and the next run continues (§spec:graceful-stop).

**Decision and constraint.** Recovery is surfaced through status as the
operator's durable dashboard, not through transient logs alone. The
constraint is no-SSH and post-failure operability: on QNAP, logs may be
harder to inspect and a product director-level operator needs the next
visible command or control-plane response to explain the state without
knowing internal phases (§req:success-criteria #6, #17). The existing
run records, item lifecycle, stale-collection marker, and collision
summary are extended into a coherent recovery narrative rather than
creating a separate recovery subsystem.

**Alternatives rejected.** Relying on container logs was rejected
because logs are ephemeral and do not answer whether a retry is safe
after the container has exited. Treating any partial failure as a full
rollback was rejected because successful item downloads and staging work
are already durable and idempotent; discarding them would slow recovery
without increasing safety. Adding a separate "repair mode" was rejected
because the existing commands are already the recovery path: retry
`sync`, rerun `restage`, rerun `reconcile`, or fix config and rerun
`validate`.

**Tradeoffs.** More status detail means status output must stay grouped
and concise so ordinary healthy deployments remain easy to scan. The
accepted tradeoff is to show the latest actionable recovery issue first
and keep item-level detail available through JSON or targeted item
inspection.

**User-level test path.** Force a network failure during sync, a storage
failure during restage, a bad source response during reconcile, and a
bad path during validation. After each failure, run `shakedown status`
and confirm it identifies the failed operation, affected collection or
item, completed work, and safe next action. Restore the dependency and
retry; status no longer reports success until the affected work has
completed, and once it completes the stale warning is gone.

## Release validation gate §spec:release-validation-gate

*Status: complete*

Covered by `tests/test_release_workflows.py`, run by CI's
`test` job, which the image-publishing `docker` job needs.

A bounded validation of the workflows users rely on runs in CI and must
pass before any release image is published; the shipped product carries
no release-check command (§req:success-criteria #18, revised).

Observable behavior:

- **Lives in the test suite, runs in CI.** The release workflows —
  first-setup readiness (`validate` catches a missing path, bad
  credentials or source access, an unsafe layout, and a broken handoff
  target), unsafe config rejection, partial-failure recovery (a failed
  sync, restage, reconcile, or validation leaves recordings intact, and
  a retry clears the recovery warning only once the work completes),
  and the ordinary sync-to-library path (hardlinked staging) — are
  tests in the project's suite, on synthetic sources and throwaway
  archive, library, config, and state paths. CI runs them on every pull
  request and every release tag. Each test is named for the workflow it
  proves, so a failure reads as "partial-failure recovery: restage" and
  not as an internal fixture.
- **Gates publication.** The job that publishes the image runs only
  after these tests pass; a release whose validation fails never
  reaches the registry, so it never reaches the NAS.
- **Never passes by skipping.** The workflow tests have no skip path; a
  scenario that cannot run fails.
- **Nothing in the image fails by construction.** The
  `release-validate` command and its bundled copy of a test source are
  removed from the product. Readiness of a real deployment stays
  `validate`'s job (§spec:setup-readiness-validation).
- **The real-source check stays separate.** The one-item archive.org
  check (§spec:e2e-real-source) remains an opt-in maintainer command
  run before tagging, excluded from CI (§req:success-criteria #10,
  §req:constraints: network-dependent verification is opt-in).

**Decision and constraint.** The gate is CI tests on synthetic data,
because criterion 18 now makes release checking a maintainer/CI concern
with no command in the shipped image, while criterion 10 and the
constraints keep network checks out of CI. The shipped command could
never pass inside the image: the image has neither the test runner nor
the tests.

**Alternatives rejected.** *Keep `release-validate` and install the
test runner and tests in the image* — ships test code to every NAS for
a check users never run. *Run the real-source check in CI on tags* —
lets an archive.org outage block a release, and criterion 10 keeps it
out of CI. *Keep a deterministic-only command in the product* — the
same scenarios already run as tests; two copies drift.

**Tradeoffs.** A maintainer must remember the real-source check before
tagging; CI does not enforce it.

**User-level test path.** Open a pull request that breaks partial-failure
recovery: CI fails with the recovery workflow named. Tag a release from
a commit whose workflow tests fail: no image is published. Run
`shakedown --help` in the published image: no release-check command is
listed.

## Graceful stop §spec:graceful-stop

*Status: complete*

`docker stop`, a NAS shutdown, Container Station's Stop, and Ctrl-C stop
a writing run cleanly and quickly, and the next run continues where it
left off (§req:quality-attributes: operability, single failures degrade
a single run, never the archive; operator decision, 2026-11-16).

Observable behavior:

- **Who stops.** Every writing command — `sync` (and its catch-ups),
  `item refetch`, `verify --reconform`, `restage`, and `reconcile` —
  handles SIGTERM and SIGINT while it holds the writer lock
  (§spec:cli). Commands that take no lock (`status`, `validate`, a plain
  `verify`, `sync --dry-run`) are unchanged.
- **The first signal stops new work.** The run starts no new listing
  page, lookup, download, tag write, or library link, and starts no
  waiting collection: it gets no run record, its waiting entry goes
  (§spec:run-activity "Waiting collections"), and one log line names
  every collection the stop kept from starting. A download in progress ends at its next chunk: the
  files it finished stay in the show's temp folder, the file it was
  writing is discarded, and the next run fetches only what is missing
  (§spec:failure-behavior). Backoff waits end at once. The step in hand
  always finishes when cutting it short would leave something half done:
  a show's swap into the archive, one file's tag write (with its backup,
  §spec:tag-filling), one show's library links (§spec:library-ownership).
- **What a stopped run records.** Each run it cut off is closed with
  the outcome `stopped`: its run record with what it did, its activity
  entry ended `stopped` (§spec:run-activity). A stopped run is not a
  failure: `status` shows no recovery warning for it (its last run says
  it was stopped), no failure notice is sent, and it is not in Needs
  attention. It prunes nothing and marks nothing disappeared — its
  listing is incomplete (§spec:prune-safety) — and sweeps no temp
  folders. Shows it fetched are recorded and linked as they landed; its
  handoff notice is not sent during the stop (a slow target can take five
  minutes), so those shows join the pending list and the collection's
  next run sends them (§spec:durable-handoff). A `reconcile` stopped
  before its rebuild writes nothing.
- **Rescheduled syncs stay rescheduled.** A stopped collection keeps its
  rescheduled entry, a stopped catch-up keeps its entry, and a sync that
  was stopped runs no catch-up (§spec:rescheduled-syncs).
- **Exit code.** A stopped command releases the writer lock and exits
  143 after SIGTERM or 130 after SIGINT (128 + the signal number, as
  shells and Docker report a process ended by that signal). It is
  non-zero because the work did not finish (§spec:cli), and distinct
  from 1, which means a run failed.
- **A second signal exits at once**, with the same code. Nothing is
  closed; the next run that takes the writer lock closes the run record
  and finishes any half-done swap or tag write, exactly as after a kill
  (§spec:run-activity, §spec:failure-behavior).
- **`serve` stops what it started.** When `serve` shuts down (SIGTERM or
  Ctrl-C), it sends SIGTERM to each run it started that is still going
  and waits up to 90 seconds for it to stop before it exits
  (§spec:api-sync-runs). Each run serve starts is in its own process
  group, so Ctrl-C in serve's terminal reaches serve alone and serve's
  SIGTERM is the run's first signal, not a second one that would end it
  at once. Such a run counts as `stopped` in `shakedown_sync_runs_total`.
- **How long a stop takes.** A healthy download notices the stop within
  a second; a stalled one only when its 30-second read timeout fires.
  The recommended stop timeout is 120 seconds (`stop_grace_period: 2m`
  in compose, `docker stop -t 120`); Docker's default of 10 seconds is
  usually enough, and a run killed after it is still safe.

**Decision and constraint.** A signal handler in shakedown, not an init
process (tini, `docker run --init`, compose `init: true`): in a
container shakedown is PID 1, and the kernel drops every signal a PID
namespace's init leaves at its default action, so before this an
uncaught SIGTERM was ignored and every `docker stop` ended in SIGKILL
after the full grace period. An init would only make that kill faster;
a handler is delivered to PID 1 like to any process and makes the stop
graceful (operator decision, 2026-11-16). Python runs handlers on the
main thread only, so the handler sets one process-wide flag that the
pool workers read at their checkpoints. Downloads are abandoned rather
than finished because a show's files can take minutes, longer than any
stop timeout, while the temp folder already keeps finished files
(§spec:failure-behavior); finishing them would turn most stops into
kills.

**Alternatives rejected.** *An init process* — kills sooner but no more
gracefully. *Finish in-flight downloads within a time bound* — a bound
short enough for Docker's 10 seconds finishes almost nothing, and a
longer one is cut off by the kill. *Exit 0 on a stop* — the work did not
finish, and the scheduler should see that. *Record a stopped run as
failed* — a deliberate stop would raise a recovery warning, a failure
notice, and a Needs attention entry nobody needs to act on.

**Tradeoffs.** The file each download was writing is fetched again
next run. A stopped run's handoff notice waits for the collection's next
run. A run on an older release ignores the signal, as before.

**User-level test path.** Start a long sync in a container; while it
downloads, `docker stop -t 120` it: it exits 143 within seconds;
`status` shows its last run stopped and no recovery warning; the
dashboard shows the run "Stopped". Start the sync again: it fetches only
the rest and ends where an uninterrupted sync would. Press Ctrl-C twice
during a manual sync: it exits at once with 130, and the next sync
closes the run record. Stop `serve` while a sync it started runs: the
sync ends stopped before `serve` exits.

## Failure behavior §spec:failure-behavior

*Status: complete*

Includes the run-activity rows (§spec:run-activity) and the
dashboard row (§spec:dashboard).

The resilience contract (§req:quality-attributes): single failures
degrade a single item or a single run, never the archive.

| Failure | Behavior |
| --- | --- |
| Network drops mid-download | Files in the temp dir that match the manifest are kept; the next attempt or run fetches only the rest |
| Malformed source metadata | Item marked `failed`, error logged, run continues |
| Checksum mismatch during download | Bounded retries, then item marked `failed` |
| Cross-filesystem archive/library | Hard error at startup; refuse to run (§spec:system-shape) |
| State DB corrupted or lost | `reconcile` rebuilds it without re-downloading (§spec:state) |
| Crash in the middle of swapping a re-fetched item into place | The next run finishes the swap: it moves the old copy back if the new one never landed, or discards the old copy if it did |
| Disk full, or another disk error while linking into the library | That item counts as failed and the run continues; no partial commits reach the archive; the run record is always closed, even on a crash — by the run itself, or by the next locked run if the process was killed (§spec:run-activity) |
| Source rate-limits | Core honors the plugin-surfaced `Retry-After` up to its back-off cap, backs off, resumes. Nothing retries underneath: each metadata or file request goes out once per core attempt; only the collection search, which has no core retry, keeps a bounded retry of its own under the same cap |
| Slow/timing-out metadata during discovery | Bounded per-item timeout + back-off; other items proceed in parallel — one slow endpoint never stalls the run (§spec:slow-metadata) |
| Discovery fails partway (pipelined) | Collection marked stale; nothing pruned — a partial enumeration is never read as items disappearing (§spec:discovery-pipeline) |
| Per-item lookup fails for an enumerated item (server error, timeout, malformed file list, any plugin exception) | Item listed failed for the run with a reason; a mirrored item keeps its recorded status and files — not pruned, not `disappeared` — a new item is `failed`; the collection is not stale; retried next run (§spec:per-item-isolation, §spec:prune-safety) |
| Two items render the same library folder | Each clashing item gets its own folder labelled with its identifier, the same on every run and restage; reported in the run summary and `status`; not a run failure (§spec:library-ownership) |
| A library path already holds a file shakedown did not place | Left untouched; that link skipped and reported for the show (§spec:library-ownership) |
| A show's library path changes (layout, metadata, replaced files, prune) | Old placed files and emptied folders removed; files shakedown did not place left and reported (§spec:library-cleanup) |
| Library tool retags files in place | Invisible to sync (§spec:sync-identity); reported by `verify --deep` on request |
| Library tool deletes staging links | Restored on next sync or `restage` |
| User wipes library tree | Rebuilt by `restage` with zero downloads |
| User wipes archive tree | DB and library invalidated; full re-download — working as intended (§spec:state) |
| Item restricted upstream | Only the files the source refuses are left out (archive.org marks each one `private`); a file it serves is always eligible. When every lossless file is refused, the best lossy copy is mirrored as `complete` and listed in `status` with its reason (a later sync mirrors the lossless files once served); when the source serves no audio at all, the local copy is retained and the status flips to `unavailable` with reason (§spec:quality-selection) |
| Source refuses a download (archive.org's 403 on a stream-only show with no restriction flag in its metadata) | Not a failure and not retried within the run: the item flips to `unavailable` with reason, the local copy (if any) is retained, and its temp directory is removed |
| Restriction lifted upstream | Detected on next discover; fetched as new |
| Source lists no audio files for an item (darked, glitch) | Never treated as a change: the archive copy is kept and the item flagged `unavailable`; it returns to `complete` when the same files are listed again |
| Item disappears upstream | Files retained by default; flagged `disappeared` (§spec:item-lifecycle) |
| Source permanently gone | Existing items retained; collection reported stale in `status` |
| Two writing commands overlap (scheduler tick, API trigger, manual run) | The second exits non-zero naming the writer lock; the first continues untouched (§spec:cli). A second `sync --collection` from the CLI or scheduler is also recorded as rescheduled, once per collection, and caught up when the first sync finishes (§spec:rescheduled-syncs) |
| A sync is skipped while the previous release's run still writes (upgrade mid-run) | Nothing is recorded and the database is not migrated under the running writer; the tick exits 1 as before (§spec:rescheduled-syncs). `status`, `serve`'s reads, and the other commands that take no lock do not migrate it either (§spec:state) |
| Config invalid when a catch-up is due | Catch-up not run; every rescheduled entry kept; error logged; the run exits non-zero (§spec:rescheduled-syncs) |
| Run killed during a catch-up | Entries not yet cleared stay; the next finishing sync catches them up (§spec:rescheduled-syncs) |
| `docker stop`, NAS shutdown, or Ctrl-C during a writing run | No new work starts; downloads in progress end at their next chunk and keep their temp folders; the run is recorded `stopped` — no recovery warning, no failure notice, no prune, rescheduled entries kept — and the command exits 143 (SIGTERM) or 130 (SIGINT); a second signal exits at once (§spec:graceful-stop) |
| `serve` stopped while a run it started is going | `serve` sends that run SIGTERM and waits up to 90 s for it to stop before exiting (§spec:graceful-stop) |
| Run killed or crashed outside `serve` (SIGKILL, OOM, host down) | Its heartbeat stops; within ~20 seconds `status` and the dashboard report it ended unexpectedly; the next run that takes the writer lock closes its record. Collections it had not started yet show nothing: they never ran (§spec:run-activity) |
| Recording progress or network activity fails (database busy) | That write is skipped; the next carries cumulative totals; the run is unaffected (§spec:run-activity) |
| `serve` down while runs happen | No dashboard meanwhile; nothing lost — runs record their own progress, network activity, and growth (§spec:run-activity, §spec:network-activity, §spec:library-stats) |
| Dashboard cannot reach `serve` | The page says so and shows the age of the data on screen (§spec:dashboard) |
| Scheduler misses runs (host down) | Next run converges; sync is idempotent at any cadence |
| Handoff target down, failing, or slow | Slow: waited up to five minutes, then recorded undelivered. Failed: notice recorded undelivered, shown in `status`, resent on the collection's next run; the sync still succeeds (§spec:durable-handoff) |
| API sync requested while a run holds the lock | `409` at once; nothing queued or rescheduled (§spec:api-sync-runs) |
| Best available copy is unplayable (SHN) | Kept archive-only; the best playable copy also mirrored and staged; with no playable copy, the SHN is staged and the show listed as not playable in `status` (§spec:quality-selection) |

## Real-source end-to-end check §spec:e2e-real-source

*Status: complete*

Delivered as `tests/test_e2e_real_source.py` (opt-in `network`
marker), with run instructions and the pinned-item replacement procedure in README.

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
  transition is exercised, not just retain (§spec:item-lifecycle).
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
