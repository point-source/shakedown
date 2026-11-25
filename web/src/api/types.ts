// Response shapes of the dashboard endpoints, as recorded in SPEC.md
// (§spec:run-activity, §spec:network-activity, §spec:run-history,
// §spec:library-stats, §spec:needs-attention, §spec:dashboard-health).
// Times are ISO 8601 strings with a UTC offset.

export type Iso = string

export type RunKind =
  | "sync" | "catch_up" | "refetch" | "reconform" | "restage" | "verify" | "reconcile" | "trim"
  | "purge"
export type Phase =
  | "starting" | "listing" | "discovering" | "downloading" | "tagging" | "staging"
  | "notifying" | "verifying" | "finishing"
export type Result =
  | "completed" | "completed_with_item_issues" | "failed_before_completion" | "stale_enumeration"
  | "stopped"
export type EndReason = "stopped_reporting"

export interface ActivityRun {
  activity_id: number
  run_id: number | null
  /** Null for a trim, which belongs to no collection. */
  source: string | null
  collection: string | null
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
  /** Null for runs that download nothing (verify, restage, reconcile). */
  bytes_downloaded: number | null
  bytes_to_fetch: number | null
  speed_bytes_per_second: number | null
  eta_seconds: number | null
  /** A trim's runs trimmed and bytes the database shrank by, or the disk a purge
   * gave back; null for other kinds. */
  runs_trimmed: number | null
  bytes_freed: number | null
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

/** A collection waiting for a slot in a multi-collection sync: not a run yet. */
export interface WaitingCollection {
  activity_id: number
  source: string
  collection: string
  kind: "sync" | "catch_up"
  started_by_serve: boolean
  since: Iso
}

export interface ActivityAnswer {
  generated_at: Iso
  runs: ActivityRun[]
  /** In the order they start. */
  waiting: WaitingCollection[]
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
  /** Null for a trim, which belongs to no collection. */
  source: string | null
  collection: string | null
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
  /** The run's per-show error lines were dropped (§spec:run-error-retention). */
  detail_trimmed: boolean
  runs_trimmed: number | null
  bytes_freed: number | null
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
  disk: { total_bytes: number; free_bytes: number }
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
  | "unidentified_files" | "scheduler_stale" | "tick_overdue"

/** An audio file whose content matches no recognised format, by its source name. */
export interface UnidentifiedFile {
  identifier: string
  file: string
}

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
  /** Null for an entry about no collection (scheduler_stale). */
  source: string | null
  collection: string | null
  since: Iso | null
  /** The first ten affected shows; `count` says how many there are. */
  identifiers: string[]
  /** How many shows the entry affects (files, for unplaced_files and unidentified_files). */
  count: number
  detail: Record<string, unknown>
}

export interface AttentionAnswer {
  generated_at: Iso
  entries: AttentionEntry[]
  health: {
    source: string
    collection: string
    // Each list holds its first ten shows; its *_count says how many there are.
    restricted: { identifier: string; reason: string }[]
    restricted_count: number
    /** removed_on is null for shows that went before dates were recorded. */
    removed_upstream: { identifier: string; removed_on: string | null }[]
    removed_upstream_count: number
    drift_files: number
    not_playable: { identifier: string; reason: string }[]
    not_playable_count: number
    playable_copies: string[]
    playable_copies_count: number
  }[]
}

export interface HealthAnswer {
  generated_at: Iso
  release: string
  config: { valid: boolean; error: string | null; last_loaded_at: Iso | null }
  validation: { source: string; collection: string; status: OperationStatus | null; at: Iso | null }[]
  /** Whether the scheduler is running (§spec:schedules). Not drawn yet: Design's. */
  scheduler: { state: "running" | "stale" | "no_schedule" | "never"; last_seen: Iso | null; release: string | null }
}

// ---- Config management (§spec:dashboard-settings and the sections it names) ----

export type ConfigState = "missing" | "unreadable" | "invalid" | "valid"
export type EditState = "editable" | "locked" | "override"
/** Keys and list indexes to the setting a problem names, e.g. ["sources", 0, "collections", 1, "query"]. */
export type Loc = (string | number)[]

export interface ConfigProblem { loc: Loc; code: string; message: string }
export interface ConfigWarning { loc: Loc; code: string }

export interface CollectionConfig {
  name: string
  query: string
  library_layout: string
  prune_disappeared: boolean
  incremental_discovery: boolean
  preserve_source_metadata: boolean
  fill_tags: boolean
  on_complete: { webhook: string } | { exec: string } | null
  /** Five-field cron; null for manual only (§spec:schedules). */
  schedule: string | null
}

export interface SourceConfig {
  name: string
  type: string
  collections: CollectionConfig[]
  max_concurrent_requests: number | null
}

export interface ConfigDocument {
  archive_root: string
  library_root: string
  state_db: string | null
  max_concurrent_downloads: number
  max_concurrent_collections: number
  keep_run_errors_days: number
  sources: SourceConfig[]
  notifications: { on_failure: { webhook: string | null } | null } | null
  timezone: string | null
}

export interface SourceType {
  type: string
  layout_fields: string[]
  layout_filters: string[]
  /** The plugin reads pasted links as searches (`GET /api/source-link`). */
  reads_links: boolean
}

/** `GET /api/source-link?type=&url=`: the search a pasted link stands for, or
 *  null when the type's plugin can't read it (§spec:dashboard-settings). */
export interface SourceLinkAnswer { query: string | null }

export interface UnconfiguredCollection {
  source: string
  collection: string
  shows: number
  archive_bytes: number
  library_bytes: number
}

export interface ConfigAnswer {
  generated_at: Iso
  state: ConfigState
  path: string
  revision: string | null
  edit: EditState
  document: ConfigDocument | null
  errors: ConfigProblem[]
  next_syncs: { source: string; collection: string; next_at: Iso | null }[]
  source_types: SourceType[]
  unconfigured: UnconfiguredCollection[]
}

/** `POST /validate` (§spec:setup-readiness-validation). */
export interface ValidationReport {
  ready: boolean
  groups: {
    source: string | null
    collection: string | null
    ok: boolean
    checks: { name: string; ok: boolean; target: string | null; consequence: string | null; action: string | null }[]
  }[]
}
