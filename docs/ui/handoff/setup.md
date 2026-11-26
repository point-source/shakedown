# Handoff: First-run setup and the page with no config

*Approved: round S4, 2026-11-25 · Kit: §spec:ui-kit · Endpoints: `GET /api/config`, `GET /api/setup`, `POST /api/config/check-paths`, `POST /api/setup`, `GET /api/source-link` (§spec:first-run-setup, §spec:dashboard-settings)*
*Code: `web/src/views/Setup.tsx`, the no-config routing in `web/src/App.tsx`, `ScheduleSetting` and `SearchSetting` in `web/src/settings/Collection.tsx`*

## Purpose

Get a new install from no shakedown.yaml to a first sync without
writing YAML, on a phone. When a file exists but won't load, say what
to do: fix it in Settings, or by hand.

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| Archive folder, Library folder | `setup.archive_root`, `.library_root` | Input | — |
| Folder check | `POST /api/config/check-paths` `{archive_root, library_root}` → `warnings` | a line under the folders; each warning under its folder (`WARNING` words) | "Checking the folders…" until it answers |
| Source | `setup.source_types[].type` | Select | "Choose a source" while several are installed; the only one, chosen, when one is |
| Source name | `sources[0].name` | Input; follows Source until typed in | — |
| Collection name, Search | `sources[0].collections[0].name`, `.query` | Input; Textarea, 2 rows | — |
| Repeat, Day, Time | `setup.schedule` via `readSchedule()` | as in the collection editor | — |
| Clock note | the browser's time zone (`Intl…resolvedOptions().timeZone`) | "Times follow <tz>, this browser's time zone. Change it in Settings." | "Times follow the container's clock." |
| File path | `config.path` | "Writes <path>." | — |
| Errors with no field here | `422 errors[]` whose `loc` matches no field | destructive Alert, "<key › path>: <words>" | omitted |
| Unreadable file's error | the 503 `detail` | mono xs, `whitespace-pre-wrap` | — |

## Layout

Shell header, then one column up to `max-w-2xl`, left-aligned with the
header: `h2` "Set up Shakedown" and its lead; the errors Alert when it
applies; cards Folders, What to mirror, Schedule; then Finish setup and
the file line. No tabs. Below 640 px Finish setup is full width and the
file line sits under it; from 640 px they share a row. Schedule's
Repeat, Day, and Time take three columns from 640 px.

With no config loaded the shell routes on `GET /api/config` `state`:
`missing` → setup; `invalid` → a destructive Alert, then the Settings
tab's page (list and editor routes work) with no tabs; `unreadable` →
the config-error Alert alone. Until the config answers, the header
shows alone.

- Components: Card, Field, Input, Textarea, Select, Alert, Button,
  toast.

## States

| State | Scenario | What changes | Golden |
| --- | --- | --- | --- |
| setup | `setup` | defaults; check passed | `setup` |
| folder warning | `setup_warnings` | hardlink warning under Library folder; "You can still finish setup…" | `setup-warnings` |
| required | `setup`, Finish setup with nothing filled | Source, Collection name, Search marked "This setting is needed."; Source name isn't marked while no source is chosen; toast | `setup-required` |
| link read | `setup`, Source ia, a pasted archive.org link | Search `collection:BobWeir`; "Read from the link you pasted. Check it before you finish." | `setup-link` |
| file with errors | `config_invalid` | Alert "Shakedown can't load shakedown.yaml"; the Settings page with its errors | `config-invalid` |
| unreadable file | `config_unreadable` | config-error Alert with the YAML error and "Fix <path> by hand." | `config-unreadable` |
| check failed | check-paths errors | warning line, Check again | behaviour test |
| finished | `setup`, Finish setup | toast "Shakedown is set up"; Overview | behaviour test |
| sync didn't start | `setup_warnings`, Finish setup | warning toast with the reason | behaviour test |
| refused | `422 {errors}` | errors under their fields; the rest in the Alert; toast | behaviour test |
| file appeared | `409 exists` | toast; Settings | behaviour test |
| write failed | `500 write_failed` | toast; setup stays | behaviour test |

All goldens are full page.

## Copy

Header: "Set up Shakedown", "There's no shakedown.yaml yet. Choose
where shows go and a first collection to mirror. Shakedown writes the
file and starts the first sync."

Folders: "Paths inside the Shakedown container. The defaults suit the
example setup."; "Archive folder" ("Where Shakedown keeps every
downloaded file."), "Library folder" ("The folders your music tools
read."); "Checking the folders…"; "Both folders work, and library files
can link to the archive."; with warnings: "You can still finish setup,
and change the folders later in Settings."; failed: "Shakedown couldn't
check the folders. You can still finish setup.", "Check again".

What to mirror: "One collection to start. Add more in Settings.";
"Source" ("Choose a source"; "The plugins installed in this
Shakedown."), "Source name" ("Fixed once saved: shows are filed under
it."), "Collection name" ("Fixed once saved: its shows are filed under
it."), "Search" (the collection editor's words, ending "Check it before
you finish.").

Schedule: the collection editor's words; "The first sync starts when you
finish. After that, it follows this schedule." (Manual only: "… After
that, it syncs only when someone presses Sync now.").

Finish: "Finish setup" ("Setting up…"), "Writes <path>. Change anything
later in Settings." Errors Alert: "Setup has an error" / "Setup has N
errors".

Toasts: "Setup isn't finished" / "Fill in the settings marked in red."
(or "Fix the settings marked in red, then finish again." after a
refusal); "Shakedown is set up" / "The first sync of <c> has started. It
shows under Running now.", or (warning) "The first sync of <c> didn't
start: another run is going. Press Sync now when it ends." / "The first
sync of <c> didn't start. Press Sync now to try again.";
"shakedown.yaml already exists" / "It was made while you were setting
up, so nothing was written. Here are its settings."; "Shakedown couldn't
write the file" / "Check that <path> isn't read-only. Nothing was set
up."; "Shakedown couldn't write its database" / "Check that <path> isn't
read-only. shakedown.yaml was written; the first sync didn't start.";
others as every write (`refusal`).

No config: "Shakedown can't load shakedown.yaml" / "Nothing runs until
it loads. Fix the settings marked below, or edit the file by hand. The
other tabs open once it loads." Unreadable: "shakedown.yaml has an
error", the error, "Shakedown can't read the file, so the dashboard
can't change it. Fix <path> by hand. Nothing to show until it loads."

## Behavior

- `GET /api/setup` gives the defaults; a skeleton card shows until it
  answers.
- The folder check runs once on opening and again when focus leaves the
  folders with either path changed. A newer check's answer wins. A
  warning never blocks Finish setup (§req:constraints: path checks warn,
  never block).
- Source starts empty with several plugins installed, so the page ranks
  none; the name follows the chosen type until someone types one.
- Finish setup checks for an empty Source, Source name, Collection name,
  or Search first and sends nothing then. Otherwise it posts
  `{document}` holding only `archive_root`, `library_root`, `timezone`,
  and the one source with its one collection (`name`, `query`,
  `schedule`); every other key stays at its default in the file.
- 201: everything reloads, the hash clears (Overview), and the toast
  says whether the first sync started. The answer's warnings are not
  shown again: the folder check showed them before Finish.
- 409 exists, and a database `write_failed`: the file is there, so the
  page reloads into Settings.
- No login is asked (§spec:dashboard-credentials).
- With an `invalid` file the Settings page saves as usual; a save
  reloads every endpoint, so the tabs return once the file loads.

## Open questions

None. Round S4 (2026-11-25): the operator approved setup as one page
(folders, what to mirror, schedule) and the two broken-file states: an
unreadable file is fixed by hand, and a file with errors opens in the
Settings editor.
