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

**The two goals.** Shakedown keeps, for every configured collection,
(1) a local *playable* copy of all the publicly available music, and
(2) an *archival-quality* copy of the music and its associated data
(notes, checksums, artwork, source metadata), in case the remote source
is ever permanently lost. When the best version a source offers is also
playable, one copy serves both goals; when it isn't (e.g. SHN), both
copies are kept.

Shakedown is the missing middle: it owns the archive layer and the seam
between archive and library, and hands off cleanly to whatever library
tool the user prefers — without that tool needing to know Shakedown
exists. A shell-script for one collection has been done before; it does
not scale to "add Phish next month and etree the month after."

**Seeing what shakedown is doing (dashboard increment, 2026-11-12).**
Once the mirror runs unattended, the operator's remaining problem is
visibility. Syncs run in short-lived containers started by the
scheduler, and a Grateful Dead sync can run for days. Today the only
windows into a run are the scheduler's log, `shakedown status`, and the
`serve` JSON/metrics endpoints — and none of them shows a run's progress
while it runs. The operator cannot tell whether a long sync is moving or
stuck, or how long is left; cannot see download speed, throttling, or
retries; and has no single place for the morning check ("what ran last
night, what failed, what is new") or for the big picture (how many
shows, how much disk, what is restricted, missing, or waiting on them).
Pointing a generic homelab dashboard (Homepage/Homarr) at `/status`
gives counters, not progress or history. The operator wants a read-only
web dashboard, usable from a phone or a desktop, that answers all of
these questions in one place. Managing config from the dashboard is a
later step, not this one.

## Success criteria §req:success-criteria

Observable outcomes that define the product as done. Each is
demonstrable end-to-end from the product's surface (CLI, config,
on-disk trees, status output). Criteria 1–10 defined v1 (shipped);
criteria 11–12 defined the discovery-performance increment (shipped);
criteria 13–15 defined the mirror-integrity & metadata increment
(shipped); criteria 16–18 defined the setup, recovery, and
validation-confidence increment (shipped; criterion 18 revised by the
ownership increment); criteria 19–26 defined the ownership, handoff,
and discovery increment (shipped); criterion 27 (operator decisions,
2026-11-11) makes a long run delay, rather than cancel, other
collections' scheduled syncs (shipped); criteria 28–33 (operator
decisions, 2026-11-12) define the current read-only web dashboard
increment. The dashboard criteria are demonstrable from the dashboard
page in a browser, compared against `shakedown status` and the run
logs.

1. **Unattended weekly mirror that actually fires.** A weekly sync of
   the IA Grateful Dead collection runs unattended for a month with no
   manual intervention and no duplicate downloads. Every scheduled tick
   actually executes its sync: the schedule does not depend on a
   long-lived container being up, and a prior run having completed and
   exited never causes a tick to be silently skipped. "The schedule is
   configured" and "the sync ran on schedule" are the same thing,
   verifiable from run logs / `status`. A tick that cannot start
   because another run holds the writer lock is not silently lost: it
   is rescheduled and caught up (criterion 27).
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
    restaging, and deletion via disappeared-item handling. It passes on a clean machine with one command
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
    triggers the library tool's work rather than waiting for it: the
    receiver acknowledges within minutes, and what the tool does
    afterwards (its import or scan) is outside shakedown. A notice
    that fails is shown in `status` (text and JSON) as undelivered, with
    the shows it covered, and is resent on later runs until it is
    delivered. A failed notice does not, by itself, make the run fail.
    Any handoff recipe the documentation ships works when followed
    literally against the stock target tool and the example compose
    stack; a recipe that cannot work is not shipped. Verifiable by
    configuring a receiver that never answers and seeing the notice
    marked undelivered within minutes, and by configuring an
    unreachable target, syncing, seeing
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
26. **Best copy kept, best playable copy in the library.** For every
    show, shakedown keeps the highest-quality version the source offers
    and the highest-quality version common players can play — one copy
    when they are the same, both when they differ (e.g. an SHN-only
    show keeps its SHN and the source's best playable copy). The
    playable copy is what appears in the library; every non-audio file
    the source lists (notes, checksums, metadata, artwork) is kept in
    the archive, and the library gets the ones the uploader provided,
    not those the source generated, nor archive files and system or
    editor leftovers (operator decisions, 2026-11-11). The operator does not pick formats per collection. Shows with
    no playable copy are listed in `status` as not playable. Shakedown
    never converts audio itself, and changing what is selected never
    re-downloads files already in the archive. Verifiable by syncing a
    FLAC show (FLAC only, plus its notes), an SHN-only Grateful Dead
    show (SHN in the archive, the best playable copy in the archive and
    library, playable in Navidrome), and a show whose lossless files
    archive.org refuses (best lossy copy kept, reason in `status`).
27. **A long run delays other collections, never cancels them.** When a
    single-collection sync (`sync --collection X`, whether ofelia
    started it or the operator typed it) cannot start because another
    run holds the writer lock, shakedown records that collection as
    rescheduled. Each collection has at most one rescheduled entry,
    keeping the time of its oldest skip: a second skip neither adds an
    entry nor moves the time. The skipped tick still fails at the
    scheduler, but its log and `status` (text and JSON) say
    "rescheduled", not an error, and name the collection and the time
    of its oldest skip — so a skip is easy to tell apart from a real
    failure, and a collection that is behind is visible.
    When any sync finishes its own work — whatever that work's result —
    it runs every rescheduled collection that was outstanding at that
    moment, oldest skip first, before it exits. Collections rescheduled
    while that catch-up runs wait for the next sync to finish. Any
    successful sync that covers a collection clears its entry: its
    catch-up, its own next tick, an all-collections sync, or an API
    "sync now". A catch-up that fails also clears the entry; the
    failure shows in `status` like any failed sync, and the
    collection's next tick retries it. An entry for a collection no
    longer in the config is dropped without running anything.
    Only single-collection syncs are rescheduled: an all-collections
    sync, an API "sync now" (still refused at once, criterion 24),
    restage, reconcile, `verify --reconform`, and item refetch refuse
    as today and leave no entry. Only a finishing sync runs a catch-up;
    restage, reconcile, reconform, and refetch exit as soon as their
    own work is done. Verifiable by holding the lock with a long sync
    of one collection, starting two other collections' syncs while it
    runs (one of them twice), and seeing: both ticks fail at the
    scheduler marked rescheduled; one entry per collection in `status`,
    the twice-skipped one keeping its first skip time; both collections
    syncing, oldest skip first, when the long sync ends; and both
    entries gone afterwards.

28. **A running sync shows live progress.** While any run is going —
    a scheduled sync, a catch-up, an API "sync now", a restage, a
    verify, a single-show refetch — the dashboard shows it: which
    collection, what kind of run, what it is doing now (for example
    discovering or downloading), shows done out of shows to do, the show
    it is working on, bytes downloaded so far, current speed, and an
    estimated time left. The numbers move on screen within a few seconds
    of the run's own progress, without reloading the page. A run that
    ends, fails, crashes, or is killed stops showing as running within
    seconds of the next refresh, and never lingers as "running".
    Verifiable by starting a long sync, watching the counts climb on a
    phone and a desktop browser, killing the sync container, and seeing
    the dashboard show the run as ended with its reason.
29. **Network activity is visible, on every source.** The dashboard
    shows, per source: current download speed and how many requests
    are in flight; a graph of download speed over time for the past 30
    days; and rate-limit hits, server errors, retries, and back-off,
    each with when it happened. The same view works for every source
    (Internet Archive, etree, and any plugin added later); nothing on
    it is specific to archive.org. The history is complete even for
    periods when the dashboard container was down or restarting: a
    sync that ran while nobody was watching still shows its speed and
    its errors afterwards. Verifiable by running a sync with the serve
    container stopped, starting serve afterwards, and seeing that
    sync's throughput and any throttling in the 30-day view.
30. **Library and database at a glance.** The dashboard shows, per
    collection and in total: shows mirrored, pending, failed,
    restricted, and disappeared; bytes in the archive and in the
    library; free space left on the data volume; and the last 30 days
    of runs, each with its kind, start, duration, result, shows added
    or updated, bytes downloaded, and errors. It shows the library's
    growth (shows and bytes) over those 30 days. Every figure that also
    appears in `shakedown status` matches it exactly.
31. **One list of what needs me.** The dashboard has a single "needs
    attention" list that names the collection or show affected and the
    action available next, covering at least: failed shows, interrupted
    or unrecovered runs, rescheduled collections and the time of their
    oldest skip, undelivered handoff notices, library collisions and
    separated recordings, and library files shakedown did not place.
    It also shows library health: restricted shows with their reason,
    disappeared shows, drift found by the last `verify --deep`, and
    shows kept as an unplayable original plus a playable copy. When
    nothing needs attention, the list says so plainly. Each entry
    matches what `shakedown status` reports for the same thing.
32. **I can tell which shakedown I am looking at.** The dashboard shows
    the running shakedown release, whether `shakedown.yaml` is valid
    right now (and, if not, the config error naming the key), and the
    result and time of the last readiness validation.
33. **The dashboard only looks.** The dashboard changes nothing: it has
    no buttons or forms that start runs, edit config, or alter state,
    and visiting it never causes a sync, a network request to a source,
    or a write to the archive or library. Watching it — even left open
    for hours on several devices — does not make a running sync or the
    NAS noticeably slower. When it cannot get fresh data, it says so
    and shows how old the data on screen is, instead of presenting old
    numbers as current.

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
  `status` and be resent on the next run, so that my library tool never
  silently misses shows. *(→ criterion 21)*
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
- **A days-long sync doesn't cost the other collections their week.**
  As a homelab operator whose Grateful Dead sync (18,432 items) can run
  for days, I want Monday's and Tuesday's collections, blocked while it
  runs, to be remembered and synced as soon as it finishes — oldest
  first — and to see in `status` which collections are waiting and
  since when, so that no collection silently goes unsynced for weeks.
  *(→ criteria 1, 27)*
- **Untagged shows still browse well.** As a homelab operator, I want
  shakedown to fill in missing artist, date, venue, and title tags from
  the source metadata it already has, so that Navidrome groups my shows
  properly — without re-downloading anything and without false drift in
  `verify --deep`. *(→ criterion 25)*
- **Play the old SHN tapes.** As a Grateful Dead collector, I want
  SHN-only shows to come with a playable version in my library while
  the original SHN stays in the archive, so that those shows are not
  dead weight in my player. *(→ criterion 26)*
- **Always the best copy, without picking formats.** As a homelab
  operator, I want shakedown to keep the best version of every show and
  the best version I can play, with all its notes and files, so that I
  have both a listening library and a preservation copy without
  writing format rules per collection. *(→ criterion 26)*
- **Is the long sync stuck?** As a homelab operator whose Grateful
  Dead sync runs for days, I want to open a page on my phone and see
  that it is moving — how many shows are done, which show it is on, how
  fast it is downloading, and roughly how long is left — so that I never
  guess from a log whether it has hung. *(→ criterion 28)*
- **The morning check.** As a homelab operator, I want one page that
  tells me what ran overnight, what failed, what is new in the library,
  and what is waiting on me, so that a ten-second glance replaces
  reading the scheduler log and running `status`. *(→ criteria 30, 31)*
- **Is the source throttling me?** As a homelab operator, I want to see
  download speed now and over the past month, and when rate limits,
  server errors, and retries happened, on whichever source they hit,
  so that I can tell a slow source from a stuck shakedown.
  *(→ criterion 29)*
- **How big is it all?** As a collector, I want to see how many shows
  each collection holds, what is restricted or gone upstream, how much
  disk the archive and library use, and how much space is left, so that
  I know the state of my mirror and a full disk never surprises me.
  *(→ criteria 30, 31)*
- **Look without fear.** As a homelab operator, I want the dashboard to
  be safe to leave open on any device in the house — it never starts or
  changes anything, never slows the sync, and never shows stale numbers
  as if they were live — so that I can trust what it tells me.
  *(→ criteria 32, 33)*

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
  pending and resent; it is never lost silently. A handoff triggers the
  library tool and waits only for its acknowledgement, never for the
  tool's own work.
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
  no-op. A tick that starts but finds another run holding the writer
  lock also fails visibly at the scheduler, and its log says it was
  rescheduled rather than failed; it is caught up when the running sync
  finishes (criterion 27). A newly deployed stack does no surprising,
  unscoped work on first bring-up: each configured collection syncs on
  its own schedule, never as an implicit "sync everything" at startup.
- **Truthful visible state.** Setup, validation, sync, restage, and
  recovery surfaces must never report success while important work was
  skipped, stale, incomplete, or unverified. The operator must be able to
  tell from the visible product surface which collection or item is
  affected and what action is available next. A collection whose
  scheduled sync was skipped shows as rescheduled, with the time of its
  oldest skip, until a sync covering it succeeds or its catch-up
  fails.
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
  auth-protected. The read-only dashboard, like the read endpoints,
  shows only operational data — never credentials or tokens.
- **Config edits take effect without restarts.** Every run — scheduled,
  manual, or API-triggered — uses the config as it is when the run
  starts.
- **The dashboard tells the same truth as everything else.** Every
  figure the dashboard shows agrees with `shakedown status`, the
  `serve` JSON, and the run logs. A disagreement is a defect, not a
  rounding choice. Data that is not fresh is labelled with its age; a
  run that has ended never shows as running. This extends "Truthful
  visible state" to the dashboard.
- **Live, but light.** Live progress reaches the screen within a few
  seconds. Watching the dashboard never noticeably slows a sync, adds
  requests to any source, or loads the NAS — including with the page
  left open for hours on more than one device. Recording progress and
  network activity for the dashboard must not weaken any existing
  guarantee: correctness, idempotency, politeness, or per-item
  isolation.
- **Nothing lost while the dashboard is down.** Runs record their own
  progress and network activity, so the 30-day history is complete
  whether or not the dashboard container was running at the time.
- **Phone and desktop alike.** Every part of the dashboard is fully
  usable on a phone and on a desktop browser; neither is a degraded
  afterthought.
- **Source-neutral observability.** Progress, network, and error views
  describe every source the same way. Adding a new source plugin makes
  it appear on the dashboard without dashboard-specific work by the
  plugin author.
- **Bounded history.** The dashboard keeps about 30 days of run and
  network history. Older detail may be dropped; the data that drives
  sync identity is never affected by that.

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
  run under the same one-writer-at-a-time rule as scheduled runs. One
  bounded exception (operator decision, 2026-11-11): shakedown
  remembers single-collection syncs that were skipped because the
  writer lock was held, and a finishing sync catches them up (criterion
  27). This is not a scheduler — no cron parser, no timing of its own,
  no runs started on its own initiative, at most one entry per
  collection, one ordering rule (oldest skip first). The
  one-writer-at-a-time rule stays: collections never sync side by side
  in separate runs.
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
  The dashboard is new in this increment; nothing depends on it yet,
  so its pages and any data it reads are free to change.
- **Governance: symphony-native.** REQUIREMENTS.md and SPEC.md are the
  repo's durable governance artifacts and ship in the PR as-is; the repo
  has no ADR/RFC/design-doc tree to convert into.
- **UI: web, track A (conventional), design state: kit and screens
  locked (2026-11-12).** One read-only web dashboard, fully usable on
  phone and desktop browsers, with a familiar admin-dashboard look from
  a standard component kit. The kit, glossary, design authority, and
  approved screens are in §spec:ui-kit. Every other surface stays as it
  is: CLI, `status`, the library tree, and the `serve` HTTP API.
- **Read-only dashboard, no config management yet (operator decision,
  2026-11-12).** This reverses the earlier "No web UI in v1" decision
  for viewing only. The dashboard starts no runs, edits no config, and
  changes no state; there is no "sync now" or "validate" button even
  though the API offers both. Managing config from the dashboard is a
  later increment. Showing each collection's next scheduled sync is
  out of this pass: the schedule stays in the compose file's ofelia
  labels. The operator may later move scheduling into the long-running
  container when config management arrives; that is a future decision,
  not part of this increment. `GET /status` stays available for
  Homepage/Homarr-style tiles.
- **The dashboard lives in the always-on serve container.** Running the
  dashboard means running the `serve` container all the time; the
  operator accepts that. It still never schedules and never starts a
  run on its own. Syncs keep running in fresh one-shot containers, and
  they work exactly as before when `serve` is not running.
- **No login of its own.** The operator's reverse proxy (for example
  Authelia) handles who may open the dashboard. Shakedown serves the
  dashboard unauthenticated, like `GET /status`, and must work correctly
  behind such a proxy. It exposes no credentials, tokens, or secrets.

## Priorities §req:priorities

Ordered by user impact. v1 shipped; a second (etree) plugin was pulled
in to prove the plugin seam. The discovery-performance increment (items
13–15), the mirror-integrity & metadata preservation increment (items
16–18), and the setup, recovery, and validation-confidence increment
(items 19–21), and the ownership, handoff, and discovery increment
(items 22–30) have shipped; the current increment is the read-only web
dashboard (items 32–36).

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

**Shipped — ownership, handoff, and discovery (post-v1).** Pre-triaged from the 2026-11-07 codebase review and the
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
27. Best available and best playable copy, with every non-audio file
    archived and the uploader's staged (→ criterion 26). 891 Grateful Dead files are SHN and unplayable
    today; the same rule replaces per-collection format settings and
    makes the refused-lossless fallback automatic on every source.
28. API "sync now" as a fresh, non-overlapping run (→ criterion 24).
    The writer lock already prevents overlap damage; this closes the
    stale-config gap and gives a clean refusal.
29. Release gate in CI before publish; no always-failing command in the
    shipped image (→ criterion 18, revised). Small and contained.
30. Reschedule lock-skipped syncs (→ criteria 1, 27). Escalated from
    production triage on 2026-11-11: a multi-day Grateful Dead sync
    makes the Monday and Tuesday collections exit 1 with nothing
    recorded, so they go unsynced until the next week, and possibly
    never while long runs keep happening. High impact and high
    confidence; small, contained effort. It reverses the earlier "no
    missed-run logic" and "missed schedules are harmless" decisions for
    this one case only. The operator chose catch-up over letting
    collections sync side by side: the one-writer rule and the shared
    archive.org request budget stay as they are.

**Next — read-only web dashboard (post-v1, current increment).**
Operator decisions, 2026-11-12. One page, phone and desktop, viewing
only. Ordered by impact on the operator's questions — "is it stuck?",
"what did last night do?", "is the source throttling me?", "how big is
it all?" — weighed against how much already exists to show:

32. Live run progress (→ criteria 28, 33). Highest impact: multi-day
    Grateful Dead syncs are the reason this increment exists, and
    nothing shows progress today. Moderate effort and confidence: runs
    must start reporting progress while they run, in short-lived
    containers, without slowing down or weakening any sync guarantee.
33. Needs attention and the recent-runs view (→ criteria 30, 31). High
    impact for the morning check and high confidence: almost everything
    it shows is already recorded and reported by `status`; the work is
    putting it on one page that matches `status` exactly.
34. Network activity, source-neutral (→ criterion 29). High impact for
    telling a slow source from a stuck run, but new recording is needed,
    it must survive the dashboard being down, and it must not lean on
    archive.org specifics while more sources are coming.
35. Library, database, and disk stats with 30-day growth (→ criterion
    30). Medium impact, high confidence: mostly counts shakedown already
    holds, plus free disk space and a growth history.
36. Version and health (→ criterion 32). Small and contained.

**Nice to have / out of scope (candidates for later):**

31. Managing config from the web UI; showing next scheduled syncs and
    possibly moving scheduling from ofelia into the long-running
    container; push notifications beyond webhooks (Pushover, Discord);
    direct library-tool integrations; multiple archive roots / tiered
    storage; BitTorrent as a source; cross-host sync. These are
    explicitly deferred. (A read-only status dashboard moved out of
    this list into items 32–36.)
