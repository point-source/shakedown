# Requirements

> Shakedown durably mirrors music collections from open archives onto a
> local NAS, keeping the on-disk archive decoupled from whatever
> library/streaming software the user points at it. This document
> captures the problem space in the user's language. Solution design
> (architecture, data model, plugin interface) lives in SPEC.md; work
> breakdown lives in ROADMAP.md. Together, this document and SPEC.md
> supersede the original PRD (retired; see git history for `prd.md`).

## Problem statement §req:problem-statement

**Target user.** A homelab operator running a NAS (QNAP or any Linux
host with Docker) alongside media services. They are comfortable with
Docker Compose and editing YAML, but do not want to write code to add a
new collection, and — on QNAP specifically — want to operate entirely
through the Container Station and File Station web UIs, with no SSH.

**The problem.** Public music archives (Internet Archive's GratefulDead
and etree collections, Phish.in, and similar) hold large bodies of
recordings worth keeping locally. Maintaining a local mirror forces two
needs into collision:

1. **Sync needs an immutable identity.** A weekly job that re-downloads
   everything is unacceptable. The tool needs a stable, durable answer
   to "do I already have this item?" that survives container
   recreations, database resets, and firmware updates.
2. **Listening needs organization.** Library tools (Beets, Lidarr,
   Picard, Navidrome's scanner) want to rename and retag files into a
   human-friendly layout — including rewriting tags in place. Whatever
   scheme they impose must not break the sync tool's idea of what is
   already on disk.

**Why current solutions fall short.** Existing tools each solve one half
but not the seam between them:

- The `internetarchive` CLI downloads and skips by checksum, but has no
  scheduling, no multi-source concept, and no notion of a "library."
- The *arr ecosystem (Lidarr et al.) handles the download→library
  handoff via hardlinks well, but assumes MusicBrainz-tagged commercial
  releases; the vast majority of archival recordings (audience tapes,
  soundboards with taper lineages, incomplete sets) have no MusicBrainz
  release, and Lidarr cannot express "every show in this collection,
  forever."
- Beets + Navidrome is the canonical library/streaming pair, but has no
  opinion about *where music comes from* or how to keep it fresh.

Shakedown is the missing middle: it owns the archive layer and the seam
between archive and library, and hands off cleanly to whatever library
tool the user prefers — without that tool needing to know Shakedown
exists. A shell-script for one collection has been done before; it does
not scale to "add Phish next month and etree the month after."

## Success criteria §req:success-criteria

Observable outcomes that define the product as done. Each is
demonstrable end-to-end from the product's surface (CLI, config,
on-disk trees, status output). Criteria 1–10 defined v1 (shipped);
criteria 11–12 defined the discovery-performance increment (shipped);
criteria 13–15 defined the mirror-integrity & metadata increment
(shipped); criteria 16–18 defined the setup, recovery, and
validation-confidence increment (shipped; criterion 18 revised by the
current increment); criteria 19–26 define the current ownership,
handoff, and discovery increment.

1. **Unattended weekly mirror that actually fires.** A weekly sync of
   the IA Grateful Dead collection runs unattended for a month with no
   manual intervention and no duplicate downloads. Every scheduled tick
   actually executes its sync: the schedule does not depend on a
   long-lived container being up, and a prior run having completed and
   exited never causes a tick to be silently skipped. "The schedule is
   configured" and "the sync ran on schedule" are the same thing,
   verifiable from run logs / `status`.
2. **New collections need no code.** Adding a second collection requires
   only YAML edits — no code changes.
3. **Fast restage.** Wiping `/data/library/` and running
   `shakedown restage` rebuilds the library tree in under five minutes
   for a ~14k-item collection, without re-downloading anything.
4. **Survives in-place retagging.** Pointing Beets at `/data/library/`
   and letting it retag files in place produces a usable music library,
   and subsequent sync cycles trigger **zero** re-downloads — even after
   every file has been retagged.
5. **Honest drift reporting.** After a library tool retags files,
   `shakedown verify --deep` reports the drift accurately (drift is
   informational, not an error); `verify --deep --reconform` restores
   the original upstream bytes for users who want byte-fidelity. (On a
   collection that opted into tag filling, reconform then refills the
   missing tags — criterion 25; users who want strict byte-fidelity
   leave tag filling off.)
6. **Web-UI-only setup on QNAP.** After creating the shared folders,
   initial setup is achievable through Container Station and File
   Station alone — no SSH. Schedule changes are made by editing the
   compose YAML in Container Station, not host cron.
7. **Restricted items are visible.** Stream-only / download-restricted
   items appear in `shakedown status` with a human-readable restriction
   reason, so the user can see what is not being mirrored and why.
8. **A second plugin is writable from the docs.** The source plugin
   interface is documented well enough that a second plugin can be
   written by someone who has not touched the IA plugin — demonstrated
   by shipping an **etree** source plugin in v1 (see Priorities).
9. **Durable against upstream takedowns.** When an item that was
   previously mirrored disappears from the source's enumeration, its
   local files are retained by default and it is flagged as
   `disappeared` in `shakedown status`; a per-collection
   `prune_disappeared: true` opt-in instead deletes the local copy for
   users who want strict mirroring.
10. **Provable end-to-end against the real source.** A single
    documented, opt-in check downloads one small public Internet
    Archive item for real and drives the typical lifecycle against it:
    sync into the archive, hardlink staging into the library tree,
    re-sorting the library by changing the layout template and
    restaging, and deletion via disappeared-item handling and
    forgetting an item. It passes on a clean machine with one command
    and is excluded from the default test run and CI, so day-to-day
    development stays offline and deterministic while releases can be
    gated on the real thing.
11. **Fast recurring sync.** A weekly sync of a large collection
    (~14k items) in which nothing has changed upstream completes in a
    small fraction of a first full sync's time — its wall-clock is
    bounded by enumerating the collection once, not by the item count —
    and it never stalls for minutes on a single slow upstream response.
    It still detects every upstream change the current
    manifest-vs-manifest comparison detects: speed never comes at the
    cost of a missed change.
12. **Polite parallel first sync.** The first full sync of a large
    collection overlaps discovery and downloading and runs multiple
    upstream requests concurrently, yet stays within the source's
    politeness ceiling as a single shared budget — so a large first
    sync is faster without ever being throttled or blacklisted, and a
    partial or failed discovery still never prunes.
13. **A metadata hiccup never deletes a present item.** On a collection
    with `prune_disappeared: true`, an item is deleted only when it is
    genuinely absent from a **successful enumeration** of the
    collection — never because fetching *that item's own* details
    failed. A run in which the collection enumerated fine but some
    items' metadata could not be fetched leaves those items' local
    files untouched and retries them next run, and does not report them
    as disappeared. Verifiable by forcing a per-item metadata failure
    on a prune-opted collection and confirming the item's archive
    survives and it is not flagged `disappeared`.
14. **A custom layout can't silently lose recordings.** A non-passthrough
    `library_layout` that cannot tell two items in the same collection
    apart (renders multiple items to the same path) is flagged at
    config-validation time, before the first sync — not discovered later
    as missing files. If items still collide during a run, the run
    completes and the rest of the sync proceeds, and a visible per-run
    summary reports every collision and where, so a lone WARNING can
    never be mistaken for a clean sync. (How a run-time collision is
    resolved — every colliding recording still reaches the library — is
    criterion 19.) Verifiable by configuring a colliding layout against a
    collection with multiple items per date/venue and seeing both the
    config-time guard and the collision report in the run summary.
15. **An opt-in, self-describing library.** A collection can opt into
    preserving each item's source metadata alongside its media — written
    into the library as a `metadata.json` sidecar carrying the item's
    full raw source metadata — so browsing the library yields the
    listing context (lineage, taper, setlist, description), not just
    audio. Preserving metadata does not disturb sync's change detection:
    an upstream edit to notes does not, on its own, trigger a re-fetch or
    replan of the item's media. Refreshing the preserved metadata is an
    explicit, user-invoked operation. Verifiable by enabling the option,
    syncing, and finding `metadata.json` hardlinked into the library
    beside the media, then running the explicit metadata-resync to
    update it.
16. **A new setup proves readiness before the first real mirror.** A
    first-time operator can validate a configured deployment and learn,
    from the product's visible surface, whether the config, archive and
    library paths, source access, credentials, and handoff targets are
    usable before a large sync begins. Failures name the affected
    collection or setting and the user action needed to continue; a
    broken setup never looks like a clean no-op. Verifiable by checking
    a fresh config with a missing path, bad source access, and a valid
    minimal collection, and seeing fast, actionable pass/fail output
    without downloading a full collection.
17. **A broken run is recoverable without guesswork.** When a sync,
    restage, reconcile, or validation flow fails partway through, the
    next visible status tells the operator what completed, what did not,
    which collection or item needs attention, and whether a retry is
    safe. Local recordings remain intact unless the user explicitly
    requested deletion, and a later successful retry clears the failure
    state rather than leaving stale warnings. Verifiable by forcing a
    network, source, storage, or config failure mid-flow, then rerunning
    and confirming no local recording is lost and status does not report
    success until the affected work is complete.
18. **Release validation proves the workflows users rely on.** A
    bounded validation exercises first-setup readiness, unsafe config
    rejection, partial-failure recovery, and the ordinary sync-to-library
    path, and it must pass before any release image is published — a
    release whose validation fails never reaches the NAS. The check
    finishes quickly enough to gate every release, does not require
    inspecting internals to understand failure, and fails loudly when a
    user-facing workflow is broken rather than passing because it skipped
    the risky scenario. The release check is a maintainer/CI concern: it
    uses synthetic data only (no real media, no operator config), so the
    shipped product carries no release-check command, and in particular
    no command that always fails inside the shipped image. Readiness of
    a real deployment remains criterion 16's job. Verifiable by
    publishing a release with a deliberately broken workflow and seeing
    publication blocked with the failure tied to that workflow, and by
    finding no command in the shipped image that fails by construction.
19. **Every library folder has exactly one owner.** When two distinct
    recordings render to the same library folder or file, both still
    reach the library: shakedown separates them by distinguishing the
    clashing paths with each recording's identifier. Which recording
    lands where is the same on every run and after a full restage — it
    never flips with run order — and no recording ever silently replaces
    or merges into another. Each separation is reported in the run
    summary and visible in `status`, naming both recordings and the
    paths they received. Verifiable by configuring a layout that renders
    two items to one folder, syncing twice and restaging, and seeing both
    recordings in separate folders at the same paths every time, with
    the clash reported.
20. **The library mirrors the archive, with no stale copies.** After a
    layout change synced without a restage, a re-fetch that brings new
    metadata, a metadata refresh, an upstream rename, or a prune, the
    library holds each show only at its current path: the old folders
    and files are gone, and a pruned show's bytes are actually freed
    rather than kept alive by a leftover library link. Shakedown removes
    only what it placed in the library; anything else found there is
    left alone and reported. The first run after upgrading clears the
    duplicates already present on the NAS (e.g. Aquarium Rescue Unit
    shows linked into two folders) without re-downloading them.
    Verifiable by changing the layout and syncing (no restage), then
    finding no library file that is no longer linked to the archive and
    no show present at two paths.
21. **Handoff notices are delivered or visibly pending.** A handoff
    command (such as a library import) may run as long as it needs; a
    long-running import completes rather than being cut off. A notice
    that fails is shown in `status` (text and JSON) as undelivered, with
    the shows it covered, and is resent on later runs until it is
    delivered. A failed notice does not, by itself, make the run fail.
    Any handoff recipe the documentation ships works when followed
    literally against the stock target tool and the example compose
    stack; a recipe that cannot work is not shipped. Verifiable by
    configuring a handoff that takes several minutes and seeing it
    finish, and by configuring an unreachable target, syncing, seeing
    the undelivered notice in `status`, fixing the target, and seeing
    the next run deliver it.
22. **One bad item fails alone, on every source.** A server error,
    timeout, or malformed file listing for one item marks only that item
    failed for the run (and retries it next run); every other item in
    the collection still syncs, and the collection is not reported
    stale. This holds for every shipped source (Internet Archive and
    etree alike) and for any plugin written from the documented plugin
    interface without extra effort by its author. Verifiable by
    injecting a per-item server error and a malformed listing into a
    collection of many items and seeing only those items failed, the
    rest synced, and the collection current in `status`.
23. **Single-show operations behave like a sync of that show.**
    Refetching one show, or reconforming shows, looks up only the shows
    it acts on — never the whole collection — respects the source's
    request limits, is recorded as a run in `status`, and sends handoff
    notices for what it fetched exactly as a sync would. Verifiable by
    refetching one identifier in a large collection and seeing a handful
    of upstream requests, a recorded run, and a handoff notice.
24. **"Sync now" from the API uses the current config and never
    overlaps.** A sync triggered through the optional `serve` API runs
    with the configuration as it is at the moment of the request, so a
    config edit takes effect without restarting `serve`. If another run
    already holds the collection, the request is refused immediately
    with a clear "a run is in progress" response, rather than queuing or
    overlapping. Verifiable by editing the config and triggering a sync
    that reflects the edit, and by triggering a sync while a scheduled
    run is in progress and getting the refusal.
25. **Shows browse properly in a tag-driven player.** A collection can
    opt in to having shakedown fill in missing tags — artist, date,
    venue, title — from the source metadata it already holds, so that
    shows group and display correctly in Navidrome. Only absent tags are
    filled; a tag already present (from the source or from the user's
    own tool) is never overwritten. Tagging never triggers a re-download,
    and an unchanged show stays unchanged: when the source's listing
    still matches what was recorded and each file on disk still matches
    the checksum recorded after tagging, nothing happens. `verify --deep`
    treats shakedown's own tagging as expected (no drift) while still
    reporting changes made by anything else. `verify --deep --reconform`
    restores the source's exact bytes and then fills the gaps again on
    tag-enabled collections. Verifiable by enabling tagging on a
    collection with untagged files, syncing, seeing the shows grouped by
    artist and date in Navidrome, then running sync and `verify --deep`
    again and seeing no re-download and no drift.
26. **Shows in unplayable formats still play.** When a show's media
    exists only in a format common players cannot read (e.g. SHN), the
    original is still mirrored to the archive, and the source's own
    playable alternative (when it offers one) is also mirrored and is
    what appears in the library. Shows with no playable alternative are
    listed in `status` as not playable. Shakedown never converts audio
    itself. Verifiable by syncing a SHN-only Grateful Dead show and
    finding the SHN files in the archive and a playable version in the
    library, playable in Navidrome.

## User stories §req:user-stories

Concrete workflows in the user's language. Each traces to a success
criterion and implies a testable path through the product's surface.

- **Mirror a collection.** As a homelab operator, I want to configure
  the IA Grateful Dead collection once and have it sync weekly, so that
  my library tool picks up new shows automatically without me re-running
  anything. *(→ criteria 1, 6)*
- **Add another collection.** As a homelab operator, I want to add a
  second collection (e.g. Phish) by dropping config into
  `shakedown.yaml`, so that I grow my mirror without touching code.
  *(→ criterion 2)*
- **Reorganize the library, then change my mind.** As a homelab
  operator, I want to wipe the library tree and regenerate it from the
  archive, so that I can experiment with layouts without re-downloading
  a single byte. *(→ criterion 3)*
- **Retag freely.** As a homelab operator, I want to point Beets/Picard
  at the staging tree and let them rewrite tags in place, so that I get a
  clean library — and I expect Shakedown's next sync to notice nothing
  and re-download nothing. *(→ criterion 4)*
- **Reconform to upstream when I care.** As a preservationist, I want to
  run `verify --deep` to see which files have drifted from upstream and
  optionally `--reconform` to restore original bytes, so that I can keep
  a byte-faithful mirror when it matters (e.g. to re-seed). *(→ criterion 5)*
- **See what I'm not getting.** As a homelab operator, I want
  `shakedown status` to list restricted, stream-only items with the
  reason, so that I understand the gaps in my mirror. *(→ criterion 7)*
- **Keep copies the source removes.** As a homelab operator, I want
  items that disappear from the source to stay on my disk by default —
  flagged in `status` but never deleted — with an opt-in
  `prune_disappeared` per collection for strict mirroring, so that an
  upstream takedown never costs me a recording I already have.
  *(→ criterion 9)*
- **Switch library tools.** As a homelab operator, I want to point a
  different library tool at the staging tree without the archive layer
  changing, so that switching from Beets to Lidarr costs me nothing on
  the archive side. *(→ criterion 4)*
- **Add a new source type.** As a technically inclined user, I want to
  write a source plugin (e.g. etree) against a documented interface and
  register it, so that Shakedown can mirror a source the authors never
  built for. *(→ criterion 8)*
- **Recover from a lost database.** As a homelab operator, I want
  `shakedown reconcile` to rebuild sync state from the archive tree and
  source metadata, so that a corrupted or reset database never forces a
  full re-download. *(→ criterion 1)*
- **Hand off to my library tool automatically.** As a homelab operator,
  I want a successful sync to poke my library tool — via a webhook, or
  by executing a command like `beet import` when I prefer — so that new
  shows flow into my library without me watching. *(→ criteria 1, 2, 21)*
- **Never miss a show because a notice failed.** As a homelab operator,
  I want a handoff that failed or was unreachable to show up in
  `status` and be resent on the next run, and a long import to be
  allowed to finish, so that my library tool never silently misses
  shows. *(→ criterion 21)*
- **Copy the docs and it works.** As a homelab operator, I want any
  handoff example in the docs to work against the stock tool when I
  follow it literally, so that I don't discover weeks later that it
  imported 64 tracks out of 58,000. *(→ criterion 21)*
- **Prove it works for real before I ship.** As a maintainer preparing
  a release, I want to run one opt-in check that downloads a small real
  IA item and exercises the typical lifecycle end-to-end — sync,
  hardlink staging, re-sorting via a layout change and restage, and
  deletion — so that I catch real-API drift and integration breakage
  that offline fixtures cannot, without slowing or destabilizing the
  everyday test run. *(→ criterion 10)*
- **Quick weekly check.** As a homelab operator, I want my weekly sync
  to finish quickly when nothing new has been posted, so that a no-op
  run doesn't take anywhere near as long as the first full mirror — and
  I still trust it to have caught anything that did change.
  *(→ criterion 11)*
- **One slow item doesn't hang the run.** As a homelab operator, I want
  a single slow or timing-out upstream response to not freeze my whole
  sync for minutes, so that one bad metadata endpoint can't hold the
  rest of the collection hostage. *(→ criterion 11)*
- **Bearable first sync.** As a homelab operator, I want the first sync
  of a big collection to start downloading while it's still discovering,
  so that I'm not waiting through thousands of metadata lookups before
  anything begins fetching. *(→ criterion 12)*
- **Stay in the archive's good graces.** As a homelab operator, I want
  Shakedown to stay within the source's politeness limits even when it
  works in parallel, so that speeding up a sync never gets me
  rate-limited or blacklisted by archive.org. *(→ criterion 12)*
- **Never lose a recording to a hiccup.** As a homelab operator who
  opted into pruning, I want an item whose metadata momentarily fails to
  fetch to stay on disk and be retried next run, so that a transient
  upstream glitch never deletes a recording that is still in the
  collection. *(→ criterion 13)*
- **Warned before my own layout loses data.** As a homelab operator, I
  want to be told — when I configure a layout that can't tell two
  recordings of the same show apart — before my first sync, and to get a
  clear count of anything dropped to a collision when a run does hit one,
  so that I never mistake a lossy layout for a working sync.
  *(→ criterion 14)*
- **Keep the liner notes.** As an archival-music collector, I want to opt
  a collection into saving each item's source metadata next to the audio,
  so that lineage/taper/setlist/description context lives in my library
  and not only in a database I might lose. *(→ criterion 15)*
- **Refresh notes on demand.** As a homelab operator, I want an explicit
  way to re-pull source metadata for items I have already mirrored, so
  that I can update the preserved notes without re-downloading audio and
  without every routine run re-checking them. *(→ criterion 15)*
- **Prove setup before the first mirror.** As a first-time operator, I
  want to validate my config, paths, source access, credentials, and
  handoff targets before I start a large sync, so that setup mistakes
  fail fast with an action I can take instead of becoming a silent no-op
  or a long run that was doomed from the start. *(→ criterion 16)*
- **Change config without risking my archive.** As a homelab operator, I
  want a layout, source, or collection change to be checked before it can
  hide, skip, or delete recordings, so that experimenting with
  configuration does not require me to inspect internals or trust raw
  logs. *(→ criteria 16, 17)*
- **Recover from a broken run.** As a homelab operator, I want status
  after a failed sync or restage to tell me what is safe to retry and
  what still needs attention, so that recovery does not involve guessing
  whether my archive is complete. *(→ criterion 17)*
- **Gate a release on real operator workflows.** As a maintainer, I want
  a bounded validation that proves setup, unsafe-config handling,
  partial-failure recovery, and sync-to-library behavior still work to
  run automatically before any image publishes, so that I do not ship a
  release that only passed narrow unit checks — and my NAS never runs a
  product command that fails by construction. *(→ criterion 18)*
- **Browse my library straight from the folders.** As a homelab
  operator using Navidrome as my primary player, I want shakedown's
  library to be good to browse on its own — every recording present,
  each show in exactly one place, no leftover duplicates — so that I
  don't need a separate organizer just to make it usable.
  *(→ criteria 19, 20)*
- **Two tapes of one show both stay.** As a homelab operator, I want two
  recordings that my layout puts in the same folder to both land in the
  library, separated the same way every run, and to be told it
  happened, so that I never lose a tape to a layout clash or see it
  move between runs. *(→ criterion 19)*
- **Change the layout without cleanup.** As a homelab operator, I want
  old folders to disappear when a show moves — after a layout change, a
  re-fetch, a rename, or a prune — so that my player never shows the
  same show twice and pruned shows actually free their space.
  *(→ criterion 20)*
- **One broken item doesn't stall the collection.** As a homelab
  operator, I want a single item that errors or has a broken file list
  to fail on its own while the rest of the collection keeps syncing, so
  that my collection never goes stale behind one bad item.
  *(→ criterion 22)*
- **Fix one show without walking the collection.** As a homelab
  operator, I want refetching or reconforming a show to touch only that
  show, count as a run, stay polite to the source, and notify my
  library tool, so that one-show fixes are quick and show up like any
  other sync. *(→ criterion 23)*
- **Trigger a sync after editing config.** As a homelab operator, I want
  the API's "sync now" to use the config as I just edited it and to
  refuse cleanly if a run is already going, so that I never restart
  `serve` to pick up a change or end up with two runs at once.
  *(→ criterion 24)*
- **Untagged shows still browse well.** As a homelab operator, I want
  shakedown to fill in missing artist, date, venue, and title tags from
  the source metadata it already has, so that Navidrome groups my shows
  properly — without re-downloading anything and without false drift in
  `verify --deep`. *(→ criterion 25)*
- **Play the old SHN tapes.** As a Grateful Dead collector, I want
  SHN-only shows to come with a playable version in my library while
  the original SHN stays in the archive, so that those shows are not
  dead weight in my player. *(→ criterion 26)*

## Quality attributes §req:quality-attributes

Non-functional expectations in the user's terms; these are the
constraints that drive architecture.

- **Durability of sync state.** "Do I already have this?" must survive
  forever — across container recreation, database reset, and QTS
  firmware updates — and must never be invalidated by anything the
  library layer does to the files. The answer comes from recorded state,
  never from re-hashing on-disk bytes during sync.
- **Idempotency.** Running sync twice in a row does nothing the second
  time. Running sync after a partial failure resumes correctly. Running
  sync at any cadence (including after missed scheduled runs) converges
  to the same result.
- **Durable mirroring against upstream loss.** The mirror is durable
  in the takedown direction: an item vanishing from the source never
  deletes the user's local copy unless they have explicitly opted into
  pruning. Losing content is only ever the user's choice, never an
  upstream side effect. Pruning is driven solely by an item's genuine
  absence from a **successful enumeration** of the collection. A failure
  to fetch a *single item's* details is not evidence the item is gone
  and never makes it prune-eligible; when enumeration succeeds but some
  item descriptions fail, those items are left intact and retried, not
  deleted.
- **No silent library data loss.** The library never silently drops a
  recording it successfully fetched. If a configured layout cannot
  represent two distinct items distinctly, that is surfaced — guarded at
  config time and, if a run still collides, reported in a visible
  per-run summary — never left as a lone warning buried in the logs. A
  run-time collision is resolved by separating the recordings, never by
  dropping, replacing, or merging one into another, and never in a way
  that depends on run order.
- **The library is a faithful, deterministic view of the archive.**
  Every library path has exactly one owning recording; the same archive
  state and config always yield the same library, whether reached by
  incremental syncs or by a full restage. Nothing stale outlives the
  show it belonged to, and shakedown never deletes a library file it
  did not place.
- **Storage efficiency.** Archive + library staging together cost
  effectively 1x storage. Any further duplication is a library-tool
  decision, not Shakedown's — with one accepted exception: mirroring a
  source's playable alternative for shows whose original format is
  unplayable (criterion 26). Tag filling changes files in place and
  adds no copies.
- **Durable handoff.** A handoff notice is either delivered or visibly
  pending and resent; it is never lost silently, and a slow but working
  target is given the time it needs.
- **Restage speed.** Rebuilding the full library tree for a ~14k-item
  collection completes in under five minutes and never re-downloads.
- **Sync speed.** A recurring sync's wall-clock scales with what
  actually changed upstream, not with the size of the collection: a
  no-op weekly run does not re-check every item one at a time. The
  first full sync overlaps discovery and downloading rather than fully
  enumerating before it fetches. No single slow upstream response
  blocks the rest of the run. Concrete targets are a design decision
  (SPEC), not fixed here; the requirement is the direction — fast when
  little changed, and never stalled by one slow endpoint.
- **Speed never costs correctness.** Any discovery fast-path must never
  fail to detect an upstream change that the manifest-vs-manifest
  comparison detects today. The "trust recorded state,
  manifest-vs-manifest" identity below remains load-bearing;
  performance work sits on top of it and must not weaken it. A partial
  or failed discovery must still never trigger pruning.
- **Politeness to the source (hard).** Regardless of internal
  parallelism, Shakedown holds to the source's connection and rate
  limits through a single shared budget spanning discovery and
  downloads — the two must not sum past the source's ceiling. It must
  never get the user throttled or blacklisted. This is a proactive
  bound, beyond the reactive back-off in Resilience below.
- **Resilience.** Network drops, malformed source metadata, checksum
  mismatches, disk-full, and source rate-limiting each fail cleanly
  (retry, back off, or mark the single item failed) without corrupting
  the archive or aborting the whole run. No partial commits reach the
  archive tree. Per-item isolation is a property of the product, not of
  each source plugin: it holds equally for every shipped source and for
  third-party plugins written from the docs.
- **Scheduled runs are observable and self-contained.** Every scheduled
  tick launches a fresh, self-contained run; it never relies on
  attaching to a container that is already running. A tick that fails to
  start at all — image missing, config missing, unknown collection —
  surfaces as a visible error at the scheduler layer, never as a silent
  no-op. A newly deployed stack does no surprising, unscoped work on
  first bring-up: each configured collection syncs on its own schedule,
  never as an implicit "sync everything" at startup.
- **Truthful visible state.** Setup, validation, sync, restage, and
  recovery surfaces must never report success while important work was
  skipped, stale, incomplete, or unverified. The operator must be able to
  tell from the visible product surface which collection or item is
  affected and what action is available next.
- **Fast feedback for setup and validation.** Mistakes that can be
  detected before a large mirror begins should surface quickly and
  locally, without waiting for a full collection walk or large download.
  Release validation should be bounded enough to run deliberately before
  publishing, while still proving the workflows that users rely on.
- **Operability without SSH.** All day-to-day operation and
  configuration on QNAP is achievable through Container Station and File
  Station. Failures are visible at the scheduler / container-exit layer,
  not buried in a daemon.
- **Security of secrets.** Source credentials (e.g. IA email/password)
  are supplied only via environment variables or secrets files, never
  written into the YAML config. Any ad-hoc trigger API is
  auth-protected.
- **Config edits take effect without restarts.** Every run — scheduled,
  manual, or API-triggered — uses the config as it is when the run
  starts.

## Constraints §req:constraints

Technical, operational, and scope bounds on the solution space.

- **Same-filesystem archive and library.** The archive and library
  trees must live on one filesystem so the seam between them is
  filesystem-native and zero-overhead; this is enforced at startup with
  a clear error if violated.
- **Trust recorded state, not disk bytes.** Sync's answer to "do I have
  it?" is a comparison of the source's current manifest against the
  manifest recorded at fetch time — never a hash of on-disk bytes. Byte
  hashing happens only in the explicit, non-scheduled `verify --deep`.
- **Sync state co-located with the archive.** Sync state lives with the
  archive tree so that backing up the archive backs up the sync state.
- **Single-user, single-host.** Not multi-tenant; sharing a library
  across users is the streaming server's job.
- **One-shot CLI, external scheduler.** Shakedown is a one-shot command,
  stateless between runs; scheduling is the deployment's job (ofelia in
  the compose stack), so all timing lives in one place (the compose
  YAML) and Shakedown stays out of the scheduler business. Because runs
  are one-shot and stateless, the scheduler must launch a fresh
  container per tick rather than exec into a persistent one; the
  deployment need not — and should not be required to — keep an idle
  Shakedown container running between ticks. An optional `serve` command
  may expose a small auth-protected control plane (health, status,
  metrics, trigger), but never schedules. A triggered sync is an ordinary
  run under the same one-writer-at-a-time rule as scheduled runs.
- **Deploys as one Docker Compose stack.** The whole deployment —
  including scheduling — is one compose file paste-able into Container
  Station.
- **Source-agnostic by design.** IA in v1, but the design must
  accommodate additional sources (etree in v1, and later Phish.in,
  generic HTTP/RSS, torrent) without core changes. A second plugin
  (etree) ships in v1 to prove the seam.
- **Library handoff = webhook with exec fallback.** After a successful
  sync, Shakedown can notify the library tool via a webhook (default,
  loosely coupled) or by executing a configured command (e.g.
  `beet import`) as a built-in fallback. Deciding the webhook payload
  contract is part of the design. The handoff is optional: Navidrome
  browsing shakedown's library directly is the primary listening path,
  and Beets is no longer assumed to be part of the stack (the operator
  will decide after seeing the library in Navidrome). Any documented
  handoff recipe must target an interface the named tool actually
  offers in its stock form.
- **Network-dependent verification is opt-in and bounded.** The default
  test run and CI are fully offline and deterministic. The real-data
  end-to-end check is invoked explicitly by a developer, downloads a
  single small public item (a few megabytes) to stay polite to the
  upstream archive, and its flakiness never blocks pull requests.
- **Readiness and release checks stay bounded.** Setup validation and
  release validation may touch configured paths, credentials, source
  availability, and handoff endpoints, but they must not turn into an
  unbounded mirror of a large collection. They prove readiness and
  recovery behavior before the expensive work starts.
- **Preserved metadata is context, not media.** When a collection opts
  into preserving source metadata, the sidecar lands in the library like
  any mirrored file, but it does not participate in the media manifest
  that drives change detection — an upstream metadata change never, by
  itself, re-fetches media or replans the item. Refreshing preserved
  metadata is a distinct, explicitly-invoked operation, not something a
  routine sync does on its own.
- **Not a library manager, discovery tool, transcoder, or general
  downloader.** Shakedown does not retag or rename inside the library
  beyond its own layout, serve audio, recommend/scrobble, transcode, or
  wrap arbitrary downloaders. Those jobs belong to
  Beets/Lidarr/Picard/Navidrome and are out of scope. Two bounded
  exceptions: (1) opt-in, per-collection gap-filling of basic tags
  (artist, date, venue, title) from source metadata shakedown already
  holds — it only adds missing tags and never overwrites existing ones;
  (2) mirroring a source-provided playable alternative for media in an
  unplayable format. Shakedown never converts audio itself.
- **Tagged bytes are recorded state.** When tag filling changes a file,
  the post-tag checksum becomes part of shakedown's recorded state.
  Sync's change detection still compares only source listing against
  recorded listing (tagging never causes a re-download or a re-hash of
  the source), and `verify --deep` compares disk against the recorded
  post-tag checksum.
- **Deployment status.** Deployed — a single operator's QNAP NAS install
  in daily use (live shakedown as of 2026-11-09); no external consumers
  (no API clients, no published packages, no other installs). Every
  surface is free to break: config keys and their shape, library
  layout, CLI flags, the HTTP API, and the state database (which may be
  migrated or even rebuilt via `reconcile`). The one hard limit is
  data: an upgrade must never force re-downloading the bulk of media
  already in the archive. Relocating or renaming archive and library
  files is acceptable, and re-downloading a small share is tolerable.
- **Governance: symphony-native.** REQUIREMENTS.md and SPEC.md are the
  repo's durable governance artifacts and ship in the PR as-is; the repo
  has no ADR/RFC/design-doc tree to convert into.
- **UI: none.** Scope is limited to the CLI, `status` output, the
  on-disk library tree, and the optional `serve` HTTP API; no visual
  user interface is in scope (see "No web UI" below).
- **No web UI in v1.** The goal is a backend service, not a dashboard;
  users who want a UI can point Homepage/Homarr at the status endpoint.

## Priorities §req:priorities

Ordered by user impact. v1 shipped; a second (etree) plugin was pulled
in to prove the plugin seam. The discovery-performance increment (items
13–15), the mirror-integrity & metadata preservation increment (items
16–18), and the setup, recovery, and validation-confidence increment
(items 19–21) have shipped; the current increment is ownership,
handoff, and discovery (items 22–29).

**Must have (v1 core):**

1. Immutable, durable sync identity — manifest-vs-manifest planning,
   never disk hashing during sync (the load-bearing decision;
   → criterion 4).
2. IA source plugin: discover, fetch (idempotent, checksum-verified),
   verify.
3. Hardlink staging tree with user-configurable per-collection layout
   templates; collisions surfaced as errors, not silent overwrites
   (→ criteria 3, 4).
4. `sync`, `status`, `restage`, `reconcile`, `verify [--deep [--reconform]]`,
   and single-item operations as the CLI surface.
5. Multi-collection config via a single YAML file, secrets via env only
   (→ criterion 2).
6. Restricted / disappeared item handling: both surfaced in `status`,
   disappeared items retained by default with an opt-in
   `prune_disappeared` per collection (→ criteria 7, 9).
7. QNAP-targeted Docker Compose deployment (Shakedown + ofelia +
   library tool + streaming server), web-UI-only setup (→ criterion 6).
8. Library handoff via webhook, with an exec fallback.

**Should have (v1, proves the design):**

9. A documented source plugin interface and a **second plugin (etree)**
   written against it to demonstrate the seam is real (→ criterion 8).
10. Optional `serve` control plane (health, status, metrics, auth-
    protected trigger).
11. Opt-in real-data end-to-end check: one small public IA item through
    sync → staging → re-sort/restage → deletion, runnable with one
    command, excluded from the default test run and CI (→ criterion 10).

**Shipped — discovery performance (post-v1):**

13. Incremental recurring sync: a no-op weekly sync no longer scales
    its wall-clock with the item count, and does so without weakening
    change detection — every change caught today is still caught
    (→ criterion 11; GH #8).
14. Parallel, pipelined first sync under one shared politeness budget:
    overlap discovery and download with bounded concurrency that spans
    both, never exceeding the source's ceiling, with prune safety
    preserved (→ criterion 12; GH #9).
15. No head-of-line stalls: a single slow upstream metadata response
    never freezes the whole run for minutes (→ criterion 11; GH #10).

**Shipped — mirror integrity & metadata preservation (post-v1). Three
independently-shippable items addressed in one pass:**

16. Prune-safety hardening: a per-item metadata-fetch failure never
    makes a still-enumerated item prune-eligible; pruning follows only
    genuine absence from a successful enumeration (→ criterion 13;
    GH #13).
17. Lossy-layout protection: a config-time guard for layouts that cannot
    distinguish items, plus a visible per-run summary of any recordings
    dropped to template collisions, without collisions aborting the rest
    of the run (→ criterion 14; GH #14).
18. Opt-in metadata sidecars: per-collection preservation of source
    metadata as a `metadata.json` sidecar in the library, excluded from
    media change-detection, with an explicit metadata-resync operation
    to refresh it (→ criterion 15; GH #15).

**Shipped — setup, recovery, and validation confidence (post-v1). Three
independently-shippable items addressed in one pass:**

19. First-setup readiness: a user can validate paths, config, source
    access, credentials, and handoff targets before the first large
    mirror begins, with fast, actionable failures instead of unclear
    exits or silent no-ops (→ criterion 16; GH #19).
20. Recovery truthfulness: failed or interrupted sync, restage,
    reconcile, and validation flows leave visible, retryable state and
    never report clean success while work is incomplete or recordings
    are at risk (→ criterion 17; GH #20).
21. Bounded release validation: maintainers have a documented pre-release
    check that exercises setup readiness, unsafe-config handling,
    partial-failure recovery, and sync-to-library behavior with clear
    pass/fail output (→ criterion 18; GH #21).

**Next — ownership, handoff, and discovery (post-v1, current
increment).** Pre-triaged from the 2026-11-07 codebase review and the
2026-11-09 live-NAS diagnosis. Ordered by impact on the operator's
day-to-day listening, which today is Navidrome browsing shakedown's
library folders directly:

22. Library ownership and stale-copy cleanup (→ criteria 19, 20).
    Highest impact and highest confidence: the duplicates and merged
    tapes are visible in Navidrome now (3,263 Aquarium Rescue Unit files
    linked into two folders), and both criteria rest on the same notion
    of which recording owns which library path. Ship together.
23. Per-item isolation on every source (→ criterion 22). A single bad
    item can freeze a whole collection forever; not active on the NAS
    today (etree is commented out) but it blocks re-enabling etree and
    is the foundation item 24 builds on.
24. Single-show operations as runs (→ criterion 23). Cheap once item 23
    exists; makes refetch/reconform polite, visible, and handed off.
25. Durable handoff and a working documented recipe (→ criterion 21).
    Lower immediate impact while Navidrome is primary and Beets is
    optional, but a silently lost notice is a correctness defect and the
    shipped example currently cannot work.
26. Tag filling (→ criterion 25). High listening impact (only ~33–50%
    of sampled Grateful Dead / Aquarium Rescue Unit files are tagged)
    but moderate confidence and effort: it is the first time shakedown
    changes archive bytes, so the recorded-state rules must hold.
27. Playable alternatives for SHN-only shows (→ criterion 26). 891
    Grateful Dead files are SHN; meaningful for that collection, narrow
    elsewhere.
28. API "sync now" as a fresh, non-overlapping run (→ criterion 24).
    The writer lock already prevents overlap damage; this closes the
    stale-config gap and gives a clean refusal.
29. Release gate in CI before publish; no always-failing command in the
    shipped image (→ criterion 18, revised). Small and contained.

**Nice to have / out of scope (candidates for later):**

30. Web UI for status and config; push notifications beyond webhooks
    (Pushover, Discord); direct library-tool integrations; multiple
    archive roots / tiered storage; BitTorrent as a source; cross-host
    sync. These are explicitly deferred.
