// Review and screenshot scenarios: answers keyed by endpoint path, in the exact
// shapes of src/api/types.ts, with a fixed clock. Served to the real page by
// intercepting its requests (scripts/render.mjs, tests/screens.spec.ts).
import type {
  ActivityAnswer, AttentionAnswer, AttentionEntry, HealthAnswer, HistoryRun, LibraryAnswer, NetworkAnswer, RunsAnswer,
} from "@/api/types"
import * as base from "./default.ts"

export type Answers = {
  activity: ActivityAnswer
  "network?days=30": NetworkAnswer
  "runs?days=30": RunsAnswer
  library: LibraryAnswer
  attention: AttentionAnswer
  health: HealthAnswer
}

export interface Scenario {
  now: string
  /** How the server behaves: answer normally, answer 503 with the config error,
   *  answer first and then stop (advance the clock by `minutes`), or never answer. */
  mode: { kind: "ok" } | { kind: "no_config"; detail: string } | { kind: "goes_away"; minutes: number } | { kind: "hangs" }
  answers: Answers
}

const NOW = base.NOW
const at = (minutesAgo: number) =>
  new Date(Date.parse(NOW) - minutesAgo * 60_000).toISOString().replace(".000Z", "+00:00")
const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v))

const defaultAnswers = (): Answers => clone({
  activity: base.activity,
  "network?days=30": base.network,
  "runs?days=30": base.runs,
  library: base.library,
  attention: base.attention,
  health: base.health,
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
    disk: { total_bytes: 16_000_000_000_000, free_bytes: 15_870_000_000_000, full_in_days: null },
    totals: { counts: zero, restricted: 0, archive_bytes: 0, library_bytes: 0 },
    collections: a.library.collections.map((c) => ({ ...c, counts: zero, restricted: 0, archive_bytes: 0, library_bytes: 0, last_successful_sync_at: null })),
    growth: a.library.collections.map((c) => ({ date: NOW.slice(0, 10), source: c.source, collection: c.collection, complete: 0, archive_bytes: 0 })),
    recently_mirrored: [],
  }
  a.attention.entries = []
  a.attention.health = a.attention.health.map((h) => ({ ...h, restricted: [], removed_upstream: [], drift_files: 0, not_playable: [], playable_copies: [] }))
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
    { kind: "failed_shows", source: "internetarchive", collection: long, since: null, identifiers: failed,
      detail: { recovery: { operation: "sync", status: "completed_with_item_issues", started_at: at(800), updated_at: at(700), finished_at: at(700),
        phase: "downloading", affected_item: null, affected_path: null, completed_work: null, preservation_context: null, deletion_context: null,
        safe_next_action: "rerun sync; failed items will be retried", errors: null } } },
    { kind: "recovery", source: "internetarchive", collection: "phish", since: at(300), identifiers: ["ph1997-11-22.flac16"],
      detail: { operation: "restage", status: "failed_before_completion", started_at: at(320), updated_at: at(300), finished_at: at(300),
        phase: "staging", affected_item: "ph1997-11-22.flac16", affected_path: "/data/library/Phish/1997-11-22 - Hampton Coliseum",
        completed_work: null, preservation_context: null, deletion_context: null, safe_next_action: "fix storage/path/layout and rerun restage", errors: null } },
    { kind: "stale", source: "etree-lma", collection: "etree-lma", since: null, identifiers: [], detail: {} },
    { kind: "rescheduled", source: "internetarchive", collection: "phish", since: at(90), identifiers: [], detail: {} },
    { kind: "handoff_pending", source: "etree-lma", collection: "etree-lma", since: at(2533), identifiers: Array.from({ length: 41 }, (_, i) => `lma-ph2003-07-${String(1 + (i % 30)).padStart(2, "0")}-${i}`),
      detail: { shows: 41, last_reason: "HTTPConnectionPool(host='beets', port=8337): Max retries exceeded with url: /api/import (Caused by NewConnectionError: Failed to establish a new connection: [Errno 111] Connection refused)" } },
    { kind: "failure_notice_undelivered", source: "internetarchive", collection: "phish", since: at(1121), identifiers: [],
      detail: { failure_notice_error: "apprise: no service accepted the notification (ntfy://ntfy.lan/shakedown: 403 Forbidden)", run_id: 486 } },
    { kind: "separations", source: "internetarchive", collection: long, since: null, identifiers: ["gd1977-05-08.sbd.hicks.4982.sbeok.shnf", "gd1977-05-08.aud.vernon.flac16"],
      detail: { separations: [] } },
    { kind: "unplaced_files", source: "internetarchive", collection: "phish", since: null, identifiers: [],
      detail: { unplaced_files: ["/data/library/Phish/1997-11-22 - Hampton Coliseum/cover.jpg", "/data/library/Phish/.DS_Store", "/data/library/Phish/1995-12-31 - Madison Square Garden/notes.txt", "/data/library/Phish/1993-08-13/folder.jpg"] } },
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
  }))
  a["runs?days=30"].runs.push(...extra)
  a.library.collections[0].collection = long
  for (let i = 0; i < 3; i++) {
    a.library.collections.push({ ...clone(a.library.collections[1]), source: "phishin", collection: `phish-in-${["1983-1989", "1990-1999", "2000-2024"][i]}` })
  }
  a.library.recently_mirrored[2].identifier = longId
  return a
}

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
  no_config: { now: NOW, mode: { kind: "no_config", detail: CONFIG_ERROR }, answers: sorted(defaultAnswers()) },
  unreachable: { now: NOW, mode: { kind: "goes_away", minutes: 4 }, answers: sorted(defaultAnswers()) },
  loading: { now: NOW, mode: { kind: "hangs" }, answers: sorted(defaultAnswers()) },
  long_content: { now: NOW, mode: { kind: "ok" }, answers: sorted(longContent()) },
}
