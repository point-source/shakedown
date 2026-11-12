# Handoff: Overview tab

*Approved: round 2, 2026-11-12 · Kit: §spec:ui-kit · Endpoints: `GET /api/library`, `/api/attention`, `/api/activity`, `/api/runs`, `/api/health`*
*Code: `web/src/views/Overview.tsx`, `web/src/dash/RunningNow.tsx`, `web/src/dash/NeedsAttention.tsx`, `web/src/dash/RunsList.tsx`*

## Purpose

The ten-second morning check: is anything running or stuck, what needs
the operator, what ran recently, and is the config sound.

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| Mirrored / "N pending" | `library.totals.counts.complete` / `.pending` | `count()` | Skeleton |
| Archive | `library.totals.archive_bytes` | `bytes()` decimal | Skeleton |
| Free space / "of T" | `library.disk.free_bytes` / `total_bytes` | `bytes()` | Skeleton |
| Running now rows | `activity.runs` where `state == "running"` | see Running now | "Nothing is running. The last run ended <age> ago." (age from newest finished run in `/api/runs`; sentence part omitted when none) |
| Needs attention entries | `attention.entries` | see Needs attention | "Nothing needs your attention." |
| Recent runs | first 5 `runs.runs` with `finished_at` | `RunsList` | "Runs show up here once the first one ends." |
| Config | `health.config.valid` | Badge success "Valid" / danger "Has an error" | Skeleton |
| Last validation rows | `health.validation[]` | collection; `clock(at)`; Badge from `VALIDATION[status]` | "Never validated" when `status` null |

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

### Needs attention entry (one per `entries[]`)

Title and next step come from `attentionWords()` (`web/src/lib/words.ts`),
one per kind; collection right-aligned, xs muted, wrapping. Then, when
present: "Next step: " + `recovery.safe_next_action` (server text, from
`detail.recovery` or a `recovery` entry's detail); the reason text
(`detail.last_reason` or `detail.failure_notice_error`, mono xs);
show IDs (`identifiers`, or `detail.unplaced_files` paths) via
`ShowIds` (first 3, "and N more"); "Since <clock>" when `since` is set
(omitted for rescheduled, whose title carries it, and for
ended_unexpectedly). The header carries a `secondary` Badge with the
entry count.

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
| long content | `long_content` | 8 entries of every kind, 300 failed show IDs, long names and server errors wrap |
| loading / can't reach / config broken | see `shell.md` | |

## Copy

Stat labels Mirrored, Archive, Free space; "N pending"; "of <size>".
Card titles Running now, Needs attention, Recent runs, Shakedown. "All
runs". Run facts Listed, Discovered, Shows, Downloaded, Speed, Time
left, Failed; "Estimating"; "Now:"; "Started <time> · <age> ago"; "No
progress for <time>". Attention titles and next steps per kind in
`words.ts`. "Next step:", "Since <time>". "Config", "Valid", "Has an
error", "Last validation", "Never validated", validation words
Passed / Passed with problems / Did not finish / Failed / Listing
incomplete.

## Behavior

Running now and the No progress badge update every 5 s with
`/api/activity`. Ended entries in `/api/activity` are not shown
(operator decision 2026-11-12). "All runs" is a button that opens the
Runs tab.

## Open questions

None. The validation words joined the Terms (round 2).
