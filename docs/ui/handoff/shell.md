# Handoff: Page shell and page-wide states

*Approved: round 2, 2026-11-12; never-reached state 2026-11-13 · Kit: §spec:ui-kit · Endpoints: all six `GET /api/*` (§spec:dashboard)*
*Code: `web/src/App.tsx`, `web/src/data/useDashboard.ts` · Fixtures: `web/src/fixtures/scenarios.ts`*

## Purpose

Frame every tab, and tell the operator when the data on screen is old or
the config is broken, so old numbers are never passed off as current.

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| "Release 1.9.0" | `health.release` | as sent | hidden until health answers |
| "Updated 21:40" | `activity.generated_at`; when stale, the oldest `generated_at` on screen | `clock()` (HH:MM today, "11 Nov, 02:10" otherwise) | hidden until activity answers |
| Dot beside Updated | stale or not | `bg-success` / `bg-warning`, `size-2 rounded-full`, decorative | — |
| Config error text | `health.config.error`, or the 503 body's `detail` | mono xs, `whitespace-pre-wrap`, `wrap-anywhere` | — |
| "loaded at 18:02" | `health.config.last_loaded_at` | `clock()` | sentence omitted |

## Layout

`max-w-6xl` centred column, `p-4 sm:p-6`, `gap-4`. Header row: h1
"Shakedown" (`text-xl` semibold), release (`text-sm` muted), and
Updated pushed right (`ml-auto`); wraps on narrow screens. Notices
(Alert) sit under the header, above the tabs. Tabs (shadcn Tabs):
Overview · Runs · Sources · Library; full width below 640 px
(`w-full`), fitted and left-aligned from 640 px (`sm:w-fit
sm:self-start`). Tab content sits in the single `main` landmark.

## States

Rendered at 390/768/1440 in light and dark (`npm run render`).

| State | Trigger (scenario) | What changes |
| --- | --- | --- |
| default | `default` | header, tabs, content |
| can't reach Shakedown | `unreachable`: answers, then requests fail for 4 min | warning Alert "Can't reach Shakedown" / "Showing data from 4 min ago."; dot amber; Updated shows when the oldest answer arrived; content keeps the last data |
| never reached | `never_reached`: every request fails before any answer | warning Alert, description "Nothing to show until it answers."; tabs hidden |
| config broken | `config_broken`: `health.config.valid` false | destructive Alert "shakedown.yaml has an error", the error text, "Showing data from the config loaded at 18:02."; content keeps showing data |
| config never loaded | `no_config`: every endpoint 503 `{detail}` | destructive Alert with the error and "Nothing to show until it loads."; tabs hidden |
| loading | `loading`: no answer yet | every card shows Skeleton rows; header shows "Shakedown" only |

Stale rule (§spec:dashboard): stale when any endpoint's last request
failed while its data is on screen, or an answer's `generated_at` is
older than twice its interval (live 5 s, others 30 s). "Showing data
from <age> ago" uses the oldest `generated_at` on screen.

## Copy

"Shakedown" · "Release <x>" · "Updated <time>" · "Can't reach
Shakedown" · "Showing data from <age> ago." · "Nothing to show until it
answers." · "shakedown.yaml has an error" · "Showing data from the
config loaded at <time>." · "Nothing to show until it loads." · tab
labels Overview, Runs, Sources, Library. Terms: Shakedown, Release
(§spec:ui-kit Terms).

## Behavior

- Polling (`useDashboard`): `activity` every 5 s; `network?days=30`,
  `runs?days=30`, `library`, `attention`, `health` every 30 s. All
  stop while `document.hidden`; on becoming visible, fetch at once.
  Relative URLs `./api/<path>`, `cache: "no-store"`. A failed request
  keeps the previous data; an answer to an older request than the last
  one applied is dropped. A failed request counts toward "can't reach
  Shakedown" whether or not that endpoint has answered before. A `503`
  after data arrived (serve restarted with a broken config) shows the
  config-broken notice over the kept data.
- A `useNow` clock ticks every 5 s so ages and staleness update between
  answers.
- The open tab lives in the URL hash (`#runs`, `#sources`, `#library`;
  none = Overview); switching tabs scrolls to top and adds one history
  entry, so Back/forward move between tabs.
- Every source-derived string renders as React text, never markup.

## Open questions

None.
