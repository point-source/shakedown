# Handoff: Overview tab

*Approved: round 2, 2026-11-12; Lost contact and Stopped 2026-11-16; Waiting rows and the rescheduled wording 2026-11-20; Shakedown card with the scheduler and run controls, scheduler stopped, trim and delete runs, round S1 2026-11-24 · Kit: §spec:ui-kit · Endpoints: `GET /api/library`, `/api/attention`, `/api/activity`, `/api/runs`, `/api/health`, `/api/config`; `POST /sync`, `POST /validate`*
*Code: `web/src/views/Overview.tsx`, `web/src/dash/RunningNow.tsx`, `web/src/dash/NeedsAttention.tsx`, `web/src/dash/RunsList.tsx`, `web/src/dash/ShakedownCard.tsx`, `web/src/dash/actions.tsx`*

## Purpose

The ten-second morning check: is anything running or stuck, what needs
the operator, what ran recently, is the config sound and the scheduler
working, and, when needed, start a sync or a validation.

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| Mirrored / "N pending" | `library.totals.counts.complete` / `.pending` | `count()` | Skeleton |
| Archive | `library.totals.archive_bytes` | `bytes()` decimal | Skeleton |
| Free space / "of T" | `library.disk.free_bytes` / `total_bytes` | `bytes()` | Skeleton |
| Running now rows | `activity.runs` where `state == "running"`, then `activity.waiting` in its order | see Running now, Waiting row | "Nothing is running. The last run ended <age> ago." when both are empty (age from newest finished run in `/api/runs`; sentence part omitted when none) |
| Needs attention entries | `attention.entries` | see Needs attention | "Nothing needs your attention." |
| Recent runs | first 5 `runs.runs` with `finished_at` | `RunsList` | "Runs show up here once the first one ends." |
| Config | `health.config.valid` | Badge success "Valid" / danger "Has an error" | Skeleton |
| Scheduler | `health.scheduler.state`, `.last_seen` | Badge from `SCHEDULER`; "since <clock>" after it when `stale` | Skeleton |
| Collection rows | `health.validation[]`, one per collection | see Shakedown card | Skeleton |
| Next sync | `config.next_syncs[]` matched on source and collection | "Next sync <clock>"; "Manual only" when `next_at` null | omitted while `/api/config` has not answered |
| Last validation | `health.validation[].status`, `.at` | "Last validation <clock>" + Badge from `VALIDATION` | "Never validated" when `status` null |

### Running now row (one per running entry)

- Line 1: collection (medium, `wrap-anywhere`) · kind word (`KIND`),
  right: phase Badge `secondary` (`PHASE`), plus Badge `warning` "No
  progress for <duration>" when now − `last_progress_at` > 5 min
  (operator decision 2026-11-12).
- Progress bar: `items_done / items_to_fetch`, only when
  `items_to_fetch` is known and the phase is not listing/discovering;
  `aria-label` "<done> of <total> shows".
- Facts (2 columns on phone, a wrapping row from 640 px): phase listing
  → "Listed" `items_listed`; discovering → "Discovered" `items_looked_up
  of items_to_look_up`; otherwise "Shows" `items_done of items_to_fetch`
  ("estimating" when null). "Downloaded" `bytes_downloaded` (+ "of
  bytes_to_fetch" when known). "Speed" `speed_bytes_per_second`. "Time
  left" `eta_seconds` or "Estimating". "Failed" `items_failed` in
  destructive when > 0.
- "Now:" + `current_items` (mono xs, wrap, first 4, "and N more").
- Meta: "Started HH:MM · <age> ago".
- A run with no collection (Trim history) leads with its kind word in
  medium, with no "—" and no dot. A Trim history or Delete files run
  has no phase, progress bar, facts, "Now:", or No progress badge: its
  row is line 1 and the meta line (round S1, 2026-11-24).

### Waiting row (one per `activity.waiting` entry, after the running rows)

A collection waiting for a slot in the current sync is not a run yet
(§spec:run-activity "Waiting collections"; operator decision 2026-11-20).

- Line 1: collection (medium, `wrap-anywhere`) · kind word (`KIND`),
  right: Badge `secondary` "Waiting" (`WAITING`). No phase, progress
  bar, facts, "Now:", or No progress badge.
- Meta: "Since HH:MM · <age> ago" from `since`.

### Needs attention entry (one per `entries[]`)

Title and next step come from `attentionWords()` (`web/src/lib/words.ts`),
one per kind; collection right-aligned, xs muted, wrapping. Then, when
present: "Next step: " + `recovery.safe_next_action` (server text, from
`detail.recovery` or a `recovery` entry's detail); the reason text
(`detail.last_reason` or `detail.failure_notice_error`, mono xs);
show IDs (`identifiers`, `detail.unplaced_files` paths, or each of
`detail.unidentified_files` as `<show ID>/<file>`) via
`ShowIds` (first 3, "and N more"); "Since <clock>" when `since` is set
(omitted for rescheduled, whose title carries it, and for
ended_unexpectedly). The header carries a `secondary` Badge with the
entry count.

### Shakedown card

- Header: title "Shakedown"; CardAction Button `outline` `sm`
  "Validate" ("Validating…" and disabled while the request is out),
  only once `/api/health` has answered.
- Two label/value rows: "Config" with its Badge, "Scheduler" with its
  Badge (`SCHEDULER`: Working success, Not responding danger, Nothing
  scheduled neutral, Never started warning).
- Collection rows (`ul.divide-y`): name (medium); "Next sync <clock>"
  or "Manual only"; "Last validation <clock>" with its Badge, or "Never
  validated"; Button `outline` `sm` "Sync now" ("Starting…" and
  disabled while the request is out). Phone and tablet: the name and
  Sync now on line 1, next sync and last validation below. From
  1024 px the rows share one subgrid of 4 columns (name, next sync,
  last validation, Sync now) so each column lines up across rows.
- Answers show as a Toast (kit usage rule): Sync now `202` → success
  "Sync started" / the collection; Validate `200` with `ready` →
  success "Validation passed", without → warning "Validation found
  problems" / "Each collection's result is under Last validation.".
  Refusals (`refusal()` in `actions.tsx`): `409` with no code → warning
  "A run is in progress" / "Try again when it ends."; `403` → danger
  "Shakedown refused the change" / "Open the dashboard at its usual
  address and try again."; no answer → danger "Can't reach Shakedown"
  / "Nothing changed. Try again."; anything else → danger "That didn't
  work" / "Shakedown answered <status>.". Toasts keep the last 3 and
  close after 8 s.

## Layout

Stats: 2 columns on phone (Free space spans both), 3 from 640 px.
Then a 5-column grid from 1024 px: Running now spans 3 on the left,
Needs attention spans 2 on the right (operator decision 2026-11-12).
Below 1024 px they stack with Needs attention first. Then Recent runs
(header action "All runs" → Runs tab) and the Shakedown card. All
cards `min-w-0`; gaps `gap-3` (stats) and `gap-4`.

## States

| State | Scenario | What changes |
| --- | --- | --- |
| default | `default` | 2 running (one with No progress), 4 entries, 4 recent runs |
| quiet | `quiet` | Running now and Needs attention empty states |
| first install | `first_install` | zero stats, empty states, "Never validated" |
| long content | `long_content` | 9 entries of every kind, 300 failed show IDs, long names and server errors wrap |
| waiting | `waiting` | a catch-up round: 2 running catch-ups, then 3 waiting rows in the order they start |
| scheduler stopped | `scheduler_stopped` | the quiet page with 2 entries: "Scheduled syncs are not starting" (`scheduler_stale`, about no collection, so no collection name) and "Scheduled sync did not start" (`tick_overdue`); Scheduler reads "Not responding since <clock>" |
| trims | `trims` | a running Trim history row; a failed one with "214 runs trimmed" and its error; a completed one with "1,890 runs trimmed · 412 MB freed" |
| purges | `purges` | a running Delete files row; "Completed, some files not deleted" with the path that failed; "912 GB freed" |
| sync started | `sync_started` | Sync now → success toast (behaviour test) |
| sync busy | `sync_busy` | Sync now → "A run is in progress"; Validate → "Validation found problems" (behaviour tests) |
| loading / can't reach / config broken | see `shell.md` | |

## Copy

Stat labels Mirrored, Archive, Free space; "N pending"; "of <size>".
Card titles Running now, Needs attention, Recent runs, Shakedown. "All
runs". Run facts Listed, Discovered, Shows, Downloaded, Speed, Time
left, Failed; "Estimating"; "Now:"; "Started <time> · <age> ago"; "No
progress for <time>". "Waiting"; "Since <time> · <age> ago". Attention titles and next steps per kind in
`words.ts`. "Next step:", "Since <time>". "Config", "Valid", "Has an
error", "Last validation", "Never validated", validation words
Passed / Passed with problems / Did not finish / Failed / Listing
incomplete. "Scheduler", Working / Not responding / Nothing scheduled /
Never started, "since <time>". "Next sync <time>", "Manual only". "Sync
now", "Starting…", "Validate", "Validating…". Toast words as listed
under Shakedown card. Run kinds Trim history, Delete files; "<N> runs
trimmed", "<size> freed", "Completed, some files not deleted", "Error
details trimmed" (see `runs.md`).

## Behavior

Running now and the No progress badge update every 5 s with
`/api/activity`. Ended entries in `/api/activity` are not shown
(operator decision 2026-11-12). "All runs" is a button that opens the
Runs tab. Sync now and Validate send one request each and never retry;
the started run appears in Running now on the next `/api/activity`
refresh. The card does not refetch `/api/health` after Validate; Last
validation updates on the next 30 s refresh. `/api/config` refreshes
every 30 s.

## Open questions

None. The validation words joined the Terms (round 2). "Stopped reporting"
became "Lost contact" and a run stopped on request reads `secondary`
"Stopped" (operator decision 2026-11-16). Collections waiting for a slot
got their own rows and the glossary term "Waiting"; the rescheduled
entry became "Sync skipped at <time>" / "It runs as a catch-up sync." so
"waiting" keeps one meaning (operator decisions 2026-11-20). Round S1
(2026-11-24): the scheduler words and the attention titles "Scheduled
syncs are not starting" / "Scheduled sync did not start" were approved
over the brief's "Scheduler stopped" / "Sync overdue", because Stopped
already means a run someone stopped.
