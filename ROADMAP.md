# Roadmap

> Work remaining to close the gap between the codebase and
> [SPEC.md](SPEC.md). Sections are in build-dependency order; each ends
> with a surface workstream and a **Verify:** block a reviewer can
> exercise end-to-end. Completed work is deleted from this file — the
> changelog records history.

## Polite parallel discovery §road:polite-parallel-discovery

Closes the first-full-sync and slow-metadata gaps: discovery resolves
per-item metadata concurrently under a single per-source politeness
budget, and one slow upstream endpoint no longer stalls the run.
Discovery still fully materializes before planning, so prune/disappeared
semantics are untouched by this section. (§spec:discovery-performance)

### Shared per-source concurrency budget §road:source-budget

Introduce a per-source shared concurrency budget (config on `Config`/
`SourceConfig` in `config.py`) and route the existing download pool in
`sync.py` to acquire from it, so all of a source's simultaneous
connections draw on one bounded pool. §spec:source-budget

### Bounded discovery timeout and back-off §road:discovery-timeout

Set a bounded read-timeout/retry envelope on the IA `ArchiveSession` and
apply the fetch path's retriable/`Retry-After` back-off classification to
discovery-time faults, in `plugins/ia/plugin.py`. §spec:slow-metadata

### Parallel per-item discovery §road:parallel-discovery

Fan the per-item metadata (`get_item()`) calls across the shared budget
during the discovery drain (`plugins/ia/plugin.py`, `sync.py`), keeping
the enumeration fully materialized before planning. §spec:discovery-pipeline — Depends on §road:source-budget, §road:discovery-timeout

**Verify:** Run `shakedown sync --collection <c>` against a plugin whose
per-item metadata fetch is instrumented (offline test double). Confirm:
per-item metadata resolves concurrently; the count of simultaneous
upstream connections never exceeds the configured per-source budget even
with `max_concurrent_collections > 1`; a deliberately slow/timing-out
single item completes within the bounded timeout while other items
proceed (no minutes-long stall); and NEW/CHANGED/UNCHANGED/DISAPPEARED
classification is byte-for-byte identical to the pre-change serial run.

## Pipelined discover to download §road:pipelined-sync

Closes the first-full-sync overlap gap: downloads for `new`/
`changed-upstream` items begin while discovery is still enumerating,
under the shared budget, with pruning held back as a post-enumeration
barrier. (§spec:discovery-performance)

### Stream classification and fetch pipeline §road:stream-fetch

Restructure `sync.py` so each discovered item is classified and (if
`new`/`changed-upstream`) fetched as it arrives rather than after the
full drain, with the prune/`disappeared` step gated on complete
successful enumeration and per-run stats accumulated thread-safely.
§spec:discovery-pipeline — Depends on §road:parallel-discovery

**Verify:** Run `shakedown sync --collection <c>` on a first sync of a
multi-item collection (offline doubles). Confirm the first download
starts before discovery has enumerated the last item (overlap observable
in logs/timing); that a discovery failure partway through leaves the
collection marked stale with zero items pruned; that a complete
enumeration prunes disappeared items exactly as before; and that run
counts (new/updated/unchanged/failed/bytes) are correct under
concurrency.

## Incremental discovery §road:incremental-discovery

Closes the recurring-no-op-sync gap: an opt-in per-collection fast-path
skips the per-item metadata fetch for items a cheap source change signal
reports unchanged. Independent of §road:polite-parallel-discovery and
§road:pipelined-sync — may proceed in parallel. (§spec:discovery-performance)

### Persist source change signal §road:change-signal-column

Add a per-item source change-signal column to the `Item` model and
`items` table with a `SCHEMA_VERSION` bump and migration
(`models.py`, `db.py`, `state.py`), populated from the descriptor.
§spec:incremental-discovery

### Surface the change signal from IA search §road:ia-change-signal

Request `oai_updatedate` and `item_size` in `search_items(...)` and
expose the combined signal on `ItemDescriptor` (`plugins/ia/plugin.py`,
`plugins/base.py`). §spec:incremental-discovery — Depends on §road:change-signal-column

### Opt-in incremental skip §road:incremental-skip

Add the per-collection `incremental_discovery` flag (`config.py`) and,
in `sync.py`/`plugins/ia/plugin.py`, short-circuit to `unchanged` —
skipping `get_item()` — when the stored signal matches, falling through
to the full manifest comparison otherwise. §spec:incremental-discovery — Depends on §road:ia-change-signal

**Verify:** Set `incremental_discovery: true` on a collection and run
`shakedown sync --collection <c>` twice (offline doubles). Confirm the
second run performs no per-item metadata fetch for items whose signal is
unchanged (observable via the instrumented double / logs); that an item
whose signal changed is still classified `changed-upstream` and
refetched; that an item with no stored signal falls through to full
manifest comparison; and that with the flag off (default) behavior is
unchanged from today. Confirm `verify --deep` still reports drift as the
residual net.
