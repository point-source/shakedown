# Handoff: Runs tab

*Approved: round 1, 2026-11-12; loading state 2026-11-13; Lost contact and Stopped 2026-11-16; trim and delete runs and the Action column, round S1 2026-11-24 · Kit: §spec:ui-kit · Endpoints: `GET /api/runs?days=30`, `GET /api/library`*
*Code: `web/src/views/Runs.tsx`, `web/src/dash/RunsList.tsx`*

## Purpose

What ran over the last 30 days, how each run ended, and how long ago
each collection last synced to completion.

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| Last completed sync, per collection | `library.collections[].last_successful_sync_at` | "<age> ago · <clock>" | "None yet" |
| Started | `started_at` | `clock()` | — |
| Collection, Action | `collection`, `kind` | `KIND` | Collection "—" when null (Trim history) |
| Took | `duration_seconds` | `duration()` | "—" while running |
| New / Updated / Failed | `items_new` / `items_updated` / `items_failed` | `count()`; Failed destructive when > 0 | "—" |
| Downloaded | `bytes_downloaded` | `bytes()` | "—" |
| Result | `result`, `end_reason`, `kind` | Badge via `outcome()`: `stopped_reporting` → danger "Lost contact"; a Delete files run with `completed_with_item_issues` → warning "Completed, some files not deleted"; else `RESULT` (`stopped` → `secondary` "Stopped"); running → `secondary` "Running" | — |
| Effects (Trim history, Delete files) | `runs_trimmed`, `bytes_freed` | `runEffects()`: "<N> runs trimmed" (trim); "<size> freed" when `bytes_freed` > 0 (a library delete frees about nothing and shows none); joined " · " | omitted |
| Error details trimmed | `detail_trimmed` | "Error details trimmed", only when the run had errors to lose (`items_failed` > 0 or a result other than Completed) | omitted |
| Errors | `errors[]` | mono xs, first 2, "and N more" | omitted |
| Failure notice | `failure_notice_error` | "Failure notice not sent: " + text | omitted |

## Layout

Two cards: Last completed sync (one row per collection), then Runs
("Last 30 days, newest first"). Runs is a Table from 1024 px; below,
stacked rows (collection · kind, Badge right; second line
"<start> · <took> · N new, N updated[, N failed] · <size>"; errors under).
A run with no collection leads with its kind in medium, with no dot. A
Trim history or Delete files row's second line is "<start> · <took>"
then its effects instead of the counts. In the Table the Result cell
stacks the Badge, then the effects and "Error details trimmed" (sm
muted), then the errors.
The server's order is kept (newest first).

## States

| State | Scenario | What changes |
| --- | --- | --- |
| default | `default` | 7 runs incl. 2 running, lost contact, partly failed, stopped |
| first install | `first_install` | "None yet" per collection; "Runs show up here once the first one starts." |
| long content | `long_content` | 30 runs, long names, multi-error rows |
| loading | `loading` | Skeleton rows |
| trims | `trims` | Trim history rows: running, failed with "214 runs trimmed" and its compaction error, completed with "1,890 runs trimmed · 412 MB freed"; a sync with "Error details trimmed" |
| purges | `purges` | Delete files rows: running, "Completed, some files not deleted" with the path, completed with "912 GB freed" |

## Copy

"Last completed sync", "<age> ago", "None yet", "Runs", "Last 30 days,
newest first", column heads Started, Collection, Action, Took, New,
Updated, Failed, Downloaded, Result; result words per `RESULT` and
"Running", "Completed, some files not deleted"; "<N> runs trimmed",
"<size> freed", "Error details trimmed"; "and N more"; "Failure notice not sent:".

## Behavior

Refreshes every 30 s. Read-only; no row actions.

## Open questions

None. Round S1 (2026-11-24): the operator renamed the column head "Run"
to "Action" (offered with "Event"), and approved the trim and delete
words.
