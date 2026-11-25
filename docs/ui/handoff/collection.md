# Handoff: Collection editor

*Approved: round S3, 2026-11-25 · Kit: §spec:ui-kit · Endpoints: `GET /api/config`, `GET /api/library`, `GET /api/source-link`, `PUT /api/config`, `POST /api/collections/purge` (§spec:dashboard-settings, §spec:schedules, §spec:collection-removal, §spec:edit-lock)*
*Code: `web/src/settings/Collection.tsx`, `web/src/settings/AddSource.tsx`, `web/src/settings/route.ts`, `web/src/lib/sourceLink.ts`, `web/src/lib/schedule.ts` (`staggeredDefault`), the route and remove handling in `web/src/views/Settings.tsx`*

## Purpose

Add, change, or remove one collection: what it mirrors, when it syncs,
how its library folders are named, and who hears about new shows.

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| Heading | `collections[j].name` | as is | "New collection" while not saved |
| Line under it | `sources[i].name`, `.type` | "<source> · type <type>"; "In <source>" while new | — |
| Name | `name` | Input while new, text once saved | — |
| Search | `query` | Textarea, 2 rows, wraps | — |
| Folder layout | `library_layout`; `source_types[].layout_fields`, `.layout_filters` | Input; fields and filters listed mono in the help | lists omitted for an unknown type |
| Fill in missing tags, Keep the source's notes, Quick change check, Delete shows the source removes | `fill_tags`, `preserve_source_metadata`, `incremental_discovery`, `prune_disappeared` | Switch | — |
| Repeat, Day, Time | `schedule` via `readSchedule()` | Daily / Weekly / Manual only / Custom; Day Select; Time as 24-hour `HH:MM` text | Manual only when `null` |
| Cron expression | `schedule` | mono Input (Custom) | — |
| Clock note | `document.timezone` | "Times follow <tz>." | "the container's clock" |
| Next sync | `next_syncs[].next_at` | "Next sync " + `clock()`, only while the schedule equals the saved one | "Next sync is worked out when you save." |
| Webhook URL | `on_complete.webhook` | Input | placeholder "None"; empty saves `on_complete: null` |
| Command | `on_complete.exec` | mono text, read-only | — (shown instead of Webhook URL) |
| Add-back note | `unconfigured[]` for this name | "Shakedown still holds N shows (size) for X." | normal Name help |
| Remove choices | `library.collections[]`: `counts.complete + counts.disappeared`, `archive_bytes` | "N shows (size)" | "its shows" |
| Errors with no field here | `errors[]` under this collection's `loc` that match no field | destructive Alert, "<key › path>: <words>" | omitted |

## Layout

Back link "‹ Settings", `h2`, and the line under it; then the read-only
Alert or the errors Alert when they apply. From 1024 px two columns:
What to mirror and Library folders on the left; Schedule, Syncing, and
Handoff notice on the right. Below 1024 px every card stacks in that
order. Remove collection is a full-width card at the bottom (saved
collections, editable file only).

- **Schedule**: Repeat, Day, and Time in three columns from 640 px;
  Custom puts Cron expression across two. Below 640 px they stack.
- **Save bar**: the page's save bar (settings handoff); toasts sit
  above it while it shows, from 640 px.
- **Add source**: an outline button beside the "Sources" heading on the
  Settings list opens a Dialog with Name and Type.
- Components: Card, Field, Input, Textarea, Select, Switch, Alert,
  AlertDialog, Dialog, RadioGroup, Label, Button, toast.

## States

| State | Scenario | What changes | Golden |
| --- | --- | --- | --- |
| editor | `default`, `#settings/internetarchive/grateful-dead` | Weekly schedule with Next sync; Webhook URL | `collection-editor` |
| new | `default`, `#settings/internetarchive?add` | "New collection", Name editable, staggered schedule (Weekly, Sunday, 06:00), passthrough layout, no Remove | `collection-new` |
| add back | `default`, `?add=dmb` | Name "dmb", add-back note | `collection-add-back` |
| manual and command | `settings_variety`, phish | "Syncs only when someone presses Sync now."; Command shown read-only | `collection-manual-command` |
| custom | `settings_variety`, etree-lma | Repeat Custom, Cron expression `0 2 1,15 * *` | `collection-custom` |
| list with variety | `settings_variety`, `#settings` | rows read "Manual only" and "Custom: …" | `collection-variety-list` |
| file with errors | `settings_invalid`, phish | error under its field; errors with no field in the Alert | `collection-invalid` |
| read-only | `settings_locked` | Alert linking to the Settings page; values as text, Repeat as "Syncs"; no Remove | `collection-locked` |
| Daily chosen | editor, Repeat → Daily | Day gone; Time stays | `collection-daily` |
| remove dialog | `settings_clean`, Remove collection…, third choice | AlertDialog, destructive "Remove, delete everything" | `collection-remove` |
| remove, delete blocked | `settings_remove_busy` | toast "Removed phish, files kept" | `collection-remove-busy` |
| add source | `settings_clean`, Add source, a taken name | Dialog with "There's already a source called internetarchive." | `collection-add-source` |
| link read | new, a pasted archive.org link | Search becomes `collection:BobWeir`; "Read from the link you pasted." | `collection-link-read` |
| link unreadable | new, a link from another site | link stays; warning under Search | `collection-link-unreadable` |
| link read failed | `/api/source-link` errors | link stays; "Shakedown couldn't read this link just now." | behaviour test |
| bad time | Time "3pm" | "Enter a 24-hour time, such as 03:30."; schedule unchanged | behaviour test |
| unknown route | `#settings/internetarchive/nope` | Card "There's no collection called nope in internetarchive." and "Back to Settings" | behaviour test |

Page states are full-page goldens; interaction states are shot at the
viewport.

## Copy

What to mirror: "Name" ("Fixed once saved: its shows are filed under
it."; once saved, text with "Fixed once created: its shows are filed
under it. To rename, remove the collection and add a new one."),
"Search" (a type that reads links: "Paste a collection or search link
from the source's site, or type the search."; otherwise "The source's
own search for the shows to mirror."; after a read: "Read from the link
you pasted. Check it before you save."; unreadable, warning: "Shakedown
can't read this link for this source. Paste a collection or search page
from the source's site, or type the search."; failed, warning:
"Shakedown couldn't read this link just now. Paste it again, or type the
search."). Add back: "Shakedown still holds N shows (size) for X.
They're reused: nothing downloads again."

Library folders: "Folder layout" ("Each show's folder, built from
fields such as {year}. End it with {identifier}: each recording needs
its own folder. passthrough copies the archive's folders as they are."
then "Fields: …. Filters, as {venue|short}: …."), "Fill in missing tags"
("Adds missing artist, album, date, venue, and track title tags from the
source. Tags already there are never changed, and the archive copy gets
them too."), "Keep the source's notes" ("Saves each show's notes from
the source as metadata.json beside its files.").

Schedule: "Repeat" (Daily, Weekly, Manual only, Custom), "Day", "Time"
("Enter a 24-hour time, such as 03:30."), "Cron expression" ("Five
fields: minute, hour, day of month, month, day of week."), "Times follow
<tz>.", "Next sync <clock>" / "Next sync is worked out when you save." /
"Syncs only when someone presses Sync now."; read-only label "Syncs".

Syncing: "Quick change check" ("Skips looking a show up again when the
source says it hasn't changed. Faster on large collections."), "Delete
shows the source removes" ("When the source stops listing a show, its
files are deleted too. Off, Shakedown keeps its copy and marks it
Disappeared.").

Handoff notice: "Tells your music tools that new shows arrived.";
"Webhook URL" ("Shakedown posts here after a sync with new shows.
{collection} becomes the collection's name."); "Command" ("Set in
shakedown.yaml. The dashboard can't change a command.").

Remove: card "Remove collection", "Stops syncing <name>. You choose what
happens to its files.", button "Remove collection…". Dialog "Remove
<name>?", "Shakedown stops syncing it. Deleted files can't be brought
back." (+ " Your other unsaved changes are saved with it."); "Keep
files" ("N shows (size) stay on disk, listed under Removed collections.
Adding <name> back reuses them."), "Delete library folders" ("The
archive stays, so adding <name> back rebuilds them with no download.
Frees almost no space: library files are links into the archive."),
"Delete library folders and archived shows" ("Also deletes N shows
(size) from the archive. Adding <name> back downloads them all
again."); "Cancel"; "Remove, keep files" / "Remove, delete library
folders" / "Remove, delete everything" ("Removing…"). Toasts: "Removed
<name>" / "Its files stay, listed under Removed collections." or
"Deleting its files. It shows under Running now."; "Removed <name>,
files kept" / "A run is in progress, so nothing was deleted. Delete them
under Removed collections when it ends." (other refusals: their words,
then "Delete them under Removed collections.").

Read-only: "Settings are read-only", "shakedown.yaml was written outside
Shakedown. To change this collection here, allow dashboard edits on the
Settings page." Errors: "This collection has an error" / "This
collection has N errors".

Add source: "Add source", "A site to mirror from. Its first collection
comes next.", "Name" ("Fixed once saved: its shows are filed under
it."; "There's already a source called <name>."), "Type" ("The plugins
installed in this Shakedown."), "Cancel", "Add source". List: "No
collections yet.", "Not saved yet", "New collection".

Unknown route: "There's no collection called <c> in <s>." / "There's no
source called <s>.", "Back to Settings".

## Behavior

- Routes: `#settings/<source>/<collection>` edits;
  `#settings/<source>?add` adds; `?add=<name>` adds a removed collection
  back under its name. A query, not a path segment, so a collection may
  be called anything. Changing route scrolls to the top.
- The editor changes the same draft as the Settings list; one save bar
  saves both. Saving a new collection moves to its own route
  (`location.replace`). Discard on a new collection drops it and goes to
  `#settings`.
- A new collection starts with the staggered default (§spec:schedules):
  Weekly, Sunday, at the first whole hour from 03:00 no other collection
  uses; passthrough layout; every switch off; no handoff.
- Repeat writes a cron expression (Daily `m h * * *`, Weekly
  `m h * * d`); Manual only writes `null`; Custom keeps whatever was
  there and stays Custom even when the expression reads as a preset.
  The time is held as typed until it reads as `H:MM` or `HH:MM`.
- Pasted links: text that is a web link is sent to
  `GET /api/source-link?type=&url=` (only for a type with
  `reads_links`). The answer replaces the field only if it still holds
  that link; a later edit clears the note. The page has no link rules
  of its own (§spec:dashboard-settings "Pasted links").
- Remove saves the file without the collection at once (other unsaved
  edits go with it), goes to `#settings`, then starts the delete chosen.
  The dialog opens on Keep files every time.
- Add source appends `{name, type, collections: []}` to the draft and
  opens `#settings/<name>?add`. Sources are never removed here.
- While locked: every value is text, no Remove, no Add source, no Add
  collection.

## Open questions

None. Round S3 (2026-11-25): the operator approved the editor, adding
and adding back, the remove dialog, the read-only and error states, and
pasted links (Q-S8b). They chose to have each source plugin read its own
links (Q-S8c), so the page asks `GET /api/source-link`, which waits on
§road:source-links. The link help words became source-neutral to match.
