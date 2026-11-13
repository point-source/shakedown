# Handoff: Sources tab

*Approved: round 1, 2026-11-12; loading state 2026-11-13 · Kit: §spec:ui-kit · Endpoints: `GET /api/network?days=30`, `/api/activity` (`network`)*
*Code: `web/src/views/Sources.tsx`*

## Purpose

Is a source throttling Shakedown: speed now and over 30 days, and when
rate limits, errors, retries, and back-offs happened.

## Data

| Shown | From (field) | Format | When null or empty |
| --- | --- | --- | --- |
| Card title | `sources[].source` | as sent | — |
| Speed now / requests in flight | `activity.network[source].speed_bytes_per_second` / `.requests_in_flight` | `speed()`; "N request(s) in flight" | "0 B/s", "0 requests in flight" when the source has no run in progress |
| Speed chart | `series[].bytes / bucket_seconds / 1e6` per `t` | step Area, `--chart-1`, MB/s, x ticks "14 Oct" | "No downloads in the last 30 days." (empty series) |
| Rate limits and errors bars | `rate_limited + server_errors + timeouts + connection_errors` per bucket | Bar, `--destructive`, integer ticks | part of the chart block |
| Events (first 10) | `events[]` newest first | When `clock(at)`; What `EVENT[kind]` + " (status)" + " · waited <duration>"; `detail` mono xs; Show `identifier` mono xs | "No rate limits, errors, or retries in the last 30 days." |
| "and N more in the last 30 days" | `events_total − 10` | `count()` | omitted |

## Layout

One card per source, in server order. Header: title left; speed and
requests in flight right (CardAction). Body: "Download speed, MB/s,
last 30 days" chart (h-40), "Rate limits and errors per hour" bars
(h-16), then "Rate limits, errors, and retries" table (When · What ·
Show; on phone the Show column folds under What).

## States

| State | Scenario | What changes |
| --- | --- | --- |
| default | `default` | two sources, one downloading, one discovering |
| first install | `first_install` | empty chart and events texts |
| long content | `long_content` | as default; events capped at 10 |
| loading | `loading` | Skeleton |

## Copy

"<n> request(s) in flight", chart titles above, event words Rate
limited, Server error, Timed out, Connection failed, Refused, Retry,
Back-off; "waited <duration>"; empty texts above.

## Behavior

Chart data refreshes every 30 s; speed now every 5 s. Charts carry
`role="img"` and an `aria-label` naming the source and window.

## Open questions

None. The event words joined the Terms (round 2).
