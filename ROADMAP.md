# Roadmap

> Work remaining to close the gap between the codebase and
> [SPEC.md](SPEC.md). Sections are in build-dependency order; each ends
> with a surface workstream and a **Verify:** block a reviewer can
> exercise end-to-end. Completed work is deleted from this file — the
> changelog records history.

## Layout collision safety §road:layout-collision-safety

Closes §spec:layout-collision-safety: a lossy `library_layout` is caught
at config time, and any runtime drops are surfaced loudly without
aborting the run.

### Layout uniqueness declaration and config-time guard §road:layout-uniqueness-guard

Add a per-item-unique-fields declaration to the plugin capability
(`src/shakedown/plugins/base.py`, `plugins/ia/plugin.py`,
`plugins/etree/plugin.py`) and extend `_validate_layouts` in
`src/shakedown/config.py` to warn when a non-passthrough `library_layout`
references no per-item-unique field and hard-error when it references no
plugin field at all. §spec:layout-collision-safety

### Consolidated collision summary in run and status §road:collision-summary

Aggregate same-run staging collisions into a single per-run count and
colliding-path summary (`src/shakedown/sync.py` collision handling and
`CollectionSyncStats`) and surface it in `shakedown status`
(`src/shakedown/status.py`), distinct from ordinary fetch failures and
without aborting the run. §spec:layout-collision-safety

**Verify:** Load a config whose non-passthrough layout omits any unique
field (e.g. `{year}/{venue}`) and confirm a named startup warning; load a
constant template and confirm a hard config error. Sync a source with two
items that render the same path with matching filenames: confirm the run
completes, exits non-zero, and `status` plus the run summary report N
recordings dropped to layout collisions with the colliding paths.

## Source metadata preservation §road:metadata-preservation

Closes §spec:metadata-preservation: opt-in `metadata.json` sidecars that
travel into the library without disturbing media change detection.

### Write the metadata sidecar at fetch, excluded from the manifest §road:sidecar-write

Add the per-collection `preserve_source_metadata` opt-in
(`src/shakedown/config.py`) and have the core write `metadata.json` from
`desc.metadata` into the fetch temp directory before atomic promotion,
excluded from the recorded manifest, in `src/shakedown/sync.py`
(`_fetch_one`). §spec:metadata-preservation

### Stage and restage the sidecar into the library §road:sidecar-stage

Hardlink the `metadata.json` sidecar into the staging directory alongside
the media and unlink it on unstage (`src/shakedown/staging.py`
`stage_item` / `unstage_item`), regenerating it from the DB
`source_metadata` when the archive copy is absent so `restage` and
`reconcile` stay network-free. Depends on §road:sidecar-write.
§spec:metadata-preservation

### Explicit metadata refresh command §road:metadata-refresh

Add `sync --refresh-metadata` (`src/shakedown/cli.py`,
`src/shakedown/sync.py`) that re-resolves source metadata for
already-mirrored items in preserve-opted collections under the shared
politeness budget, rewrites each `metadata.json` and its recorded
`source_metadata`, and restages without re-downloading media. Depends on
§road:sidecar-stage. §spec:metadata-preservation

**Verify:** Set `preserve_source_metadata: true` and sync: confirm
`metadata.json` appears in the archive item directory and is hardlinked
(same inode) into the library staging directory, and is absent from the
recorded manifest (a re-sync reports `unchanged` and downloads no media).
Wipe the library and `restage`: the sidecar reappears with no network.
Run `sync --refresh-metadata` after the upstream metadata changes:
`metadata.json` and the DB `source_metadata` update while no media is
re-downloaded.
