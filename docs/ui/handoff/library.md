# Handoff: Library tab

*Approved: round 1, 2026-11-12 · Kit: §spec:ui-kit · Endpoints: `GET /api/library`, `GET /api/attention` (`health`)*
*Code: `web/src/views/Library.tsx`*

## Purpose

How big is it all: what each collection holds, disk used and left,
30-day growth, what arrived most recently, and library health facts.

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| Mirrored / pending | `totals.counts.complete` / `.pending` | `count()` | Skeleton |
| Archive / "N shows" | `totals.archive_bytes` / `counts.complete` | `bytes()` | Skeleton |
| Library / "No extra space" | `totals.library_bytes` | `bytes()` | Skeleton |
| Free space / "of T" | `disk.free_bytes` / `total_bytes` | `bytes()` | Skeleton |
| Collections table | `collections[]`: complete, pending, failed (destructive > 0), restricted, disappeared, archive_bytes, library_bytes; source under the name | `count()`, `bytes()` | — |
| Growth charts | `growth[]` summed per date: `complete`, `archive_bytes / 1e12` | two Areas (`--chart-1` shows, `--chart-2` TB), 3 y ticks | "Growth shows up here after a second day of runs." (< 2 dates) |
| Recently mirrored (first 10) | `recently_mirrored[]` | title (or show ID when null), "<age> ago", collection · show ID mono | "Shows show up here as they are mirrored." |
| Library health, per collection with any fact | `attention.health[]` | "N shows restricted" + first 3 "id: reason"; "N shows disappeared from the source, kept here" + "id · missing since 2 Nov"; "Drift: N files changed since Shakedown recorded them"; "N shows have no playable copy" + "id: reason"; "N shows are kept as the original plus a playable copy" + IDs | "Every mirrored show is complete and playable." |

The page shows no disk-full estimate (operator decision 2026-11-12); the API no longer sends one.

## Layout

Stats: 2 columns, 4 from 1024 px. Collections: Table from 1024 px,
stacked rows below. Growth: two charts side by side from 640 px.
Recently mirrored and Library health side by side from 1024 px.

## States

| State | Scenario | What changes |
| --- | --- | --- |
| default | `default` | 3 collections, 30 days growth, 5 recent shows, health facts |
| first install | `first_install` | zero stats, growth and recent empty texts, health empty text |
| long content | `long_content` | 6 collections, long names and IDs wrap |
| loading | `loading` | Skeletons |

## Copy

As in Data. Terms: Mirrored, Pending, Failed, Restricted, Disappeared,
Archive, Library, Free space, Drift.

## Behavior

Refreshes every 30 s. Lists show the first N with "and N more"; no
expanders.

## Open questions

None. The library health words joined the Terms (round 2).
