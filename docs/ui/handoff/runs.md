# Handoff: Runs tab

*Approved: round 1, 2026-11-12 · Kit: §spec:ui-kit · Endpoints: `GET /api/runs?days=30`, `GET /api/library`*
*Code: `web/src/views/Runs.tsx`, `web/src/dash/RunsList.tsx`*

## Purpose

What ran over the last 30 days, how each run ended, and how long ago
each collection last synced to completion.

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| Last completed sync, per collection | `library.collections[].last_successful_sync_at` | "<age> ago · <clock>" | "None yet" |
| Started | `started_at` | `clock()` | — |
| Collection, Run | `collection`, `kind` | `KIND` | — |
| Took | `duration_seconds` | `duration()` | "—" while running |
| New / Updated / Failed | `items_new` / `items_updated` / `items_failed` | `count()`; Failed destructive when > 0 | "—" |
| Downloaded | `bytes_downloaded` | `bytes()` | "—" |
| Result | `result`, `end_reason` | Badge via `outcome()`: `stopped_reporting` → danger "Stopped reporting"; else `RESULT`; running → `secondary` "Running" | — |
| Errors | `errors[]` | mono xs, first 2, "and N more" | omitted |
| Failure notice | `failure_notice_error` | "Failure notice not sent: " + text | omitted |

## Layout

Two cards: Last completed sync (one row per collection), then Runs
("Last 30 days, newest first"). Runs is a Table from 1024 px; below,
stacked rows (collection · run, Badge right; second line
"<start> · <took> · N new, N updated[, N failed] · <size>"; errors under).
The server's order is kept (newest first).

## States

| State | Scenario | What changes |
| --- | --- | --- |
| default | `default` | 6 runs incl. 2 running, stopped reporting, partly failed |
| first install | `first_install` | "None yet" per collection; "Runs show up here once the first one starts." |
| long content | `long_content` | 30 runs, long names, multi-error rows |
| loading | `loading` | Skeleton rows |

## Copy

"Last completed sync", "<age> ago", "None yet", "Runs", "Last 30 days,
newest first", column heads Started, Collection, Run, Took, New,
Updated, Failed, Downloaded, Result; result words per `RESULT` and
"Running"; "and N more"; "Failure notice not sent:".

## Behavior

Refreshes every 30 s. Read-only; no row actions.

## Open questions

None.
