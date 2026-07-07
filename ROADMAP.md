# Roadmap

> Work remaining to close the gap between the codebase and
> [SPEC.md](SPEC.md). Sections are in build-dependency order; each ends
> with a surface workstream and a **Verify:** block a reviewer can
> exercise end-to-end. Completed work is deleted from this file — the
> changelog records history.

## Core fetch durability

The no-partial-commit guarantee currently lives inside the IA plugin;
the spec assigns it to the core so every plugin inherits it.

### §road:core-atomic-fetch

Move temp-dir download and atomic rename from the IA plugin into the
core fetch phase (`src/shakedown/sync.py`, `src/shakedown/plugins/base.py`,
`src/shakedown/plugins/ia/plugin.py`) so no plugin can commit partial
state to the archive. §spec:sync-workflow

### §road:core-retry-backoff

Add core-owned bounded retries for checksum mismatches and
`Retry-After`-honoring rate-limit backoff to the fetch phase
(`src/shakedown/sync.py`), instead of delegating both to the
`internetarchive` library. §spec:failure-behavior. Depends on
§road:core-atomic-fetch.

### §road:concurrent-collections

Restore the `max_concurrent_collections` global cap so collections sync
in bounded parallel (`src/shakedown/config.py`, `src/shakedown/sync.py`,
`tests/test_config.py`, `shakedown.example.yaml`).
§spec:configuration, §spec:sync-workflow

**Verify:** Configure two collections and run `shakedown sync`; kill the
process mid-download. Confirm the archive tree contains no
partially-fetched item directories and `shakedown status` reports no
`complete` item with missing files. Re-run `shakedown sync` and confirm
it resumes cleanly with both collections syncing concurrently; a third
run downloads nothing.

## Item lifecycle and status conformance

### §road:prune-retains-record

Change `prune_disappeared` to delete an item's archive files and staging
links while retaining its database record marked pruned
(`src/shakedown/sync.py`, `src/shakedown/models.py`,
`src/shakedown/state.py`). §spec:item-lifecycle

### §road:stale-collection-status

Report a collection as stale in `shakedown status` when its source
enumeration fails (`src/shakedown/sync.py`, `src/shakedown/status.py`).
§spec:failure-behavior. Depends on §road:prune-retains-record (shared
`status` reporting surface).

**Verify:** With `prune_disappeared: true` on a collection, remove an
already-mirrored item from the source's enumeration and run
`shakedown sync`. Confirm the item's files are gone from both trees but
`shakedown status` still reports the takedown as pruned. Then point a
collection at an unreachable source, sync, and confirm `status` marks
the collection stale while its existing items remain.

## Library staging robustness

### §road:template-unknown-fallback

Render missing or null `library_layout` template fields as a literal
`unknown` path segment instead of raising
(`src/shakedown/utils/templates.py`, `src/shakedown/staging.py`,
`tests/test_staging.py`), relying on collision detection to catch any
resulting conflicts. §spec:library-staging

**Verify:** Mirror an item whose metadata lacks a field used by the
collection's `library_layout` (e.g. no venue) and run
`shakedown restage`; confirm the item stages under an `unknown` segment
rather than failing. Wipe the library tree and confirm `restage`
rebuilds it fully with zero downloads.

## Handoff contract

The implemented webhook fires per item with an unversioned payload; the
spec contract is versioned, batched, once per (collection, run), and the
failure-notification path is configured but never fires.

### §road:handoff-batch-payload

Replace the per-item `item_complete` webhook/exec with the versioned
once-per-(collection, run) `sync.complete` batch payload and the
`{source}`/`{collection}`/`{staging_root}` template fields
(`src/shakedown/notify.py`, `src/shakedown/sync.py`,
`tests/test_notify.py`). §spec:handoff

### §road:failure-notifications

Implement the `notifications.on_failure` webhook, firing
`event: sync.failed` with an `errors` array when a run fails
(`src/shakedown/notify.py`, `src/shakedown/sync.py`). §spec:handoff.
Depends on §road:handoff-batch-payload.

**Verify:** Point a collection's `on_complete.webhook` at a local
listener and run `shakedown sync`; confirm exactly one POST per
(collection, run) carrying `payload_version: 1`,
`event: "sync.complete"`, a `run` object, and a `staged` array. Force a
run failure (unreachable source) and confirm the `on_failure` webhook
receives `event: "sync.failed"` with errors. Confirm a down listener is
logged in run errors, visible in `status`, and never fails the sync.

## Plugin seam proof

Success criterion 8 requires a documented interface and a second plugin
written against it; neither exists yet.

### §road:plugin-interface-doc

Write the source-plugin interface documentation (`docs/plugins.md`,
linked from `README.md`) sufficient to author a plugin without reading
the IA plugin's internals. §spec:source-plugins. Depends on
§road:core-atomic-fetch and §road:core-retry-backoff (the documented
contract must reflect core-owned durability and backoff).

### §road:etree-plugin

Implement and register the etree source plugin against the documented
interface only (`src/shakedown/plugins/etree/`, `tests/test_etree_plugin.py`).
§spec:source-plugins. Depends on §road:plugin-interface-doc.

**Verify:** Add a `type: etree` source with one collection to
`shakedown.yaml` and run `shakedown sync`; confirm items mirror into the
archive and library trees and appear in `shakedown status`. Confirm via
review that the plugin was written from `docs/plugins.md` alone, without
reference to the IA plugin's internals.

## Serve auth conformance

### §road:serve-bearer-auth

Switch mutating-endpoint authentication from the custom
`X-Shakedown-Token` header to standard `Authorization: Bearer` tokens
(`src/shakedown/server.py`, `tests/test_server.py`,
`docker-compose.example.yaml`). §spec:serve

**Verify:** Run `shakedown serve` with `SHAKEDOWN_API_TOKEN` set:
`POST /sync` with `Authorization: Bearer <token>` triggers a sync and
without it is rejected. Unset the token and confirm mutating endpoints
report disabled (not open) while `GET /healthz`, `/status`, and
`/metrics` remain reachable.
