// Review and screenshot scenarios: answers keyed by endpoint path, in the exact
// shapes of src/api/types.ts, with a fixed clock. Served to the real page by
// intercepting its requests (scripts/render.mjs, tests/screens.spec.ts).
import type {
  SetupAnswer, SourceLinkAnswer,
  ActivityAnswer, AttentionAnswer, AttentionEntry, ConfigAnswer, HealthAnswer, HistoryRun, LibraryAnswer, NetworkAnswer,
  RunsAnswer,
} from "@/api/types"
import * as base from "./default.ts"
import { config as baseConfig } from "./config.ts"

export type Answers = {
  activity: ActivityAnswer
  "network?days=30": NetworkAnswer
  "runs?days=30": RunsAnswer
  library: LibraryAnswer
  attention: AttentionAnswer
  health: HealthAnswer
  config: ConfigAnswer
  /** `GET /api/setup`: only setup scenarios answer it. */
  setup?: SetupAnswer
} & { [link: `source-link?${string}`]: SourceLinkAnswer }

/** An answer to a write (POST/PUT/DELETE), keyed "<METHOD> <path after the page>",
 *  e.g. "POST sync?source=internetarchive&collection=phish" or "PUT api/config". */
export type Writes = Record<string, { status: number; body: unknown }>

export interface Scenario {
  now: string
  /** How the server behaves: answer normally, answer 503 with the config error
   *  (`config`, `setup`, and `source-link` still answer, as they need no config),
   *  answer first and then stop (advance the clock by `minutes`), or never answer. */
  mode: { kind: "ok" } | { kind: "no_config"; detail: string } | { kind: "goes_away"; minutes: number } | { kind: "hangs" }
    | { kind: "restarts_broken"; detail: string; minutes: number } | { kind: "refused" }
  answers: Answers
  /** Answers to the page's writes; a write with no entry answers 404. */
  writes?: Writes
  /** Endpoints that always answer this HTTP error status instead of their answer. */
  fail?: Partial<Record<keyof Answers, number>>
}

const NOW = base.NOW
const at = (minutesAgo: number) =>
  new Date(Date.parse(NOW) - minutesAgo * 60_000).toISOString().replace(".000Z", "+00:00")
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v))

// What the ia plugin answers for the links the editor's tests and renders paste
// (§spec:dashboard-settings "Pasted links"); any other link reads as unreadable.
const LINKS: [string, string | null][] = [
  ["https://archive.org/details/GratefulDead", "collection:GratefulDead"],
  ["https://archive.org/details/BobWeir", "collection:BobWeir"],
  ["https://archive.org/details/etree?query=phish&tab=collection", "collection:etree AND (phish)"],
  ["https://archive.org/search?query=collection%3APhish+AND+year%3A1997", "collection:Phish AND year:1997"],
  ["https://archive.org/advancedsearch.php?q=creator%3A%22Grateful+Dead%22&fl%5B%5D=identifier", 'creator:"Grateful Dead"'],
  ["https://example.net/bob-weir", null],
  ["https://example.net/shows", null],
]
const linkAnswers = () => Object.fromEntries(
  LINKS.map(([url, query]) => [`source-link?${new URLSearchParams({ type: "ia", url })}`, { query }]),
) as Record<`source-link?${string}`, SourceLinkAnswer>

const defaultAnswers = (): Answers => clone({
  ...linkAnswers(),
  activity: base.activity,
  "network?days=30": base.network,
  "runs?days=30": base.runs,
  library: base.library,
  attention: base.attention,
  health: base.health,
  config: baseConfig,
})

const CONFIG_ERROR = "config validation failed:\nmax_concurrent_collectons: Extra inputs are not permitted"

function quiet(): Answers {
  const a = defaultAnswers()
  a.activity.runs = []
  a.activity.network = []
  a.attention.entries = []
  a["runs?days=30"].runs = a["runs?days=30"].runs.filter((r) => r.finished_at)
  return a
}

function firstInstall(): Answers {
  const a = defaultAnswers()
  const zero = { pending: 0, downloading: 0, complete: 0, failed: 0, unavailable: 0, disappeared: 0, pruned: 0 }
  a.activity.runs = []
  a.activity.network = []
  for (const s of a["network?days=30"].sources) { s.series = []; s.events = []; s.events_total = 0 }
  a["runs?days=30"].runs = []
  a.library = {
    generated_at: NOW,
    disk: { total_bytes: 16_000_000_000_000, free_bytes: 15_870_000_000_000 },
    totals: { counts: zero, restricted: 0, archive_bytes: 0, library_bytes: 0 },
    collections: a.library.collections.map((c) => ({ ...c, counts: zero, restricted: 0, archive_bytes: 0, library_bytes: 0, last_successful_sync_at: null })),
    growth: a.library.collections.map((c) => ({ date: NOW.slice(0, 10), source: c.source, collection: c.collection, complete: 0, archive_bytes: 0 })),
    recently_mirrored: [],
  }
  a.attention.entries = []
  a.attention.health = a.attention.health.map((h) => ({
    ...h, restricted: [], restricted_count: 0, removed_upstream: [], removed_upstream_count: 0, drift_files: 0,
    not_playable: [], not_playable_count: 0, playable_copies: [], playable_copies_count: 0,
  }))
  a.health.validation = a.health.validation.map((v) => ({ ...v, status: null, at: null }))
  return a
}

function configBroken(): Answers {
  const a = defaultAnswers()
  a.health.config = { valid: false, error: CONFIG_ERROR, last_loaded_at: at(182) }
  return a
}

function longContent(): Answers {
  const a = defaultAnswers()
  const long = "grateful-dead-soundboards-and-matrix-recordings-1965-1995"
  const longId = "gd1977-05-08.sbd.hicks.4982.sbeok.shnf.remastered-by-charlie-miller-2023-flac24-96k"
  a.activity.runs[0].collection = long
  a.activity.runs[0].current_items = [longId, "gd1972-08-27.sbd.orf.111.sbeok.shnf", "gd1974-06-18.sbd.miller.flac16", "gd1990-03-29.sbd.miller.flac24", "gd1978-04-24.aud.vernon.shnf"]
  a.activity.runs[0].items_failed = 1_204
  const failed = Array.from({ length: 300 }, (_, i) => `gd19${66 + (i % 30)}-0${1 + (i % 9)}-${String(1 + (i % 28)).padStart(2, "0")}.sbd.r${i}.flac16`)
  const entries: AttentionEntry[] = [
    { kind: "failed_shows", source: "internetarchive", collection: long, since: null, identifiers: base.first(failed), count: failed.length,
      detail: { recovery: { operation: "sync", status: "completed_with_item_issues", started_at: at(800), updated_at: at(700), finished_at: at(700),
        phase: "downloading", affected_item: null, affected_path: null, completed_work: null, preservation_context: null, deletion_context: null,
        safe_next_action: "fix failed item issues and rerun sync", errors: null } } },
    { kind: "recovery", source: "internetarchive", collection: "phish", since: at(300), identifiers: ["ph1997-11-22.flac16"], count: 1,
      detail: { operation: "restage", status: "failed_before_completion", started_at: at(320), updated_at: at(300), finished_at: at(300),
        phase: "staging", affected_item: "ph1997-11-22.flac16", affected_path: "/data/library/Phish/1997-11-22 - Hampton Coliseum",
        completed_work: null, preservation_context: null, deletion_context: null, safe_next_action: "fix storage/path/layout and rerun restage", errors: null } },
    { kind: "stale", source: "etree-lma", collection: "etree-lma", since: null, identifiers: [], count: 0, detail: {} },
    { kind: "rescheduled", source: "internetarchive", collection: "phish", since: at(90), identifiers: [], count: 0, detail: {} },
    { kind: "handoff_pending", source: "etree-lma", collection: "etree-lma", since: at(2533), count: 41,
      identifiers: base.first(Array.from({ length: 41 }, (_, i) => `lma-ph2003-07-${String(1 + (i % 30)).padStart(2, "0")}-${i}`)),
      detail: { shows: 41, last_reason: "HTTPConnectionPool(host='beets', port=8337): Max retries exceeded with url: /api/import (Caused by NewConnectionError: Failed to establish a new connection: [Errno 111] Connection refused)" } },
    { kind: "failure_notice_undelivered", source: "internetarchive", collection: "phish", since: at(1121), identifiers: [], count: 0,
      detail: { failure_notice_error: "apprise: no service accepted the notification (ntfy://ntfy.lan/shakedown: 403 Forbidden)", run_id: 486 } },
    { kind: "separations", source: "internetarchive", collection: long, since: null, identifiers: ["gd1977-05-08.sbd.hicks.4982.sbeok.shnf", "gd1977-05-08.aud.vernon.flac16"], count: 2,
      detail: { separations: [] } },
    { kind: "unplaced_files", source: "internetarchive", collection: "phish", since: null, identifiers: [], count: 4,
      detail: { unplaced_files: ["/data/library/Phish/1997-11-22 - Hampton Coliseum/cover.jpg", "/data/library/Phish/.DS_Store", "/data/library/Phish/1995-12-31 - Madison Square Garden/notes.txt", "/data/library/Phish/1993-08-13/folder.jpg"] } },
    { kind: "unidentified_files", source: "internetarchive", collection: long, since: null,
      identifiers: ["gd1982-10-10.aud.berger.98765.flac16", "gd1990-03-29.sbd.miller.27720.flac16"], count: 5,
      detail: { unidentified_files: [
        { identifier: "gd1982-10-10.aud.berger.98765.flac16", file: "gd82-10-10d1t01.flac" },
        { identifier: "gd1982-10-10.aud.berger.98765.flac16", file: "gd82-10-10d1t02.flac" },
        { identifier: "gd1982-10-10.aud.berger.98765.flac16", file: "gd82-10-10d2t07 Drums > Space.flac" },
        { identifier: "gd1990-03-29.sbd.miller.27720.flac16", file: "gd90-03-29d1t03.flac" },
        { identifier: "gd1990-03-29.sbd.miller.27720.flac16", file: "gd90-03-29d3t01.flac" },
      ] } },
  ]
  a.attention.entries = entries
  const kinds = ["sync", "verify", "catch_up", "restage", "reconcile", "refetch"] as const
  const extra: HistoryRun[] = Array.from({ length: 24 }, (_, i) => ({
    run_id: i % 2 ? null : 400 + i, activity_id: 800 + i, kind: kinds[i % kinds.length], source: "internetarchive",
    collection: i % 3 ? "phish" : long, started_at: at(3000 + i * 1440), finished_at: at(2950 + i * 1440), duration_seconds: 3000,
    result: i % 5 === 0 ? "failed_before_completion" : "completed", end_reason: null, items_discovered: 4113, items_new: i, items_updated: 0,
    items_failed: i % 5 === 0 ? 2 : 0, bytes_downloaded: i * 1_200_000_000,
    errors: i % 5 === 0 ? ["source enumeration failed: HTTPSConnectionPool(host='archive.org', port=443): Read timed out. (read timeout=30)", "gd1977-05-08: checksum mismatch after 3 attempts", "gd1978-01-22: checksum mismatch after 3 attempts"] : [],
    failure_notice_error: i === 5 ? "apprise: no service accepted the notification" : null,
    detail_trimmed: false, runs_trimmed: null, bytes_freed: null,
  }))
  a["runs?days=30"].runs.push(...extra)
  a.library.collections[0].collection = long
  for (let i = 0; i < 3; i++) {
    a.library.collections.push({ ...clone(a.library.collections[1]), source: "phishin", collection: `phish-in-${["1983-1989", "1990-1999", "2000-2024"][i]}` })
  }
  a.library.recently_mirrored[2].identifier = longId
  return a
}

// A verify run: no download facts, so bytes and speed are null (activity.py).
function verifyRunning(): Answers {
  const a = defaultAnswers()
  a.activity.runs.push({
    ...clone(a.activity.runs[0]), activity_id: 920, run_id: null, collection: "phish", kind: "verify",
    phase: "verifying", current_items: [], bytes_downloaded: null, bytes_to_fetch: null,
    speed_bytes_per_second: null, eta_seconds: null,
  })
  return a
}

// Trim runs (§spec:run-error-retention), shaped as serve answers them: a trim
// running now, one that ended without room to compact, one that freed space, and
// a sync whose per-show error lines were dropped. A trim belongs to no collection.
// Purge runs (§spec:collection-removal), shaped as serve answers them: a purge of
// a removed collection running now, one that could not delete a file, and one that
// freed its archive's disk. A purge names its collection but has no phases or
// show counts.
function purges(): Answers {
  const a = defaultAnswers()
  const purge = (activity_id: number, collection: string, started: number, finished: number | null) => ({
    activity_id, run_id: null, source: "internetarchive", collection, kind: "purge" as const,
    started_by_serve: true, started_at: at(started), finished_at: finished === null ? null : at(finished),
    last_heartbeat_at: at(finished ?? 0.05), last_progress_at: at(started), current_items: [], phase: null,
    items_listed: null, items_to_look_up: null, items_looked_up: null, items_to_fetch: null,
    items_done: null, items_failed: null, bytes_downloaded: null, bytes_to_fetch: null,
    speed_bytes_per_second: null, eta_seconds: null, end_reason: null, runs_trimmed: null,
  })
  const ran = (activity_id: number, collection: string, started: number, finished: number | null,
    result: HistoryRun["result"], errors: string[] | null, bytes_freed: number | null): HistoryRun => ({
    run_id: null, activity_id, kind: "purge", source: "internetarchive", collection, started_at: at(started),
    finished_at: finished === null ? null : at(finished), duration_seconds: finished === null ? null : (started - finished) * 60,
    result, end_reason: null, items_discovered: null, items_new: null, items_updated: null, items_failed: null,
    bytes_downloaded: null, errors, failure_notice_error: null, detail_trimmed: false, runs_trimmed: null, bytes_freed,
  })
  // Only one run holds the writer lock: the purge runs alone.
  a.activity.runs = [
    { ...purge(941, "phish", 6, null), state: "running", result: null, bytes_freed: null },
    { ...purge(940, "moe", 12, 9), state: "ended", result: "completed_with_item_issues", bytes_freed: 0 },
  ]
  a.activity.network = []
  a["runs?days=30"].runs = a["runs?days=30"].runs.filter((r) => r.finished_at)
  a["runs?days=30"].runs.push(
    ran(941, "phish", 6, null, null, null, null),
    ran(940, "moe", 12, 9, "completed_with_item_issues",
      ["/data/library/internetarchive/moe/moe2001-07-14/notes.txt: could not delete: Permission denied"], 0),
    ran(939, "dmb", 3000, 2990, "completed", [], 912_400_000_000),
  )
  return a
}

function trims(): Answers {
  const a = defaultAnswers()
  const trim = (activity_id: number, started: number, finished: number | null) => ({
    activity_id, run_id: null, source: null, collection: null, kind: "trim" as const,
    started_by_serve: true, started_at: at(started), finished_at: finished === null ? null : at(finished),
    last_heartbeat_at: at(finished ?? 0.05), last_progress_at: at(started), current_items: [], phase: null,
    items_listed: null, items_to_look_up: null, items_looked_up: null, items_to_fetch: null,
    items_done: null, items_failed: null, bytes_downloaded: null, bytes_to_fetch: null,
    speed_bytes_per_second: null, eta_seconds: null, end_reason: null,
  })
  const ran = (activity_id: number, started: number, finished: number | null,
    result: HistoryRun["result"], errors: string[] | null, runs_trimmed: number | null, bytes_freed: number | null): HistoryRun => ({
    run_id: null, activity_id, kind: "trim", source: null, collection: null, started_at: at(started),
    finished_at: finished === null ? null : at(finished), duration_seconds: finished === null ? null : (started - finished) * 60,
    result, end_reason: null, items_discovered: null, items_new: null, items_updated: null, items_failed: null,
    bytes_downloaded: null, errors, failure_notice_error: null, detail_trimmed: false, runs_trimmed, bytes_freed,
  })
  // Only one run holds the writer lock: the trim runs alone.
  a.activity.runs = [
    { ...trim(931, 8, null), state: "running", result: null, runs_trimmed: null, bytes_freed: null },
    { ...trim(930, 9, 8.5), state: "ended", result: "failed_before_completion", runs_trimmed: 214, bytes_freed: 0 },
  ]
  a.activity.network = []
  a["runs?days=30"].runs = a["runs?days=30"].runs.filter((r) => r.finished_at)
  a["runs?days=30"].runs.push(
    ran(931, 8, null, null, null, null, null),
    ran(930, 9, 8.5, "failed_before_completion", ["compaction failed: database or disk is full"], 214, 0),
    ran(929, 3000, 2998, "completed", [], 1890, 412_000_000),
  )
  const synced = a["runs?days=30"].runs.find((r) => r.run_id === 485)!
  Object.assign(synced, { errors: [], detail_trimmed: true })
  return a
}

// A catch-up round with more collections than slots (§spec:sync-workflow "Waiting
// collections"): two catch-ups run, three wait their turn in the order they start.
// The round began when grateful-dead started; etree-lma took the slot freed since.
// Each waiting collection keeps its rescheduled entry until its catch-up ends
// (§spec:rescheduled-syncs Clearing), each skipped before the round began.
function waitingRound(): Answers {
  const a = defaultAnswers()
  for (const r of a.activity.runs) if (r.state === "running") r.kind = "catch_up"
  const roundStart = a.activity.runs[0].started_at
  const collections = ["phish", "dead-and-company", "ratdog"]
  a.activity.waiting = collections.map((collection, i) => ({
    activity_id: 914 + i, source: "internetarchive", collection, kind: "catch_up",
    started_by_serve: false, since: roundStart,
  }))
  const at_ = a.attention.entries.findIndex((e) => e.kind === "rescheduled")
  a.attention.entries.splice(at_, 1, ...collections.map((collection, i): AttentionEntry => ({
    kind: "rescheduled", source: "internetarchive", collection, since: at(1120 - i * 60),
    identifiers: [], count: 0, detail: {},
  })))
  return a
}

// A show that went before removal dates were recorded (status.py: removed_on None).
function removedUndated(): Answers {
  const a = defaultAnswers()
  a.attention.health[0].removed_upstream = [{ identifier: "gd1969-02-27.undated", removed_on: null }]
  a.attention.health[0].removed_upstream_count = 1
  return a
}

// Parallel collections: the later-started run finished first (history is sorted by start).
function quietFinishOrder(): Answers {
  const a = quiet()
  const [r] = a["runs?days=30"].runs
  a["runs?days=30"].runs = [
    { ...clone(r), activity_id: 1, started_at: at(60), finished_at: at(30), duration_seconds: 1800 },
    { ...clone(r), activity_id: 2, started_at: at(120), finished_at: at(5), duration_seconds: 6900 },
  ]
  return a
}

// The scheduler stopped 45 minutes ago and nothing has run since, so a tick that
// passed meanwhile is overdue (§spec:schedules). The server puts scheduler_stale
// first, then each collection's entries in kind order.
function schedulerStopped(): Answers {
  const a = quiet()
  a.attention.entries = [
    { kind: "scheduler_stale", source: null, collection: null, since: at(45),
      identifiers: [], count: 0, detail: {} },
    { kind: "tick_overdue", source: "internetarchive", collection: "grateful-dead", since: at(40),
      identifiers: [], count: 0, detail: {} },
  ]
  a.health.scheduler = { state: "stale", last_seen: at(45), release: "1.9.0" }
  // grateful-dead syncs daily at 21:00; the 21:00 tick passed with the scheduler down.
  a.config.document!.sources[0].collections[0].schedule = "0 21 * * *"
  a.config.next_syncs[0].next_at = "2026-11-13T21:00:00+00:00"
  return a
}

const PHISH_SYNC = "POST sync?source=internetarchive&collection=phish"
const VALIDATION_PROBLEMS = {
  ready: false,
  groups: [
    { source: null, collection: null, ok: true, checks: [] },
    { source: "internetarchive", collection: "phish", ok: false, checks: [] },
  ],
}

// Settings (§spec:dashboard-settings, §spec:edit-lock).
function settingsLocked(): Answers {
  const a = defaultAnswers()
  a.config.edit = "locked"
  return a
}

// Every way a collection's schedule and handoff read back: phish is manual only
// and hands off with a command that uses {staging_root} (so the library folder is
// file-only too); etree-lma runs on a custom cron.
function settingsVariety(): Answers {
  const a = defaultAnswers()
  const [ia, etree] = a.config.document!.sources
  ia.collections[1].schedule = null
  ia.collections[1].on_complete = { exec: "/scripts/beets-import.sh {staging_root}" }
  etree.collections[0].schedule = "0 2 1,15 * *"
  a.config.next_syncs = [
    a.config.next_syncs[0],
    { source: "etree-lma", collection: "etree-lma", next_at: "2026-11-15T02:00:00+00:00" },
  ]
  return a
}

// A file that parses but a run would refuse: errors against the settings they
// name, and one against a key the page has no field for.
function settingsInvalid(): Answers {
  const a = defaultAnswers()
  const doc = a.config.document!
  doc.keep_run_errors_days = 10
  doc.timezone = "Europe/Londn"
  doc.sources[0].collections[1].library_layout = "{year}/{band}"
  a.config.state = "invalid"
  a.config.errors = [
    { loc: ["keep_run_errors_days"], code: "greater_than_equal", message: "Input should be greater than or equal to 30" },
    { loc: ["timezone"], code: "timezone_unknown", message: "unknown timezone" },
    { loc: ["sources", 0, "collections", 1, "library_layout"], code: "layout_unknown_field", message: "unknown field 'band'" },
    { loc: ["max_concurrent_collectons"], code: "extra_forbidden", message: "Extra inputs are not permitted" },
  ]
  a.config.next_syncs = []
  return a
}

// A hand edit left a number where a schedule goes: the file keeps it as written
// (the server's answer for `schedule: 5`).
function settingsInvalidSchedule(): Answers {
  const a = defaultAnswers()
  a.config.document!.sources[0].collections[0].schedule = 5 as unknown as string
  a.config.state = "invalid"
  a.config.errors = [
    { loc: ["sources", 0, "collections", 0, "schedule"], code: "string_type", message: "Input should be a valid string" },
  ]
  a.config.next_syncs = []
  return a
}

// No config loaded (every other endpoint 503): the config resource says why.
// No file leads to setup; a file with errors opens in Settings; a file that isn't
// YAML is fixed by hand (§spec:first-run-setup, §spec:dashboard-settings).
const CONFIG_PATH = "/data/config/shakedown.yaml"
const NO_FILE = `config file not found: ${CONFIG_PATH}`
const NOT_YAML = `failed to parse YAML at ${CONFIG_PATH}: mapping values are not allowed here\n  in "${CONFIG_PATH}", line 14, column 12`
const INVALID = "config validation failed:\nkeep_run_errors_days: Input should be greater than or equal to 30\ntimezone: unknown timezone\nsources.0.collections.1.library_layout: unknown field 'band'\nmax_concurrent_collectons: Extra inputs are not permitted"

function noFile(): Answers {
  const a = defaultAnswers()
  Object.assign(a.config, { state: "missing", revision: null, document: null, errors: [], next_syncs: [], unconfigured: [] })
  a.setup = {
    generated_at: a.config.generated_at,
    archive_root: "/data/archive",
    library_root: "/data/library",
    state_db: "/data/archive/.shakedown/state.db",
    source_types: a.config.source_types,
    schedule: "0 3 * * 0",
  }
  return a
}

function notYaml(): Answers {
  const a = defaultAnswers()
  Object.assign(a.config, { state: "unreadable", document: null, next_syncs: [], unconfigured: [],
    errors: [{ loc: [], code: "yaml_invalid", message: "mapping values are not allowed here" }] })
  return a
}

const SETUP_STARTED = { revision: "sha256:1", warnings: [], started: { source: "etree", collection: "phish" } }
const PATHS_OK = { status: 200, body: { warnings: [] } }

const SAVED_WITH_WARNINGS = { revision: "sha256:2", warnings: [
  { loc: ["library_root"], code: "hardlink_failed" }, { loc: ["library_root"], code: "path_moved" },
] }

// The server lists runs newest first (§spec:run-history).
function sorted(a: Answers): Answers {
  a["runs?days=30"].runs.sort((x, y) => Date.parse(y.started_at) - Date.parse(x.started_at))
  return a
}

export const SCENARIOS: Record<string, Scenario> = {
  default: { now: NOW, mode: { kind: "ok" }, answers: sorted(defaultAnswers()) },
  quiet: { now: NOW, mode: { kind: "ok" }, answers: sorted(quiet()) },
  first_install: { now: NOW, mode: { kind: "ok" }, answers: sorted(firstInstall()) },
  config_broken: { now: NOW, mode: { kind: "ok" }, answers: sorted(configBroken()) },
  config_invalid: { now: NOW, mode: { kind: "no_config", detail: INVALID }, answers: sorted(settingsInvalid()) },
  config_unreadable: { now: NOW, mode: { kind: "no_config", detail: NOT_YAML }, answers: sorted(notYaml()) },
  // First-run setup: no file. The folder check passes and the first sync starts;
  // then the folder check warns and the sync can't start (another run).
  setup: { now: NOW, mode: { kind: "no_config", detail: NO_FILE }, answers: sorted(noFile()),
    writes: { "POST api/config/check-paths": PATHS_OK, "POST api/setup": { status: 201, body: SETUP_STARTED } } },
  setup_warnings: { now: NOW, mode: { kind: "no_config", detail: NO_FILE }, answers: sorted(noFile()),
    writes: { "POST api/config/check-paths": { status: 200, body: { warnings: [{ loc: ["library_root"], code: "hardlink_failed" }] } },
      "POST api/setup": { status: 201, body: { ...SETUP_STARTED, started: null, reason: "busy" } } } },
  unreachable: { now: NOW, mode: { kind: "goes_away", minutes: 4 }, answers: sorted(defaultAnswers()) },
  loading: { now: NOW, mode: { kind: "hangs" }, answers: sorted(defaultAnswers()) },
  never_reached: { now: NOW, mode: { kind: "refused" }, answers: sorted(defaultAnswers()) },
  long_content: { now: NOW, mode: { kind: "ok" }, answers: sorted(longContent()) },
  waiting: { now: NOW, mode: { kind: "ok" }, answers: sorted(waitingRound()) },
  scheduler_stopped: { now: NOW, mode: { kind: "ok" }, answers: sorted(schedulerStopped()) },
  // Run controls (review): Sync now answered at once, refused as busy; Validate.
  sync_started: { now: NOW, mode: { kind: "ok" }, answers: sorted(quiet()),
    writes: { [PHISH_SYNC]: { status: 202, body: { started: "sync", source: "internetarchive", collection: "phish" } } } },
  sync_busy: { now: NOW, mode: { kind: "ok" }, answers: sorted(defaultAnswers()),
    writes: { [PHISH_SYNC]: { status: 409, body: { detail: "a run is in progress; try again when it finishes" } },
      "POST validate": { status: 200, body: VALIDATION_PROBLEMS } } },
  // Settings: editable (the default answers), locked, and a file with errors; then
  // each answer a save, a delete, and the override can get.
  settings_locked: { now: NOW, mode: { kind: "ok" }, answers: sorted(settingsLocked()),
    writes: { "POST api/config/override": { status: 200, body: { edit: "override" } } } },
  settings_invalid: { now: NOW, mode: { kind: "ok" }, answers: sorted(settingsInvalid()) },
  settings_invalid_schedule: { now: NOW, mode: { kind: "ok" }, answers: sorted(settingsInvalidSchedule()) },
  settings_saved: { now: NOW, mode: { kind: "ok" }, answers: sorted(defaultAnswers()),
    writes: { "PUT api/config": { status: 200, body: SAVED_WITH_WARNINGS },
      "POST api/collections/purge": { status: 202, body: { started: "purge", source: "internetarchive", collection: "dmb", scope: "everything" } },
      "POST api/history/trim": { status: 202, body: { started: "trim" } } } },
  settings_refused: { now: NOW, mode: { kind: "ok" }, answers: sorted(defaultAnswers()),
    writes: { "PUT api/config": { status: 422, body: { errors: [
      { loc: ["keep_run_errors_days"], code: "greater_than_equal", message: "Input should be greater than or equal to 30" },
    ] } } } },
  settings_variety: { now: NOW, mode: { kind: "ok" }, answers: sorted(settingsVariety()) },
  // Saves that go through clean, then the delete after a remove starts or finds a run.
  settings_clean: { now: NOW, mode: { kind: "ok" }, answers: sorted(defaultAnswers()),
    writes: { "PUT api/config": { status: 200, body: { revision: "sha256:2", warnings: [] } },
      "POST api/collections/purge": { status: 202, body: { started: "purge", source: "internetarchive", collection: "phish", scope: "library" } } } },
  settings_remove_busy: { now: NOW, mode: { kind: "ok" }, answers: sorted(defaultAnswers()),
    writes: { "PUT api/config": { status: 200, body: { revision: "sha256:2", warnings: [] } },
      "POST api/collections/purge": { status: 409, body: { detail: "a run is in progress; try again when it finishes" } } } },
  settings_conflict: { now: NOW, mode: { kind: "ok" }, answers: sorted(defaultAnswers()),
    writes: { "PUT api/config": { status: 409, body: { code: "changed" } } } },
  settings_write_failed: { now: NOW, mode: { kind: "ok" }, answers: sorted(defaultAnswers()),
    writes: { "PUT api/config": { status: 500, body: { code: "write_failed", path: "/data/config/shakedown.yaml" } } } },
  // Behaviour scenarios (tests/behaviour.spec.ts, tests/freshness.spec.ts); no screenshots.
  // The browser's clock runs a minute ahead of the server's; the server answers normally.
  server_clock_behind: { now: at(-1), mode: { kind: "ok" }, answers: sorted(defaultAnswers()) },
  verify_running: { now: NOW, mode: { kind: "ok" }, answers: sorted(verifyRunning()) },
  trims: { now: NOW, mode: { kind: "ok" }, answers: sorted(trims()) },
  purges: { now: NOW, mode: { kind: "ok" }, answers: sorted(purges()) },
  restarts_broken: { now: NOW, mode: { kind: "restarts_broken", detail: CONFIG_ERROR, minutes: 1 }, answers: sorted(defaultAnswers()) },
  library_fails: { now: NOW, mode: { kind: "ok" }, answers: sorted(defaultAnswers()), fail: { library: 500 } },
  removed_undated: { now: NOW, mode: { kind: "ok" }, answers: sorted(removedUndated()) },
  quiet_finish_order: { now: NOW, mode: { kind: "ok" }, answers: sorted(quietFinishOrder()) },
}
