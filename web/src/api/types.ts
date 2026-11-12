// Response shapes of the dashboard endpoints, as recorded in SPEC.md
// (§spec:run-activity, §spec:network-activity, §spec:run-history,
// §spec:library-stats, §spec:needs-attention, §spec:dashboard-health).
// Times are ISO 8601 strings with a UTC offset.

export type Iso = string

export type RunKind =
  | "sync" | "catch_up" | "refetch" | "reconform" | "restage" | "verify" | "reconcile"
export type Phase =
  | "starting" | "listing" | "discovering" | "downloading" | "tagging" | "staging"
  | "notifying" | "verifying" | "finishing"
export type Result =
  | "completed" | "completed_with_item_issues" | "failed_before_completion" | "stale_enumeration"
export type EndReason = "stopped_reporting"

export interface ActivityRun {
  activity_id: number
  run_id: number | null
  source: string
  collection: string
  kind: RunKind
  started_by_serve: boolean
  started_at: Iso
  finished_at: Iso | null
  last_heartbeat_at: Iso
  last_progress_at: Iso
  state: "running" | "ended"
  result: Result | null
  end_reason: EndReason | null
  phase: Phase | null
  items_listed: number | null
  items_to_look_up: number | null
  items_looked_up: number | null
  items_to_fetch: number | null
  items_done: number | null
  items_failed: number | null
  current_items: string[]
  bytes_downloaded: number
  bytes_to_fetch: number | null
  speed_bytes_per_second: number
  eta_seconds: number | null
}

export type NetworkEventKind =
  | "rate_limited" | "server_error" | "timeout" | "connection_error" | "refused" | "retry" | "backoff"

export interface NetworkEvent {
  at: Iso
  kind: NetworkEventKind
  http_status: number | null
  collection: string | null
  identifier: string | null
  wait_seconds: number | null
  detail: string | null
}

export interface NetworkNow {
  source: string
  speed_bytes_per_second: number
  requests_in_flight: number
  last_event: NetworkEvent | null
}

export interface ActivityAnswer {
  generated_at: Iso
  runs: ActivityRun[]
  network: NetworkNow[]
}

export interface NetworkPoint {
  t: Iso
  bytes: number
  peak_requests_in_flight: number
  rate_limited: number
  server_errors: number
  timeouts: number
  connection_errors: number
  retries: number
  backoff_seconds: number
}

export interface NetworkAnswer {
  generated_at: Iso
  bucket_seconds: number
  sources: { source: string; series: NetworkPoint[]; events: NetworkEvent[]; events_total: number }[]
}

export interface HistoryRun {
  run_id: number | null
  activity_id: number | null
  kind: RunKind
  source: string
  collection: string
  started_at: Iso
  finished_at: Iso | null
  duration_seconds: number | null
  result: Result | null
  end_reason: EndReason | null
  items_discovered: number | null
  items_new: number | null
  items_updated: number | null
  items_failed: number | null
  bytes_downloaded: number | null
  errors: string[] | null
  failure_notice_error: string | null
}

export interface RunsAnswer {
  generated_at: Iso
  runs: HistoryRun[]
}

export type Counts = Record<
  "pending" | "downloading" | "complete" | "failed" | "unavailable" | "disappeared" | "pruned",
  number
>

export interface LibraryAnswer {
  generated_at: Iso
  disk: { total_bytes: number; free_bytes: number; full_in_days: number | null }
  totals: { counts: Counts; restricted: number; archive_bytes: number; library_bytes: number }
  collections: {
    source: string
    collection: string
    counts: Counts
    restricted: number
    archive_bytes: number
    library_bytes: number
    last_successful_sync_at: Iso | null
  }[]
  growth: { date: string; source: string; collection: string; complete: number; archive_bytes: number }[]
  recently_mirrored: {
    source: string
    collection: string
    identifier: string
    title: string | null
    downloaded_at: Iso
  }[]
}

export type AttentionKind =
  | "failed_shows" | "recovery" | "ended_unexpectedly" | "stale" | "rescheduled"
  | "handoff_pending" | "failure_notice_undelivered" | "separations" | "unplaced_files"

export type OperationStatus =
  | "not_started" | "in_progress" | "completed" | "completed_with_item_issues"
  | "failed_before_completion" | "stale_enumeration"

// `status`'s recovery summary; rides on an entry as detail.recovery, or is the
// detail of a `recovery` entry (§spec:needs-attention).
export interface Recovery {
  operation: string
  status: OperationStatus
  started_at: Iso
  updated_at: Iso
  finished_at: Iso | null
  phase: string | null
  affected_item: string | null
  affected_path: string | null
  completed_work: unknown
  preservation_context: unknown
  deletion_context: unknown
  safe_next_action: string | null
  errors: string[] | null
}

export interface AttentionEntry {
  kind: AttentionKind
  source: string
  collection: string
  since: Iso | null
  identifiers: string[]
  detail: Record<string, unknown>
}

export interface AttentionAnswer {
  generated_at: Iso
  entries: AttentionEntry[]
  health: {
    source: string
    collection: string
    restricted: { identifier: string; reason: string }[]
    removed_upstream: { identifier: string; removed_on: string }[]
    drift_files: number
    not_playable: { identifier: string; reason: string }[]
    playable_copies: string[]
  }[]
}

export interface HealthAnswer {
  generated_at: Iso
  release: string
  config: { valid: boolean; error: string | null; last_loaded_at: Iso | null }
  validation: { source: string; collection: string; status: OperationStatus | null; at: Iso | null }[]
}
