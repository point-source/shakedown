// Every word the page shows for an endpoint code (§spec:dashboard "Codes, not
// copy"). Terms come from the glossary in §spec:ui-kit.
import type {
  AttentionEntry, HealthAnswer, NetworkEventKind, OperationStatus, Phase, Recovery, Result, RunKind,
} from "@/api/types"
import { bytes, clock, plural } from "@/lib/format"

export type Tone = "success" | "warning" | "danger"

export const KIND: Record<RunKind, string> = {
  sync: "Sync", catch_up: "Catch-up sync", refetch: "Refetch", reconform: "Reconform",
  restage: "Restage", verify: "Verify", reconcile: "Reconcile",
  trim: "Trim history", purge: "Delete files",
}
const kindLower = (k: RunKind) => KIND[k].toLowerCase()

export const PHASE: Record<Phase, string> = {
  starting: "Starting", listing: "Listing", discovering: "Discovering", downloading: "Downloading",
  tagging: "Tagging", staging: "Updating library", notifying: "Sending notices",
  verifying: "Verifying", finishing: "Finishing",
}

// A collection whose run starts when a slot frees in the current sync (glossary
// "Waiting", §spec:ui-kit).
export const WAITING = "Waiting"

// Stopped is neutral: a run someone stopped is not a problem (operator decision,
// 2026-11-16).
export const RESULT: Record<Result, { label: string; tone: Tone | null }> = {
  completed: { label: "Completed", tone: "success" },
  completed_with_item_issues: { label: "Completed, some shows failed", tone: "warning" },
  failed_before_completion: { label: "Failed", tone: "danger" },
  stale_enumeration: { label: "Listing incomplete", tone: "warning" },
  stopped: { label: "Stopped", tone: null },
}

/** A run's outcome, with "Lost contact" taking precedence over its result. A
 *  delete that finished with some deletes failed names files, not shows. */
export function outcome(result: Result | null, endReason: string | null, kind?: RunKind): { label: string; tone: Tone | null } | null {
  if (endReason === "stopped_reporting") return { label: "Lost contact", tone: "danger" }
  if (kind === "purge" && result === "completed_with_item_issues") return { label: "Completed, some files not deleted", tone: "warning" }
  return result ? RESULT[result] : null
}

/** What a trim or a delete did, for its run rows; empty for other kinds. A delete
 *  of library folders frees about nothing (its files are links), so no
 *  "0 B freed" is shown. */
export function runEffects(r: { kind: RunKind; runs_trimmed: number | null; bytes_freed: number | null }): string[] {
  const out: string[] = []
  if (r.kind === "trim" && r.runs_trimmed !== null) out.push(plural(r.runs_trimmed, "run trimmed", "runs trimmed"))
  if ((r.kind === "trim" || r.kind === "purge") && (r.bytes_freed ?? 0) > 0) out.push(`${bytes(r.bytes_freed!)} freed`)
  return out
}

export const VALIDATION: Record<OperationStatus, { label: string; tone: Tone | null }> = {
  not_started: { label: "Not started", tone: null },
  in_progress: { label: "Did not finish", tone: "warning" },
  completed: { label: "Passed", tone: "success" },
  completed_with_item_issues: { label: "Passed with problems", tone: "warning" },
  failed_before_completion: { label: "Failed", tone: "danger" },
  stale_enumeration: { label: "Listing incomplete", tone: "warning" },
}

export const SCHEDULER: Record<HealthAnswer["scheduler"]["state"], { label: string; tone: Tone | null }> = {
  running: { label: "Working", tone: "success" },
  stale: { label: "Not responding", tone: "danger" },
  no_schedule: { label: "Nothing scheduled", tone: null },
  never: { label: "Never started", tone: "warning" },
}

export const EVENT: Record<NetworkEventKind, string> = {
  rate_limited: "Rate limited", server_error: "Server error", timeout: "Timed out",
  connection_error: "Connection failed", refused: "Refused", retry: "Retry", backoff: "Back-off",
}

const OPERATION: Record<string, string> = {
  sync: "Sync", restage: "Restage", reconcile: "Reconcile", validate: "Validation",
  refetch: "Refetch", reconform: "Reconform", verify: "Verify",
}
const RECOVERY_STATUS: Partial<Record<OperationStatus, string>> = {
  in_progress: "did not finish",
  completed_with_item_issues: "completed, some shows failed",
  failed_before_completion: "failed",
  stale_enumeration: "could not list the source",
}

export interface AttentionWords {
  title: string
  /** What happens next, or what the operator can do; null when the server supplies it. */
  next: string | null
}

export function attentionWords(e: AttentionEntry, now: string): AttentionWords {
  const d = e.detail as Record<string, unknown>
  switch (e.kind) {
    case "failed_shows":
      return { title: `${plural(e.count, "show", "shows")} failed`, next: "The next sync retries them." }
    case "recovery": {
      const r = d as unknown as Recovery
      const op = OPERATION[r.operation] ?? r.operation
      return { title: `${op} ${RECOVERY_STATUS[r.status] ?? r.status}`, next: null }
    }
    case "ended_unexpectedly": {
      const runs = (d.runs ?? []) as { kind: RunKind; finished_at: string | null }[]
      if (runs.length === 1) {
        const r = runs[0]
        const at = r.finished_at ? ` at ${clock(r.finished_at, now)}` : ""
        if (r.kind === "trim") return { title: `Lost contact with trim history${at}`, next: "Trim history again from Settings." }
        if (r.kind === "purge") return { title: `Lost contact while deleting files${at}`, next: "Delete the files again from Settings." }
        return {
          title: `Lost contact with ${kindLower(r.kind)}${at}`,
          next: ["restage", "verify", "reconcile"].includes(r.kind)
            ? `Run ${kindLower(r.kind)} again.`
            : `The next ${kindLower(r.kind === "catch_up" ? "sync" : r.kind)} picks up where it left off.`,
        }
      }
      return { title: `Lost contact with ${runs.length} runs`, next: "Each picks up where it left off on its next run." }
    }
    case "stale":
      return { title: "Listing incomplete", next: "Nothing was removed. The next sync lists the source again." }
    case "rescheduled":
      return { title: `Sync skipped at ${clock(e.since!, now)}`, next: "It runs as a catch-up sync." }
    case "handoff_pending":
      return {
        title: `Handoff notice not sent for ${plural(Number(d.shows), "show", "shows")}`,
        next: "The next run sends it again.",
      }
    case "failure_notice_undelivered":
      return { title: "Failure notice not sent", next: "Check the failure notice settings in shakedown.yaml." }
    case "separations":
      return {
        title: e.count === 1 ? "1 show has its own folder" : `${plural(e.count, "show", "shows")} have their own folders`,
        next: "Their folders would clash, so each name carries its show ID.",
      }
    case "unplaced_files": {
      return {
        title: `${plural(e.count, "file", "files")} in the library Shakedown didn't place`,
        next: "Delete them if you don't need them. Shakedown leaves them alone.",
      }
    }
    case "unidentified_files":
      return {
        title: `${plural(e.count, "file", "files")} Shakedown can't identify`,
        next: "Shakedown keeps them as they are and does nothing more with them.",
      }
    case "scheduler_stale":
      return { title: "Scheduled syncs are not starting", next: "Start the shakedown-scheduler container." }
    case "tick_overdue":
      return { title: "Scheduled sync did not start", next: "It runs at its next scheduled time." }
  }
}

// ---- Settings (§spec:dashboard-settings, §spec:edit-lock, §spec:dashboard-credentials) ----

/** A config error's words, against the setting its `loc` names. `message` is
 *  developer text and never shown; the code is enough. */
export function problemWords(code: string, loc: (string | number)[]): string {
  const key = loc[loc.length - 1]
  switch (code) {
    case "greater_than_equal": return key === "keep_run_errors_days" ? "Enter 30 or more." : "Enter a bigger number."
    case "greater_than": return "Enter a bigger number."
    case "less_than_equal": case "less_than": return "Enter a smaller number."
    case "int_parsing": case "int_type": case "int_from_float": return "Enter a whole number."
    case "missing": return "This setting is needed."
    case "string_type": case "bool_type": case "bool_parsing": return "This value isn't the right kind for this setting."
    case "extra_forbidden": return "Shakedown doesn't know this setting. Remove it from shakedown.yaml."
    case "schedule_invalid": return "Shakedown can't read this schedule. Use five fields: minute, hour, day of month, month, day of week."
    case "timezone_unknown": return "Not a time zone Shakedown knows. Use a name like Europe/London."
    case "duplicate_name": return "Another collection in this source has this name."
    case "source_type_unknown": return "Shakedown has no source of this type."
    case "layout_unknown_field": return "The layout uses a field this source doesn't have."
    case "layout_unknown_filter": return "The layout uses a filter this source doesn't have."
    case "layout_no_field": return "The layout needs at least one field, like {identifier}."
    case "exec_file_only": return key === "library_root"
      ? "Change this in shakedown.yaml: a collection's command uses it."
      : "Commands are set in shakedown.yaml only."
    default: return "Shakedown can't use this value."
  }
}

/** A path check or layout warning from a save (§spec:dashboard-settings). */
export const WARNING: Record<string, string> = {
  not_writable: "Shakedown can't write to this folder. Runs won't start until it can.",
  hardlink_failed: "Library files can't link to the archive here. Put both folders on the same drive.",
  path_moved: "Shakedown moves nothing: the next sync downloads again what the old location held.",
  layout_not_unique: "Two shows could land in one folder. Add {identifier} to keep them apart.",
}

export const CREDENTIALS: Record<"environment" | "dashboard" | "none", string> = {
  environment: "From environment variables",
  dashboard: "Saved in the dashboard",
  none: "No login",
}
