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
  "removed upstream on <date>; kept" (text: a count per collection;
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

*Status: implemented*

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

*Status: implemented*

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

*Status: complete — the config-time guard, and runtime separation
(§spec:library-ownership).*

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

*Status: complete, including the core-owned listing/lookup contract
(§spec:per-item-isolation).*

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

Plugins shipped in v1:

- **Internet Archive (`ia`).** Wraps the official `internetarchive`
  Python package for search and download — those problems are solved;
  the plugin does not reimplement them. That package checksums only
  files already on disk before a download, never the bytes it just
  wrote, so the plugin checks each downloaded file's size and md5
  against the manifest before reporting success. Optional credentials
  (for download-restricted items) are read from environment variables
  only.
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

*Status: implemented, including the core's file-selection rule
(§spec:quality-selection).*

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
  they hold exactly the show's track count and no track twice. The
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

*Status: complete, including path ownership, clash separation, and
stale-copy cleanup (§spec:library-ownership, §spec:library-cleanup).*

Per collection, the user provides a `library_layout` template that
renders each item's **staging directory** under the library tree, from
fields the source plugin surfaces (e.g.
`{year}/{date} - {venue}/{identifier}`). The item's original archive
filenames are then hardlinked into that directory unchanged.

- **Templates produce directories, never renamed files.** Filenames in
  the deepest directory always match the archive, so tools (and users)
  can correlate the two trees by name. Renaming and retagging are the
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
  is staged into that folder name with ` [<identifier>]` appended to its
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

A collection may opt in (`fill_tags`, default off, §spec:configuration)
to having shakedown fill in missing basic tags from the source metadata
it already holds, so shows group and display correctly in a tag-driven
player such as Navidrome (§req:success-criteria #25, §req:constraints:
the bounded gap-filling exception; tagged bytes are recorded state).

Observable behavior:

- **What is filled.** For each audio file in a format the system can
  tag (FLAC, MP3, Ogg Vorbis; other files, including SHN, are left
  untouched), the system fills only tags that are absent — a tag whose
  values are all empty or whitespace counts as absent (operator
  decision, 2026-11-10):
  - artist and album artist ← the show's creator;
  - album ← `<date> – <venue>` (the show's title when venue or date
    is missing; the identifier when the title is missing too — never
    `unknown`). When two or more
    shows that shakedown tags would end up with the same album artist,
    album, and date — which a tag-driven player merges into one album —
    each of them gets ` [<identifier>]` appended, by the same
    every-clasher rule as library folders (§spec:library-ownership).
    (Operator decision, 2026-11-10.)
  - date ← the show's date; venue ← the show's venue, in the format's
    custom venue field;
  - track title and track number ← the per-file values the source lists
    for that file, when it lists them; otherwise left absent.

  A file whose existing tags the tagging library cannot write back as
  they are (some archive.org MP3s carry a malformed link frame that
  cannot be saved as ID3v2.3, which mutagen rejects as an invalid
  value) is left exactly as it is, with a warning
  in the run log; the show still completes and is not retried for that
  file. Shakedown never rewrites or drops another party's frame to
  make room for its own tags (§spec:per-item-isolation). A disk fault
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

## Verification and drift §spec:verify-drift

*Status: implemented, including reconform as single-show runs
(§spec:single-show-runs), comparison against post-tag checksums, and
refilling tags after a reconform (§spec:tag-filling).*

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
  collection, has its missing tags filled again afterwards. Only the
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
| `status [--json]` | Last run per collection with its kind (sync, refetch, or reconform), item counts by status, archive size (from the recorded manifests — never a walk of the archive tree), restricted items with reasons, shows removed upstream and kept (each with the date it was first found missing, §spec:item-lifecycle), known drift, library separations in effect (§spec:library-ownership), library files shakedown did not place (§spec:library-cleanup), undelivered handoff notices (§spec:durable-handoff), and shows not playable (§spec:quality-selection). |
| `verify [--source S] [--collection C] [--deep] [--list] [--reconform] [--yes]` | Integrity check (§spec:verify-drift). |
| `restage [--source S] [--collection C]` | Rebuild the staging tree from the archive without downloading. |
| `reconcile` | Rebuild the state database from the archive tree plus current source manifests (§spec:state). |
| `validate [--source S] [--collection C] [--live-handoff] [--json]` | Preflight a configured deployment before a large mirror begins, optionally exercising handoff targets with an explicit test payload (§spec:setup-readiness-validation). |
| `item show \| refetch <identifier>` | Single-item inspection and refetch; `refetch` is a single-show run (§spec:single-show-runs). |
| `serve [--host] [--port]` | Optional HTTP control plane (§spec:serve). |

- Commands shall exit non-zero on failure so failures surface at the
  scheduler/container layer (§req:quality-attributes: operability).
- A `--source` or `--collection` that names nothing configured is a
  usage error (exit 2) listing the configured names — never a run that
  does nothing and exits 0 (§req:quality-attributes: scheduled runs are
  observable).
- Writing commands — `sync` (except `--dry-run`), `restage`,
  `reconcile`, `item refetch`, and `verify --reconform`, whether run from
  the CLI, the scheduler, or `serve` — hold one exclusive writer lock
  (a kernel `flock` on `<state_db>.lock`). A second writer exits non-zero
  at once, naming the lock, instead of racing the first over shared temp
  folders. The kernel releases the lock when the holder exits, even on a
  crash, so a stale lock file never blocks the next run.
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

*Status: implemented, including filling tags after a fetch on a `fill_tags`
collection (§spec:tag-filling).*

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

*Status: implemented, including the removal of the format settings
(§spec:quality-selection) and `fill_tags` (§spec:tag-filling).*

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
`library_layout`, `prune_disappeared`, `incremental_discovery`,
`preserve_source_metadata`, `fill_tags`, `on_complete`); and global
`notifications`. The ownership, handoff, and discovery increment adds
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
  `email_env: IA_EMAIL`) rather than holding values, so the config
  file is safe to back up, share, and commit.
- **There is no `schedule:` field.** Timing lives entirely in the
  deployment (ofelia labels in the compose file, §spec:deployment).
  **Why:** one place to answer "when does what run," and Shakedown
  stays out of the scheduler business — no cron parser, no missed-run
  logic, no drift between two sources of timing truth.

## State persistence and recovery §spec:state

*Status: implemented, including the library placement, pending-handoff, and
tag-filling records of the ownership, handoff, and discovery increment
(below).*

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
  Per run: its kind (a sync, refetch, or reconform), counts, bytes
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
in the URL or command:

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
  collection, "handoff not delivered: N shows since <time>" with the
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
| `POST /sync?source=&collection=` | Start an ad-hoc sync and answer at once (auth required; §spec:api-sync-runs) |
| `POST /verify` | Start a verify run and answer at once (auth required; §spec:api-sync-runs) |
| `POST /validate` | Run setup readiness validation; live handoff validation is auth required |

`POST /sync`, `/verify`, and `/validate` answer `404`, naming the
configured sources or collections, when `source` or `collection` names
nothing configured.

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
- **Answers at once.** When the new run has taken the writer lock and
  opened its run records, `serve` answers `202 Accepted` with the scope
  it started and those records' ids — one per collection the sync
  covers; a verify opens none (operator decision, 2026-11-10). Progress
  and the result are read from `GET /status` like any run's, where each
  collection's latest run carries its id; the run record always closes,
  even if the run crashes or is killed (§spec:failure-behavior).
- **Refuses immediately when busy.** If another run holds the writer
  lock, the request is refused with `409 Conflict` and the message "a
  run is in progress; try again when it finishes". Nothing is queued.
- `POST /verify` follows the same pattern (fresh run, `202`, `409` when
  a reconform cannot take the lock). `POST /validate`, `GET /status`,
  and `GET /metrics` read the config as it is at the request.
- **Config errors are answers, not crashes.** If the edited config is
  invalid, each endpoint that needs it answers `503` with the config
  error naming the offending key (§spec:configuration; operator
  decision, 2026-11-10); `serve` keeps running
  so the operator can fix the file and retry. Auth (bearer token) and
  unknown-scope `404` behave as before (§spec:serve).
- **The run counter keeps its meaning.** `serve` watches each sync it
  started and counts it in `shakedown_sync_runs_total` as `ok` or
  `fail` when it exits; a refused (`409`) trigger and a verify are not
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
  Every Shakedown container — ofelia jobs, `serve`, manual runs — runs
  as one configured user, so no path creates files another cannot
  write (ofelia's own default is root).
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
(`/share/data/{archive,library,music,shakedown-config}`), and every
Shakedown container mounts that share once as `/data`, so archive and
library share a filesystem *and* a mount — Linux refuses hardlinks
across separate mounts (§spec:system-shape).

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
from the ofelia container's log, which records each tick's start, stop,
output, and failure (the per-tick containers are deleted when they
finish). Missed schedules are harmless
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
  no-SSH deployments. Mutating live handoff tests remain protected by
  the same bearer-token posture as ad-hoc sync and verify
  (§spec:serve).

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
  completes it.

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

*Status: complete — `tests/test_release_workflows.py`, run by CI's
`test` job, which the image-publishing `docker` job needs.*

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

## Failure behavior §spec:failure-behavior

*Status: complete*

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
| Disk full, or another disk error while linking into the library | That item counts as failed and the run continues; no partial commits reach the archive; the run record is always closed, even on a crash |
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
| Two writing commands overlap (scheduler tick, API trigger, manual run) | The second exits non-zero naming the writer lock; the first continues untouched (§spec:cli) |
| Scheduler misses runs (host down) | Next run converges; sync is idempotent at any cadence |
| Handoff target down, failing, or slow | Slow: waited up to five minutes, then recorded undelivered. Failed: notice recorded undelivered, shown in `status`, resent on the collection's next run; the sync still succeeds (§spec:durable-handoff) |
| API sync requested while a run holds the lock | `409` at once; nothing queued (§spec:api-sync-runs) |
| Best available copy is unplayable (SHN) | Kept archive-only; the best playable copy also mirrored and staged; with no playable copy, the SHN is staged and the show listed as not playable in `status` (§spec:quality-selection) |

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
