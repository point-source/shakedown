// Deterministic fixture answers in the exact shapes of src/api/types.ts.
// Clock: 2026-11-12 21:40:00 UTC.
import type {
  ActivityAnswer, AttentionAnswer, HealthAnswer, LibraryAnswer, NetworkAnswer, NetworkPoint, RunsAnswer,
} from "@/api/types"

export const NOW = "2026-11-12T21:40:00+00:00"
const at = (minutesAgo: number) =>
  new Date(Date.parse(NOW) - minutesAgo * 60_000).toISOString().replace(".000Z", "+00:00")

export const activity: ActivityAnswer = {
  generated_at: at(0),
  runs: [
    {
      activity_id: 912, run_id: 488, source: "internetarchive", collection: "grateful-dead",
      kind: "sync", started_by_serve: false, started_at: at(371), finished_at: null,
      last_heartbeat_at: at(0.05), last_progress_at: at(0.07), state: "running", result: null,
      end_reason: null, phase: "downloading", items_listed: 17842, items_to_look_up: 17842,
      items_looked_up: 17842, items_to_fetch: 3310, items_done: 1204, items_failed: 3,
      current_items: ["gd1977-05-08.sbd.hicks.4982.sbeok.shnf", "gd1972-08-27.sbd.orf.111.sbeok.shnf"],
      bytes_downloaded: 412_380_000_000, bytes_to_fetch: 1_131_900_000_000,
      speed_bytes_per_second: 38_200_000, eta_seconds: 18_840,
    },
    {
      activity_id: 913, run_id: 489, source: "etree-lma", collection: "etree-lma",
      kind: "sync", started_by_serve: false, started_at: at(95), finished_at: null,
      last_heartbeat_at: at(0.05), last_progress_at: at(41), state: "running", result: null,
      end_reason: null, phase: "discovering", items_listed: 52110, items_to_look_up: 52110,
      items_looked_up: 20431, items_to_fetch: null, items_done: 0, items_failed: 0,
      current_items: [], bytes_downloaded: 0, bytes_to_fetch: null,
      speed_bytes_per_second: 0, eta_seconds: null,
    },
    {
      activity_id: 910, run_id: 487, source: "internetarchive", collection: "phish",
      kind: "refetch", started_by_serve: true, started_at: at(14), finished_at: at(6),
      last_heartbeat_at: at(6), last_progress_at: at(7), state: "ended",
      result: "failed_before_completion", end_reason: "stopped_reporting", phase: "downloading",
      items_listed: null, items_to_look_up: null, items_looked_up: null, items_to_fetch: 1,
      items_done: 0, items_failed: 0, current_items: ["ph1997-11-22.flac16"],
      bytes_downloaded: 210_400_000, bytes_to_fetch: 655_000_000,
      speed_bytes_per_second: 0, eta_seconds: null,
    },
  ],
  network: [
    {
      source: "internetarchive", speed_bytes_per_second: 38_200_000, requests_in_flight: 4,
      last_event: {
        at: at(9), kind: "rate_limited", http_status: 429, collection: "grateful-dead",
        identifier: "gd1977-05-08.sbd.hicks.4982.sbeok.shnf", wait_seconds: 30, detail: null,
      },
    },
    {
      source: "etree-lma", speed_bytes_per_second: 0, requests_in_flight: 2,
      last_event: {
        at: at(41), kind: "timeout", http_status: null, collection: "etree-lma",
        identifier: "lma-ph2003-07-15", wait_seconds: null, detail: "read timed out after 30 s",
      },
    },
  ],
}

// 30 days of hourly buckets (bucket_seconds 3600 for days=30): a nightly sync per
// source, with a long Grateful Dead sync running over the last six hours.
function series(peak: number, phase: number, longRunHours: number): NetworkPoint[] {
  const points: NetworkPoint[] = []
  const end = Date.parse(NOW) - (Date.parse(NOW) % 3_600_000)
  for (let i = 719; i >= 0; i--) {
    const t = new Date(end - i * 3_600_000)
    const hour = t.getUTCHours()
    const day = Math.floor(i / 24)
    const nightly = hour >= 2 && hour < 4 ? 0.55 + 0.45 * Math.abs(Math.sin(day * 1.7 + phase)) : 0
    const longRun = i < longRunHours ? 0.8 + 0.2 * Math.abs(Math.sin(i * 0.9)) : 0
    const share = Math.max(nightly, longRun)
    const throttled = share > 0 && (day * 7 + hour + phase) % 11 === 0
    points.push({
      t: t.toISOString().replace(".000Z", "+00:00"),
      bytes: Math.round(share * peak * 3600),
      peak_requests_in_flight: share > 0 ? 4 : 0,
      rate_limited: throttled ? 3 : 0,
      server_errors: share > 0 && (day + hour) % 17 === 0 ? 1 : 0,
      timeouts: 0, connection_errors: 0,
      retries: throttled ? 3 : 0,
      backoff_seconds: throttled ? 90 : 0,
    })
  }
  return points
}

export const network: NetworkAnswer = {
  generated_at: at(0),
  bucket_seconds: 3600,
  sources: [
    {
      source: "internetarchive", series: series(41_000_000, 0, 6),
      events: [
        { at: at(9), kind: "rate_limited", http_status: 429, collection: "grateful-dead",
          identifier: "gd1977-05-08.sbd.hicks.4982.sbeok.shnf", wait_seconds: 30, detail: null },
        { at: at(9), kind: "backoff", http_status: null, collection: "grateful-dead",
          identifier: null, wait_seconds: 30, detail: null },
      ],
      events_total: 2,
    },
    {
      source: "etree-lma", series: series(9_500_000, 3, 0),
      events: [
        { at: at(41), kind: "timeout", http_status: null, collection: "etree-lma",
          identifier: "lma-ph2003-07-15", wait_seconds: null, detail: "read timed out after 30 s" },
      ],
      events_total: 1,
    },
  ],
}

export const runs: RunsAnswer = {
  generated_at: at(0),
  runs: [
    { run_id: 489, activity_id: 913, kind: "sync", source: "etree-lma", collection: "etree-lma",
      started_at: at(95), finished_at: null, duration_seconds: null, result: null, end_reason: null,
      items_discovered: 52110, items_new: 0, items_updated: 0, items_failed: 0, bytes_downloaded: 0,
      errors: [], failure_notice_error: null },
    { run_id: 488, activity_id: 912, kind: "sync", source: "internetarchive", collection: "grateful-dead",
      started_at: at(371), finished_at: null, duration_seconds: null, result: null, end_reason: null,
      items_discovered: 17842, items_new: 1201, items_updated: 0, items_failed: 3,
      bytes_downloaded: 412_380_000_000, errors: [], failure_notice_error: null },
    { run_id: 487, activity_id: 910, kind: "refetch", source: "internetarchive", collection: "phish",
      started_at: at(14), finished_at: at(6), duration_seconds: 480, result: "failed_before_completion",
      end_reason: "stopped_reporting", items_discovered: null, items_new: 0, items_updated: 0,
      items_failed: 0, bytes_downloaded: 210_400_000,
      errors: ["run ended unexpectedly (stopped reporting)"], failure_notice_error: null },
    { run_id: 486, activity_id: 905, kind: "sync", source: "internetarchive", collection: "phish",
      started_at: at(1170), finished_at: at(1121), duration_seconds: 2940, result: "completed",
      end_reason: null, items_discovered: 4113, items_new: 6, items_updated: 1, items_failed: 0,
      bytes_downloaded: 8_720_000_000, errors: [], failure_notice_error: null },
    { run_id: null, activity_id: 904, kind: "verify", source: "internetarchive", collection: "phish",
      started_at: at(1290), finished_at: at(1252), duration_seconds: 2280, result: "completed",
      end_reason: null, items_discovered: null, items_new: 0, items_updated: 0, items_failed: 0,
      bytes_downloaded: null, errors: [], failure_notice_error: null },
    { run_id: 485, activity_id: 901, kind: "sync", source: "etree-lma", collection: "etree-lma",
      started_at: at(2610), finished_at: at(2533), duration_seconds: 4620,
      result: "completed_with_item_issues", end_reason: null, items_discovered: 52098, items_new: 14,
      items_updated: 2, items_failed: 2, bytes_downloaded: 6_310_000_000,
      errors: ["lma-ph2003-07-15: checksum mismatch after 3 attempts"], failure_notice_error: null },
  ],
}

// One row per collection per day for 30 days; a day with no run repeats the day before.
function growth(): LibraryAnswer["growth"] {
  const rows: LibraryAnswer["growth"] = []
  const cols: [string, string, number, number, number, number][] = [
    ["internetarchive", "grateful-dead", 12_900, 14_318, 6_810e9, 7_512e9],
    ["internetarchive", "phish", 4_060, 4_097, 1_590e9, 1_604e9],
    ["etree-lma", "etree-lma", 12_700, 12_789, 718e9, 725e9],
  ]
  for (let i = 29; i >= 0; i--) {
    const date = new Date(Date.parse(NOW) - i * 86_400_000).toISOString().slice(0, 10)
    const f = (29 - i) / 29
    for (const [source, collection, c0, c1, b0, b1] of cols) {
      const step = Math.floor(f * 6) / 6 // runs on some days only
      rows.push({ date, source, collection, complete: Math.round(c0 + (c1 - c0) * step), archive_bytes: Math.round(b0 + (b1 - b0) * step) })
    }
  }
  return rows
}

const counts = (complete: number, pending: number, failed: number, unavailable: number, disappeared: number) =>
  ({ pending, downloading: 0, complete, failed, unavailable, disappeared, pruned: 0 })

export const library: LibraryAnswer = {
  generated_at: at(0),
  disk: { total_bytes: 16_000_000_000_000, free_bytes: 5_204_000_000_000, full_in_days: 84 },
  totals: {
    counts: { pending: 2106, downloading: 2, complete: 31204, failed: 5, unavailable: 211, disappeared: 9, pruned: 0 },
    restricted: 211, archive_bytes: 9_841_000_000_000, library_bytes: 7_102_000_000_000,
  },
  collections: [
    { source: "internetarchive", collection: "grateful-dead", counts: { ...counts(14318, 2106, 3, 188, 7), downloading: 2 },
      restricted: 188, archive_bytes: 7_512_000_000_000, library_bytes: 5_380_000_000_000,
      last_successful_sync_at: at(10_080) },
    { source: "internetarchive", collection: "phish", counts: counts(4097, 0, 0, 16, 2),
      restricted: 16, archive_bytes: 1_604_000_000_000, library_bytes: 1_211_000_000_000,
      last_successful_sync_at: at(1121) },
    { source: "etree-lma", collection: "etree-lma", counts: counts(12789, 0, 2, 7, 0),
      restricted: 7, archive_bytes: 725_000_000_000, library_bytes: 511_000_000_000,
      last_successful_sync_at: at(2533) },
  ],
  growth: growth(),
  recently_mirrored: [
    ["gd1977-05-07.sbd.miller.97060.flac16", "Grateful Dead Live at Boston Garden on 1977-05-07", 1],
    ["gd1977-05-05.sbd.hicks.4983.sbeok.shnf", "Grateful Dead Live at Veterans Memorial Coliseum on 1977-05-05", 3],
    ["gd1977-05-04.aud.vernon.82548.flac16", null, 6],
    ["gd1977-05-03.sbd.miller.30411.flac16", "Grateful Dead Live at The Palladium on 1977-05-03", 9],
    ["gd1977-05-01.sbd.unknown.1231.flac16", "Grateful Dead Live at The Palladium on 1977-05-01", 13],
  ].map(([identifier, title, m]) => ({
    source: "internetarchive", collection: "grateful-dead", identifier: identifier as string,
    title: title as string | null, downloaded_at: at(m as number),
  })),
}

export const attention: AttentionAnswer = {
  generated_at: at(0),
  entries: [
    { kind: "failed_shows", source: "internetarchive", collection: "grateful-dead", since: null,
      identifiers: ["gd1970-02-13.aud.unknown.1234.flac16", "gd1980-10-25.sbd.miller.7.flac16", "gd1991-06-14.mtx.seamons.flac16"],
      detail: {} },
    { kind: "ended_unexpectedly", source: "internetarchive", collection: "phish", since: at(6),
      identifiers: [], detail: { runs: [{ run_id: 487, activity_id: 910, kind: "refetch", finished_at: at(6) }] } },
    { kind: "rescheduled", source: "internetarchive", collection: "phish", since: at(90),
      identifiers: [], detail: {} },
    { kind: "handoff_pending", source: "etree-lma", collection: "etree-lma", since: at(2533),
      identifiers: ["lma-ph2003-07-12", "lma-ph2003-07-13"],
      detail: { shows: 2, last_reason: "connection refused (beets:8337)" } },
  ],
  health: [
    { source: "internetarchive", collection: "grateful-dead",
      restricted: [
        { identifier: "gd1977-05-08.sbd.betty.flac16", reason: "stream only" },
        { identifier: "gd1969-02-27.sbd.lemieux.flac24", reason: "stream only" },
        { identifier: "gd1973-06-10.aud.unknown.shnf", reason: "access restricted by the uploader" },
      ].concat(Array.from({ length: 185 }, (_, i) => ({ identifier: `gd19${70 + (i % 25)}-01-${String(1 + (i % 28)).padStart(2, "0")}.sbd.r${i}.flac16`, reason: "stream only" }))),
      removed_upstream: [
        { identifier: "gd1972-05-04.sbd.unknown.flac16", removed_on: "2026-11-02" },
        { identifier: "gd1985-06-14.aud.vernon.shnf", removed_on: "2026-10-28" },
      ],
      drift_files: 0, not_playable: [], playable_copies: [] },
    { source: "internetarchive", collection: "phish",
      restricted: Array.from({ length: 16 }, (_, i) => ({ identifier: `ph199${i % 10}-12-${String(10 + i).padStart(2, "0")}.sbd.flac16`, reason: "stream only" })),
      removed_upstream: [], drift_files: 3, not_playable: [],
      playable_copies: ["ph1989-05-28.shnf", "ph1990-04-05.shnf"] },
    { source: "etree-lma", collection: "etree-lma",
      restricted: Array.from({ length: 7 }, (_, i) => ({ identifier: `lma-gd19${80 + i}-03-0${1 + i}`, reason: "access restricted by the uploader" })),
      removed_upstream: [], drift_files: 0,
      not_playable: [{ identifier: "lma-ph1991-11-19", reason: "only SHN audio, which no player here reads" }],
      playable_copies: [] },
  ],
}

export const health: HealthAnswer = {
  generated_at: at(0),
  release: "1.9.0",
  config: { valid: true, error: null, last_loaded_at: at(0) },
  validation: [
    { source: "internetarchive", collection: "grateful-dead", status: "completed", at: at(20_160) },
    { source: "internetarchive", collection: "phish", status: "completed_with_item_issues", at: at(20_150) },
    { source: "etree-lma", collection: "etree-lma", status: null, at: null },
  ],
}
