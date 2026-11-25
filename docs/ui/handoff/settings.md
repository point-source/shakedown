# Handoff: Settings tab

*Approved: round S2, 2026-11-24 · Kit: §spec:ui-kit · Endpoints: `GET /api/config`, `PUT /api/config`, `POST /api/config/override`, `POST /api/collections/purge`, `POST /api/history/trim` (§spec:dashboard-settings, §spec:edit-lock, §spec:collection-removal, §spec:run-error-retention)*
*Code: `web/src/views/Settings.tsx`, `web/src/settings/` (`draft.ts`, `fields.tsx`, `General.tsx`, `Sources.tsx`, `Removed.tsx`, `AddSource.tsx`, `route.ts`), `web/src/lib/schedule.ts`, `problemWords`/`WARNING` in `web/src/lib/words.ts`*

## Purpose

Change what Shakedown mirrors and how, without editing shakedown.yaml by
hand; see why a file Shakedown didn't write can't be changed here.

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| "Saved to <path>." | `config.path` | as is | — |
| Archive folder, Library folder | `document.archive_root`, `library_root` | as typed | — |
| Database file | `document.state_db` | as typed | placeholder "In the archive folder"; empty saves `null` |
| Collections at once, Downloads at once | `max_concurrent_collections`, `max_concurrent_downloads` | whole number | — |
| Time zone | `timezone` | as typed | placeholder "The container's clock"; empty saves `null` |
| Keep run errors for (days) | `keep_run_errors_days` | whole number | — |
| Webhook URL (Failure notice) | `notifications.on_failure.webhook` | as typed | placeholder "None"; empty saves `notifications: null` |
| Source card title, "Type x" | `sources[].name`, `.type` | as is | — |
| Requests at once | `sources[].max_concurrent_requests` | whole number | placeholder "Same as downloads at once"; empty saves `null` |
| Collection row | `collections[].name`, `.schedule`, `next_syncs[].next_at` | `scheduleLine()`: "Daily at 03:30", "Weekly, Sunday at 03:00", "Manual only", "Custom: <expr>"; "Next sync " + `clock()` | Next sync omitted when absent |
| "N settings have errors" on a row | `errors[]` whose `loc` is under that collection | count | omitted at 0 |
| Error under a setting | `errors[]` (or a 422 save's `errors`) by `loc` | `problemWords(code, loc)` | omitted |
| Errors with no field here | `errors[]` whose `loc` this page doesn't show | destructive Alert: "<key › path>: <words>" | Alert omitted |
| Warning under a setting | 200 save's `warnings[]` by `loc` | `WARNING[code]`, several per setting | omitted; cleared by the next edit |
| Removed collections | `unconfigured[]` | "<source> · N shows · <archive size>"; " · library folders deleted" when `library_bytes` is 0 | card omitted when empty |

## Layout

One column of cards under the page text "Saved to <path>. Runs use
changes from their next start." From 1024 px a two-column grid: Folders
and Failure notice on the left, Runs on the right; below that an `h2`
"Sources" with an outline "Add source" button beside it (editable
only; its dialog is in the collection editor handoff), then one Card per
source, then Removed collections. Below 1024 px every card stacks in
that order.

- **Runs**: Collections at once and Downloads at once side by side from
  640 px; then Time zone, Keep run errors for (days), and the Trim
  history now button with its line.
- **Source card**: CardAction "Add collection" (editable only). Requests
  at once, half the card's width from 1024 px. "Collections" then a
  bordered list, one link row per collection (name, schedule, Next sync,
  error count, ChevronRight); from 1024 px the row's facts sit in fixed
  columns, below they stack.
- **Save bar**: fixed to the bottom edge, full width, `border-t
  bg-background`, appears on the first edit; the page gains bottom
  padding so the last card clears it. From 640 px, toasts sit above it
  while it shows, so a toast never covers Save.
- Components: Card, Field (FieldLabel, FieldDescription, FieldError),
  Input, Button, Alert, AlertDialog, Dialog, RadioGroup, Label, toast.

## States

| State | Scenario | What changes | Golden |
| --- | --- | --- | --- |
| default (editable) | `default` | Every setting as an Input; Add source, Add collection, Add back | `default-settings` |
| read-only | `settings_locked` | Alert "Settings are read-only" with Allow dashboard edits; settings as label plus text ("Not set" or the placeholder when empty); no Add collection or Add back; Trim history now and Delete files… stay | `settings-locked-settings` |
| file with errors | `settings_invalid` | Errors under Time zone and Keep run errors for; "1 setting has an error" on phish; destructive Alert at the top for `max_concurrent_collectons` | `settings-invalid-settings` |
| unsaved | `settings_saved`, after an edit | Save bar "Unsaved changes", Discard, Save | `settings-dirty` |
| saved with warnings | `settings_saved`, after Save | Both warnings under Library folder; toast "Saved, with warnings" | `settings-saved-warnings` |
| refused (422) | `settings_refused`, after Save | Field red with "Enter 30 or more."; bar "1 setting needs fixing before it saves"; toast "Nothing saved"; the edit stays | `settings-refused` |
| file changed (409 changed) | `settings_conflict`, after Save | AlertDialog "shakedown.yaml changed": Not now, Reload settings | `settings-conflict` |
| delete dialog | `settings_saved`, Delete files… then the second choice | AlertDialog "Delete files for dmb?", destructive "Delete everything" | `settings-delete` |
| override confirm | `settings_locked`, Allow dashboard edits | AlertDialog "Allow dashboard edits?" | `settings-override` |
| write failed (500) | `settings_write_failed` | Toast "Shakedown couldn't write the file"; the edit stays | behaviour test |
| loading | no answer yet | One Card with RowsSkeleton | — |

Interaction goldens are shot at the viewport; the three page states are
full-page.

## Copy

Page: "Saved to <path>. Runs use changes from their next start."

Folders: "Folders", "Paths inside the Shakedown container."; "Archive
folder" ("Where Shakedown keeps every downloaded file."), "Library
folder" ("The folders your music tools read."; while a collection's
command uses `{staging_root}`, shown as text with "Set in
shakedown.yaml: a collection's command uses this path."), "Database
file" ("Shakedown's records of every show and run.").

Runs: "Collections at once" ("How many collections sync at the same
time."), "Downloads at once" ("Per collection."), "Time zone" ("The
clock schedules follow, such as Europe/London."), "Keep run errors for
(days)" ("At least 30. After that a run keeps its result and counts."),
"Trim history now" ("Starting…"), "Drops older error details now and
gives the space back to the disk."

Failure notice: "Failure notice", "Shakedown posts to this address when
a run fails.", "Webhook URL".

Sources: "Sources", "Add source", "Type <type>", "Add collection", "Requests at once" ("The most
connections Shakedown opens to this source, across all its
collections."), "Collections", "Next sync <clock>", "1 setting has an
error" / "N settings have errors"; screen-reader "Edit <name>" / "Show
<name>".

Removed collections: "Removed collections", "No longer synced. Their
files are still on disk.", "Add back", "Delete files…". Dialog "Delete
files for <name>?", "Deleted files can't be brought back.", "Library
folders" ("The archive stays, so adding <name> back rebuilds them with
no download. Frees almost no space: library files are links into the
archive."), "Library folders and archived shows" ("Also deletes N shows
(<size>) from the archive. Adding <name> back downloads them all
again."), "Cancel", "Delete library folders" / "Delete everything".
Toasts: "Deleting files" / "<name>. It shows under Running now.";
"<name> is in shakedown.yaml again" / "Nothing was deleted."; "Nothing
left to delete".

Read-only: "Settings are read-only", "shakedown.yaml was written outside
Shakedown, so the dashboard doesn't change it. Sync now,
Validate, Trim history now, and deleting removed collections' files
still work.", "Allow dashboard edits". Confirm: "Allow dashboard
edits?", "If shakedown.yaml is later changed where it came from, by hand
or by a deploy, that version replaces what you change here. This stays
on: the dashboard can't lock the file again.", "Cancel", "Allow
dashboard edits". Toast "Dashboard edits allowed".

Saving: "Unsaved changes", "1 setting needs fixing before it saves" /
"N settings need fixing before they save", "Discard", "Save"
("Saving…"). Toasts: "Settings saved" / "Runs use them from their next
start."; "Saved, with warnings" / "Each one is under its setting.";
"Nothing saved" / "Fix the settings marked in red, then save again.";
"Settings are read-only now" / "Nothing saved."; "A run is in progress"
/ "Shakedown can't switch databases during a run. Save again when it
ends."; "Shakedown couldn't write the file" / "Check that <path> isn't
read-only. Nothing changed." Conflict: "shakedown.yaml changed",
"Someone changed the file after you opened Settings, so nothing was
saved. Reload to see their version, then make your changes again.",
"Not now", "Reload settings". Error and warning words: `problemWords`
and `WARNING` in `web/src/lib/words.ts`; "shakedown.yaml has an error" /
"shakedown.yaml has N errors" heads the list of errors with no field.

## Behavior

- The draft is a copy of `document`; each edit changes it by path, and
  Save sends it whole with the revision it was loaded under
  (`PUT /api/config {revision, document}`). Whole numbers save as
  numbers; anything else is sent as typed so the server's check names
  the problem.
- A fresh `GET /api/config` replaces the draft only when its revision is
  new and nothing is being edited; after a save the values stay up until
  the refresh brings the file back. Discard and Reload settings return
  to the last answer.
- After a save, a delete, or the override, the page
  refetches `GET /api/config` and `GET /api/health`.
- Editing any setting clears the last save's warnings. A 422's errors
  replace the file's errors until the next answer with a new revision.
- While locked: no save bar, no Inputs, no Add collection, no Add back;
  Trim history now and Delete files… still work.
  Allow dashboard edits confirms, then posts the override; there is no
  way back.
- The delete dialog opens on "Library folders" every time.
- Collection rows link to `#settings/<source>/<collection>`, Add
  collection to `#settings/<source>?add`, Add back to
  `#settings/<source>?add=<collection>` (the collection editor,
  `docs/ui/handoff/collection.md`). A collection being added shows in
  its source's list as "Not saved yet". The tab is chosen by the hash's
  first segment.

## Open questions

None. Round S2 (2026-11-24): the operator approved the page and its
words, the read-only view and the override confirm, the save states,
and the login and delete dialogs as rendered. 2026-11-25: the operator
dropped the login, since no archive.org download needs an account
(§spec:dashboard-credentials); the login row, dialog, and toasts are
gone and the source card keeps Requests at once. Round S3
(2026-11-25): Add source and the raised toasts were approved with the
collection editor.
