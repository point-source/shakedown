# Requirements

> Shakedown durably mirrors music collections from open archives onto a
> local NAS, keeping the on-disk archive decoupled from whatever
> library/streaming software the user points at it. This document
> captures the problem space in the user's language. Solution design
> (architecture, data model, plugin interface) lives in SPEC.md; work
> breakdown lives in ROADMAP.md. Source PRD: `prd.md`.

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

Observable outcomes that define v1 as done. Each is demonstrable
end-to-end from the product's surface (CLI, config, on-disk trees,
status output).

1. **Unattended weekly mirror.** A weekly sync of the IA Grateful Dead
   collection runs unattended for a month with no manual intervention
   and no duplicate downloads.
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
   the original upstream bytes for users who want byte-fidelity.
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
  shows flow into my library without me watching. *(→ criteria 1, 2)*

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
- **Storage efficiency.** Archive + library staging together cost
  effectively 1x storage. Any further duplication is a library-tool
  decision, not Shakedown's.
- **Restage speed.** Rebuilding the full library tree for a ~14k-item
  collection completes in under five minutes and never re-downloads.
- **Resilience.** Network drops, malformed source metadata, checksum
  mismatches, disk-full, and source rate-limiting each fail cleanly
  (retry, back off, or mark the single item failed) without corrupting
  the archive or aborting the whole run. No partial commits reach the
  archive tree.
- **Operability without SSH.** All day-to-day operation and
  configuration on QNAP is achievable through Container Station and File
  Station. Failures are visible at the scheduler / container-exit layer,
  not buried in a daemon.
- **Security of secrets.** Source credentials (e.g. IA email/password)
  are supplied only via environment variables or secrets files, never
  written into the YAML config. Any ad-hoc trigger API is
  auth-protected.

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
  YAML) and Shakedown stays out of the scheduler business. An optional
  `serve` command may expose a small auth-protected control plane
  (health, status, metrics, trigger), but never schedules.
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
  contract is part of the design.
- **Not a library manager, discovery tool, transcoder, or general
  downloader.** Shakedown does not tag, rename inside the library, serve
  audio, recommend/scrobble, transcode, or wrap arbitrary downloaders.
  Those jobs belong to Beets/Lidarr/Picard/Navidrome and are out of
  scope.
- **No web UI in v1.** The goal is a backend service, not a dashboard;
  users who want a UI can point Homepage/Homarr at the status endpoint.

## Priorities §req:priorities

Ordered by user impact. v1 is the target; a second (etree) plugin is
pulled in to prove the plugin seam.

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
6. Restricted / disappeared item handling surfaced in `status`
   (→ criterion 7).
7. QNAP-targeted Docker Compose deployment (Shakedown + ofelia +
   library tool + streaming server), web-UI-only setup (→ criterion 6).
8. Library handoff via webhook, with an exec fallback.

**Should have (v1, proves the design):**

9. A documented source plugin interface and a **second plugin (etree)**
   written against it to demonstrate the seam is real (→ criterion 8).
10. Optional `serve` control plane (health, status, metrics, auth-
    protected trigger).

**Nice to have / out of scope for v1 (candidates for later):**

11. Web UI for status and config; push notifications beyond webhooks
    (Pushover, Discord); direct library-tool integrations; multiple
    archive roots / tiered storage; BitTorrent as a source; cross-host
    sync. These are explicitly deferred.
