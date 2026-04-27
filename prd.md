# Shakedown — Product Requirements Document

**Status:** Draft v0.1
**Last updated:** 2026-04-26
**Owner:** [you]

> *"Nothing shaking on Shakedown Street — used to be the heart of town."* A self-hosted tool for durably mirroring music collections from open archives onto a local NAS, while keeping the on-disk archive decoupled from whatever library/streaming software you point at it.

---

## 1. Problem & motivation

Public music archives — Internet Archive's GratefulDead and etree collections, Phish.in, and similar — contain large bodies of recordings worth keeping locally. Two needs collide when trying to maintain a local mirror:

1. **Sync needs immutable identity.** A weekly job that re-downloads everything is unacceptable. It needs a stable answer to "do I already have this item?" that survives forever.
2. **Listening needs organization.** Library tools (Beets, Lidarr, Picard, Navidrome's own scanner) want to rename, retag, and reshape files into a human-friendly layout. Whatever scheme they impose must not break the sync tool's idea of what's already on disk.

Existing tooling solves each half but not the seam:

- The `internetarchive` Python CLI handles downloading and checksum-based skip, but has no scheduling, no multi-source concept, and no awareness of "library."
- The *arr ecosystem (Lidarr et al.) handles the download → library handoff via hardlinks beautifully, but assumes MusicBrainz-tagged commercial releases and doesn't model archival collections.
- Beets + Navidrome is the canonical library/streaming pair but has no opinion about *where music comes from* or how to keep it fresh.

**Shakedown is the missing piece in the middle.** It owns the archive layer and the hardlink seam between archive and library. It hands off cleanly to whatever library tool the user prefers, without that tool needing to know Shakedown exists.

---

## 2. Goals & non-goals

### Goals

- Maintain a local mirror of one or more remote source collections, refreshed on a configurable schedule.
- Make "have I already downloaded this?" a property of the archive layer alone — never invalidated by anything the library layer does.
- Present a clean, library-ready directory tree (via hardlinks) that any music library tool can consume without modification.
- Be source-agnostic in design: Internet Archive in v1, but the abstraction should accommodate Phish.in, etree.org, Bandcamp collections, and arbitrary HTTP/torrent sources later.
- Run as a single Docker Compose stack on a QNAP (or any Linux host with Docker).
- Survive QTS firmware updates, container recreations, and database resets without losing sync state or re-downloading content.

### Non-goals

- **Not a library manager.** Shakedown doesn't tag, doesn't rename inside the library, doesn't serve audio. Beets/Lidarr/Picard/Navidrome do those jobs.
- **Not a discovery tool.** Shakedown syncs collections the user explicitly configures. It doesn't recommend, scrobble, or follow artists.
- **Not a transcoder or quality-upgrader.** What's on the source is what lands on disk. Format selection is configurable but per-source, not adaptive.
- **Not a general-purpose downloader.** Out of scope: yt-dlp wrappers, torrent automation beyond what a source plugin needs, paid/DRM'd content.
- **Not multi-tenant.** Single user, single host. Sharing a library across users is the streaming server's job.

---

## 3. Users & use cases

**Primary user:** homelab operator running a NAS with media services. Comfortable with Docker Compose, willing to edit YAML configs, doesn't want to write code to add a new collection.

**Use cases (in priority order):**

1. **"Mirror the Grateful Dead collection on Internet Archive."** Configure once, runs weekly, library tool picks up new shows automatically.
2. **"Add the Phish collection too."** Drop in a second source config, no code changes.
3. **"Reorganize my library, then change my mind."** Wipe the library tree, regenerate it from the archive without re-downloading a byte.
4. **"Switch from Beets to Lidarr."** Point the new tool at the staging tree; archive layer is untouched.
5. **"Add a new source type that doesn't exist yet."** Write a source plugin, register it, done.

---

## 4. Architectural overview

Three logical layers, two on-disk trees, one process:

```
┌─────────────────────────────────────────────────────────────────┐
│  Remote sources (IA, Phish.in, etree, ...)                      │
└────────────────────────────┬────────────────────────────────────┘
                             │  fetch
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  ARCHIVE LAYER  /data/archive/<source>/<collection>/<item>/     │
│  - Immutable from user perspective                              │
│  - Filenames mirror source identifiers exactly                  │
│  - Source of truth for "do I have it?"                          │
│  - Shakedown's state DB lives here                               │
└────────────────────────────┬────────────────────────────────────┘
                             │  hardlink (same filesystem)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGING / LIBRARY-READY TREE  /data/library/<source>/...       │
│  - Human-readable layout                                        │
│  - Library tool (Beets/Lidarr/etc.) reads from here             │
│  - Library tool may further reorganize into its own root        │
└────────────────────────────┬────────────────────────────────────┘
                             │  (library tool's own pipeline)
                             ▼
                    Streaming server (Navidrome, etc.)
```

The crucial property: **the inode is shared between archive and library.** Renaming or deleting the library-side link does not affect the archive-side file or Shakedown's record of having downloaded it. Library tools that further reorganize (e.g., Beets moving the file to its own managed root) likewise don't touch the archive copy.

### Why hardlinks specifically (not symlinks, not copies)

- **Symlinks** break when the library tool moves the target file into its own root (Beets does this by default). The library ends up with dangling references.
- **Copies** double storage, and changes to one copy don't propagate — defeating the purpose.
- **Hardlinks** make both paths first-class references to the same bytes. Either can be deleted without affecting the other; the inode is freed only when the last link goes away. Filesystem-native, zero overhead.

The constraint hardlinks impose: **archive and library trees must be on the same filesystem.** This is enforced at startup with a clear error.

---

## 5. Integrity model: surviving library tools that rewrite tags

This is the load-bearing design decision of the whole project, and it's worth dwelling on because the obvious approach is wrong.

### The trap

The naive design says: "every week, run `ia download --checksum` to re-verify and fetch what's missing." The IA CLI compares each local file's MD5 against IA's stored manifest and re-downloads on mismatch.

This breaks the moment a library tool retags a file. Beets writing Vorbis comments, Picard fixing genre tags, Lidarr updating MusicBrainz IDs — any of these mutate the file's bytes, which mutates its MD5, which means a checksum comparison says "I don't have this." The next sync re-downloads the file. The library tool retags it again. The sync re-downloads. Forever.

Hardlinks don't help and don't hurt: because the archive copy and the staging copy share an inode, a tag rewrite via either path mutates both — but that's fine, because we're not going to look at on-disk bytes during sync.

### The fix: trust the database, not the disk

Shakedown's answer to "do I have this item?" is **never** "let me hash the file and compare." It's "let me look in my state DB."

At download time, Shakedown records:
- The source's manifest (IA's file list with MD5s and sizes)
- Which files were fetched, where they were placed, and the MD5s **as reported by the source**
- The fetch timestamp

On subsequent sync runs:
- Shakedown asks the source for the current manifest
- Compares it to **the manifest it recorded at fetch time**, not to bytes on disk
- An item is "have it" if the source manifest matches the recorded manifest and the expected file paths still exist (existence check only — no hashing)
- An item is "fetch it" if the source manifest is new, has new files, or shows changed MD5s upstream

This is exactly how Lidarr, Sonarr, and Radarr handle their imported-file tracking. They record what they imported and trust their own DB; they don't re-hash every file weekly to figure out what they have.

### Library tools are free to mutate files in place

Because sync never looks at on-disk bytes, a library tool retagging a hardlinked file is invisible to Shakedown. Beets, Picard, Lidarr, and friends can all be pointed at `/data/library/` and configured however the user prefers — including in-place tag writing. The DB stays consistent; the manifest comparison stays correct; sync stays idempotent.

This is the simplest possible model and it's robust by design: **drift is allowed, not feared.**

### When you do want to reconform to upstream

Some users will want their archive copy to be byte-identical to upstream — for example, to re-seed to IA, to run a public mirror, or just out of preservationist instinct. For these cases, Shakedown provides:

```
shakedown verify --deep [--reconform]
```

- Without `--reconform`: hashes on-disk files, compares against recorded manifests, reports drift. Most users will see "47 files have drifted, probably from tag writes" and shrug. This is informational, not an error.
- With `--reconform`: drifted files are re-fetched from source, overwriting the local copy with original bytes. Use this if byte-level fidelity to upstream matters more to you than retained tag work.

`verify --deep` is **not scheduled by default**. It's an explicit, occasional operation.

### Storage cost

Archive + library staging via hardlinks = effectively 1x storage. The library tool may further copy/move into its own root if configured that way (e.g., Beets with `import.copy: yes`), making it 2x — but that's a library-tool decision, not a Shakedown one. Users who want the library tool to operate in place on the staging tree can do so; users who want a separate Beets-managed root can do that. Shakedown is agnostic.

---

## 6. Data model

### Source (configured)
- `name`: short identifier, used in paths (e.g., `internetarchive`, `phishin`)
- `type`: which plugin handles it (e.g., `ia`, `phishin`)
- `collections`: list of collection definitions

### Collection (configured)
- `name`: short identifier, used in paths (e.g., `grateful-dead`)
- `query`: source-specific query (for IA: a search expression like `collection:GratefulDead`)
- `format_filters`: which file types to download (e.g., `["flac", "shn"]`)
- `exclude_filters`: patterns to skip (e.g., derived MP3s, IA's auto-generated formats)
- `library_layout`: path template for the staging tree (see §8)
- `prune_disappeared`: bool, default false (see §9)
- `on_complete`: optional webhook to poke after successful sync

### Item (discovered, persisted)
- `source_name` + `collection_name` + `identifier`: composite primary key
- `archive_path`: where it lives in the archive tree (null if never fetched)
- `discovered_at`, `downloaded_at`, `last_verified_at`
- `status`: see status enum below
- `restriction_reason`: human-readable explanation when `status = unavailable` (e.g., "stream-only soundboard per band request")
- `source_metadata`: JSON blob from the source (IA `_meta.xml` parsed, etc.)
- `recorded_manifest`: source's file list with sizes and MD5s **as recorded at fetch time** — this is the source of truth for "do I have it?" (see §5)

#### Status enum
- `pending` — discovered but not yet attempted
- `downloading` — fetch in progress
- `complete` — successfully fetched, manifest recorded
- `failed` — fetch attempted and failed; will retry
- `unavailable` — source explicitly disallows download (e.g., IA stream-only items). Surfaced in `status` output so the user knows what's not being mirrored. Re-checked occasionally in case restrictions change.
- `disappeared` — was previously `complete`, but no longer in source's enumeration. Local files retained by default (see §9 prune option).

### Run (operational)
- `id`, `source_name`, `collection_name`, `started_at`, `finished_at`
- `items_discovered`, `items_new`, `items_updated`, `items_failed`
- `bytes_downloaded`, `errors[]`

State lives in SQLite at `/data/archive/.shakedown/state.db`. Co-locating it with the archive means a backup of the archive tree includes sync state.

---

## 7. Source plugin interface

A source plugin is the abstraction that lets v1 ship with IA only but accept Phish.in/etree/etc. without core changes.

A plugin implements:

```
discover(collection_config) -> Iterator[ItemDescriptor]
    # Enumerate all items currently matching the collection's query.
    # Returns lightweight descriptors with at minimum: identifier,
    # source-native metadata, list of available files.

fetch(item_descriptor, dest_dir, format_filters) -> FetchResult
    # Download files matching filters into dest_dir.
    # Must be idempotent: re-running on an existing dir verifies
    # checksums and skips matching files.

verify(item_descriptor, archive_path) -> VerifyResult
    # Cheap re-check that what's on disk still matches the source.
    # Used for periodic integrity passes, not every sync.
```

Plus a manifest declaring what kinds of queries the plugin understands and what file metadata it can surface (date, venue, lineage, etc.) to feed the library layout templates.

The IA plugin wraps the official `internetarchive` Python package. It does not reimplement search, download, or checksum verification — those are solved.

A future Phish.in plugin would hit the Phish.in REST API. A future generic-HTTP plugin could ingest from any RSS/Atom feed of audio files. The point is the seam is well-defined.

---

## 8. Library staging layout

Per source/collection, the user provides a path template that Shakedown uses to materialize hardlinks under `/data/library/`. The template draws fields from the source plugin's surfaced metadata.

For IA Grateful Dead, a sensible default:

```
/data/library/internetarchive/grateful-dead/
  {year}/
    {date} - {venue|sanitize}/
      [{lineage_short}]/
        {original_filename}
```

Critical design choices:

- **Templates produce directories, not retagged files.** Shakedown doesn't touch ID3/Vorbis tags. The library tool does that.
- **Filenames inside the deepest directory match the archive.** So library tools can match files between archive and staging by name if they want to.
- **Sanitization is conservative.** Slashes, colons, and other filesystem-hostile characters are replaced; non-ASCII is preserved.
- **Collisions are an error, not a silent overwrite.** If two items would produce the same staging path, Shakedown surfaces it for the user to disambiguate the template.

Users who don't want any opinion can set `library_layout: passthrough` and get hardlinks mirroring the archive structure exactly.

---

## 9. Sync workflow

For each scheduled run of a (source, collection):

1. **Discover.** Plugin enumerates current items matching the collection query. Returns lightweight descriptors with current source manifests.
2. **Plan.** For each remote item, compare its current source manifest against the `recorded_manifest` in Shakedown's state DB:
   - Not in DB → `new`
   - In DB, source manifest matches recorded → `unchanged` (skip — no disk hashing)
   - In DB, source manifest differs from recorded → `changed-upstream`
   - Source flagged it as restricted → `unavailable` (record reason, don't fetch)
   For each item in DB, also check: was it in the discover results? If not → `disappeared`.
3. **Fetch.** For `new` and `changed-upstream`, plugin downloads to a temp dir within the archive tree, verifying source-provided checksums on the way down. On full success, atomically rename the temp dir to its final archive location and persist the manifest to the DB. Partial failures leave the temp dir for next-run cleanup.
4. **Stage.** For items now in the archive, render the library layout template and create hardlinks. If a hardlink target already exists and points to the same inode, leave it. If it points to a different inode, that's a collision — log and skip.
5. **Notify.** Optionally hit a webhook or trigger a downstream tool (e.g., `beet import` on the new staging directory). v1: webhook only; v2: pluggable post-actions.
6. **Record.** Update state DB with run results.

**Idempotency is non-negotiable.** Running sync twice in a row does nothing the second time — the manifest comparison sees everything as `unchanged`. Running sync after a partial failure resumes correctly. Running sync on a fresh DB against an existing archive uses `reconcile` (§10) to rebuild state from the source's current manifests + on-disk presence — never by re-downloading.

**Critical:** the integrity check in Plan is **manifest-vs-manifest**, never **manifest-vs-disk-bytes**. This is what makes the system robust to library tools that retag files (see §5). The only operation that hashes on-disk bytes is `verify --deep`, which is an explicit drift-detection tool, not part of normal sync.

**Disappeared items are not deleted locally** by default. The whole point is durable mirroring; if IA pulls a show, the user keeps their copy. A per-collection `prune_disappeared: true` option exists for users who want strict mirroring.

---

## 10. CLI surface

Shakedown is a one-shot CLI. The container's default entrypoint is the binary itself; scheduling is the deployment's job (see §12 — ofelia in compose).

```
shakedown sync [--source S] [--collection C] [--dry-run]
    Run a sync now. Defaults to all configured collections.
    This is what ofelia (or cron, or a human) invokes on a schedule.

shakedown status [--json]
    Show last run per collection, item counts, disk usage,
    pending-restricted items, and currently-known drift (if any).

shakedown verify [--source S] [--collection C] [--deep] [--reconform]
    --deep: hash on-disk files, compare against recorded manifests, report drift.
            Drift is not an error — it usually means a library tool retagged files.
    --reconform: with --deep, additionally re-fetch drifted files from source,
                 overwriting local copies with original bytes.
    Not scheduled by default. Run when you care.

shakedown restage [--source S] [--collection C]
    Rebuild the staging hardlink tree from the archive without re-downloading.
    Used when changing library_layout templates.

shakedown reconcile
    Walk the archive tree and rebuild state DB from scratch.
    Used for disaster recovery or migrating from a manual setup.

shakedown item show <identifier>
shakedown item refetch <identifier>
shakedown item forget <identifier>
    Single-item operations.
```

Keeping the binary one-shot has real benefits:

- The container is stateless between runs. State lives in the SQLite DB on disk; the process exits cleanly when sync is done.
- Failures are visible at the scheduler layer (ofelia logs, container exit codes) rather than buried inside a long-running daemon.
- Testing is easy: every command is just `shakedown <verb> --flags`, no daemon to stand up.
- It matches how the *arr ecosystem and most homelab cron-style tools work.

For the rare user who wants on-demand triggers without `docker exec`, an optional `shakedown serve` command exposes a small HTTP control plane:

- `GET /healthz` — liveness
- `GET /status` — JSON of `shakedown status`
- `GET /metrics` — Prometheus metrics
- `POST /sync?source=X&collection=Y` — trigger an ad-hoc sync (auth-protected)
- `POST /verify` — trigger a verify run (auth-protected)

`serve` is a separate container in the compose if used. It does not schedule; ofelia still does that. It only provides an external API for triggering work and reading state. It's optional and most users won't need it.

No web UI in v1 — the goal is to be a backend service, not another dashboard to babysit. Users who want a UI can point Homepage/Homarr at the `/status` endpoint.


---

## 11. Configuration

Single YAML file, mounted into the container as `/config/shakedown.yaml`. The user edits this file via QNAP File Station's text editor (or any tool that can write to a share); no shell access required.

```yaml
archive_root: /data/archive
library_root: /data/library
state_db: /data/archive/.shakedown/state.db

# Global concurrency caps
max_concurrent_downloads: 4   # across all collections
max_concurrent_collections: 2 # parallel collection syncs (when invoked together)

sources:
  - name: internetarchive
    type: ia
    auth:
      # only needed for download-restricted items
      email_env: IA_EMAIL
      password_env: IA_PASSWORD
    collections:
      - name: grateful-dead
        query: 'collection:GratefulDead'
        format_filters: [flac, shn]
        exclude_filters: ['*_64kb.mp3', '*_vbr.mp3']
        library_layout: |
          {year}/{date} - {venue|sanitize}/[{lineage_short}]/{filename}
        on_complete:
          webhook: http://beets:8337/import?path={staging_path}

      - name: phish
        query: 'collection:Phish'
        format_filters: [flac]

notifications:
  on_failure:
    webhook: http://ntfy:80/shakedown
```

Note: there's no `schedule:` field per collection. Scheduling lives in the compose file as ofelia labels (see §12). This keeps Shakedown out of the scheduler business and puts all timing information in one place.

Secrets via env vars only, never in the YAML. Env vars are configured in Container Station's "Environment" tab when creating/editing the application.

---

## 12. Deployment shape (QNAP-targeted)

Designed for setup entirely through Container Station's web UI — no SSH required at any step.

### Folder layout

Single share, all paths under it for hardlink compatibility:

```
/share/data/
├── archive/                   ← Shakedown's domain
│   ├── .shakedown/
│   │   └── state.db
│   ├── internetarchive/
│   └── phishin/               (future)
├── library/                   ← Shakedown's hardlink targets
│   ├── internetarchive/
│   └── phishin/
├── music/                     ← Library tool's managed root
│                                  (Beets/Lidarr writes here, Navidrome reads here)
└── shakedown-config/
    └── shakedown.yaml         ← edited via File Station
```

### Containers

- `shakedown` — one-shot CLI. The container's image is the binary; ofelia invokes `shakedown sync` against it on a schedule. Container is "running" only during a sync.
- `ofelia` — scheduler. Reads schedule definitions from labels on the compose file, executes commands inside the `shakedown` container at the right times. No code, no separate UI to configure — it's just labels in YAML.
- `beets` (or whatever library tool) — reads from `/data/library/`, manages `/data/music/`. Free to retag in place; sync doesn't care (see §5).
- `navidrome` — reads `/data/music/` read-only.

Why ofelia rather than cron: ofelia is a container in the same compose stack, so the entire deployment is one YAML file. Configuration is done via Docker labels, which means schedule changes are made by editing the same compose YAML the user already pasted into Container Station — no host cron, no separate scheduling UI, no SSH. Ofelia also handles things like missed runs across container restarts and exposes its own logs through Docker, which makes "did the Sunday sync actually happen?" a one-click question in Container Station.

Compose sketch:

```yaml
services:
  shakedown:
    image: shakedown:latest
    volumes:
      - /share/data/archive:/data/archive
      - /share/data/library:/data/library
      - /share/data/shakedown-config:/config:ro
    environment:
      - IA_EMAIL_FILE=/run/secrets/ia_email
      - IA_PASSWORD_FILE=/run/secrets/ia_password
    # No restart policy — container exits when sync completes

  ofelia:
    image: mcuadros/ofelia:latest
    depends_on: [shakedown]
    command: daemon --docker
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    labels:
      ofelia.job-exec.weekly-sync.schedule: "0 0 3 * * 0"  # Sun 3am
      ofelia.job-exec.weekly-sync.container: "shakedown"
      ofelia.job-exec.weekly-sync.command: "shakedown sync"

  beets:
    # ... library tool of choice
  navidrome:
    # ... streaming server
```

The schedule lives as ofelia labels, not as a `schedule:` field inside `shakedown.yaml`. Per-collection scheduling is done by adding more ofelia jobs with `--collection` flags:

```yaml
ofelia.job-exec.dead-sync.schedule: "0 0 3 * * 0"
ofelia.job-exec.dead-sync.command: "shakedown sync --collection grateful-dead"
ofelia.job-exec.phish-sync.schedule: "0 0 4 * * 0"
ofelia.job-exec.phish-sync.command: "shakedown sync --collection phish"
```

This is more verbose than embedding schedules in `shakedown.yaml` would be, but it puts all the "when does what run" information in one place (the compose file) and gets us out of the scheduler-implementation business.

### Setup steps (no SSH)

1. **File Station:** create `/share/data/{archive,library,music,shakedown-config}` directories.
2. **File Station:** upload an initial `shakedown.yaml` to `/share/data/shakedown-config/`.
3. **Container Station → Applications → Create:** paste compose YAML (including the ofelia labels), set environment variables (IA credentials), click Create.
4. **File Station** to edit `shakedown.yaml` for source/collection changes; **Container Station** to edit compose YAML for schedule changes; **Container Station** for logs.

Day-to-day operation and configuration is web UI only.

---

## 13. Failure modes & resilience

| Failure | Behavior |
|---|---|
| Network drops mid-download | Resume on next run; partial files in temp dir cleaned up |
| Source returns malformed metadata | Item marked `failed`, error logged, sync continues |
| Checksum mismatch during download | Retry up to N times, then mark `failed` |
| Hardlink creation fails (cross-FS) | Hard error at startup, refuse to run |
| State DB corrupted | `reconcile` command rebuilds from archive tree + source metadata |
| Disk full | Sync aborts cleanly, no partial commits to archive tree |
| Source rate-limits us | Plugin honors `Retry-After`, backs off, resumes |
| User changes library template | `restage` rebuilds staging without re-downloading |
| User wipes library tree manually | Next sync (or `restage`) rebuilds it |
| User wipes archive tree | DB and library both invalidated; full re-download required (working as intended) |
| Library tool rewrites tags in place | No effect on sync — Shakedown trusts manifest comparison, not on-disk hashing. `verify --deep` will report drift if run; `--reconform` will restore upstream bytes if user wants that. |
| Library tool deletes a hardlink in staging | Staging restored on next sync (or `restage`) |
| Source becomes permanently unavailable | Existing items retained; new items fail to discover; collection marked stale |
| Item transitions from downloadable → restricted upstream | Local copy retained; status flips to `unavailable` on next discover |
| Restricted item becomes downloadable | Detected on next discover; fetched as `new` |
| Ofelia missed a scheduled run (host down) | Next ofelia run picks up where it left off; sync is idempotent regardless of how often it runs |

---

## 14. Open questions

### Resolved (recorded here for posterity)

- ~~**Authentication for restricted IA items.**~~ Resolved: items the source disallows are persisted as `status: unavailable` with a `restriction_reason`, surfaced in `shakedown status`, and re-checked occasionally in case restrictions lift.
- ~~**Concurrent collection syncs.**~~ Resolved: `max_concurrent_downloads` and `max_concurrent_collections` are global config knobs (§11).
- ~~**Source plugin distribution.**~~ Resolved: built-in only for v1. Plugin interface formalization is a v2 concern.
- ~~**Metadata refresh cadence.**~~ Resolved: `shakedown verify --deep` exists for this and is **not scheduled by default**; users opt in if they want it.
- ~~**Embedded vs. external scheduler.**~~ Resolved: external (ofelia in compose). Ofelia is itself a container, so the entire stack — including scheduling — is configurable through Container Station's compose YAML paste, with no SSH required and no scheduler logic in Shakedown.
- ~~**Library tool retags files in place.**~~ Resolved: allowed. Sync uses manifest-vs-manifest comparison and never hashes on-disk bytes during sync. Drift is reported by `verify --deep` and corrected (if desired) by `verify --deep --reconform`.

### Still open

- **Webhook vs. direct invocation for library handoff.** Webhook is loosely coupled and pluggable; direct invocation (e.g., shelling out to `beet import`) is simpler. Lean toward webhook with a built-in "exec" fallback. Need to decide before v1 ships.
- **Drift detection output format.** With in-place tag writes allowed, drift is expected and common — every show Beets touches will drift. `verify --deep` should probably default to a one-line summary ("47 files drifted, run with `--list` for details") rather than dumping a wall of paths. The `--reconform` action presumably wants confirmation before overwriting.
- **Webhook payload contract.** Need to nail down what fields go in the `on_complete` webhook body (item identifier, archive path, staging path, source metadata excerpt?) so library-tool integrations are stable.

---

## 15. Out of scope for v1, candidate for later

- Web UI for status and configuration
- Push notifications beyond webhooks (Pushover, Discord, etc.)
- Direct integration with library tools (vs. just webhook-and-pray)
- Multiple archive roots / tiered storage (hot SSD, cold HDD)
- BitTorrent as a source type
- Cross-host sync (e.g., one Shakedown pushing to multiple NASes)

---

## 16. Success criteria

v1 is done when:

1. A weekly sync of the IA Grateful Dead collection runs unattended for a month with no manual intervention and no duplicate downloads.
2. Adding a second collection requires only YAML edits — no code changes.
3. Wiping `/data/library/` and running `shakedown restage` rebuilds it in under five minutes for a 14k-item collection.
4. Pointing Beets at `/data/library/` and letting it retag files in place produces a usable music library, and subsequent sync cycles don't trigger any re-downloads in Shakedown — even after Beets has rewritten tags on every file.
5. Running `shakedown verify --deep` after Beets has retagged files reports the drift accurately. Running `verify --deep --reconform` restores upstream bytes.
6. Initial QNAP setup (post-share-creation) is achievable through Container Station and File Station alone, with no SSH access required. Schedule changes are made by editing compose YAML in Container Station, not by editing host cron.
7. Restricted (stream-only) items are discoverable in `shakedown status` with their restriction reason — the user can see what isn't being mirrored and why.
8. The source plugin interface is documented well enough that a second plugin (Phish.in or etree) could be written by someone who hasn't touched the IA plugin.

---

## Appendix A: Why not just use Lidarr?

Lidarr is the obvious "buy don't build" answer. It's been considered and ruled out for these reasons specific to archival sources:

- **MusicBrainz-anchored.** Lidarr's data model is artist→release→track, matched against MusicBrainz. The vast majority of IA recordings (audience tapes, soundboards with taper lineages, set-incomplete recordings) have no MusicBrainz release.
- **Indexer model assumes Usenet/BitTorrent.** Internet Archive isn't a torrent indexer; it's a content-addressable HTTP store. A Lidarr "indexer" plugin for IA would be more code than this whole project.
- **Doesn't model collections.** Lidarr thinks in artists. "Every show in this collection forever" is not a query Lidarr can express.

Lidarr remains a fine choice for *commercial* music — and Shakedown doesn't compete with it. Run both side by side if you want.

## Appendix B: Why not just use the `internetarchive` CLI directly?

The CLI handles fetch and checksum-skip well. What it doesn't handle:

- Multi-source abstraction (it's IA-only by definition)
- The hardlink seam to a library staging tree
- Collection-level state and reporting beyond per-item
- Scheduling and post-action hooks
- Any concept of "library layout" distinct from "archive layout"

You could shell-script all of the above. People have, for one-off projects (e.g., the JackStrawFromWichita `all-the-grateful-dead` repo). That works for a single collection one-time. It does not scale to "I want to add Phish next month and etree the month after."
