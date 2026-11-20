// Every word the page shows for an endpoint code (§spec:dashboard "Codes, not
// copy"). Terms come from the glossary in §spec:ui-kit.
import type {
  AttentionEntry, NetworkEventKind, OperationStatus, Phase, Recovery, Result, RunKind,
} from "@/api/types"
import { clock, plural } from "@/lib/format"

export type Tone = "success" | "warning" | "danger"

export const KIND: Record<RunKind, string> = {
  sync: "Sync", catch_up: "Catch-up sync", refetch: "Refetch", reconform: "Reconform",
  restage: "Restage", verify: "Verify", reconcile: "Reconcile",
}
const kindLower = (k: RunKind) => KIND[k].toLowerCase()

export const PHASE: Record<Phase, string> = {
  starting: "Starting", listing: "Listing", discovering: "Discovering", downloading: "Downloading",
  tagging: "Tagging", staging: "Updating library", notifying: "Sending notices",
  verifying: "Verifying", finishing: "Finishing",
}

// Stopped is neutral: a run someone stopped is not a problem (operator decision,
// 2026-11-16).
export const RESULT: Record<Result, { label: string; tone: Tone | null }> = {
  completed: { label: "Completed", tone: "success" },
  completed_with_item_issues: { label: "Completed, some shows failed", tone: "warning" },
  failed_before_completion: { label: "Failed", tone: "danger" },
  stale_enumeration: { label: "Listing incomplete", tone: "warning" },
  stopped: { label: "Stopped", tone: null },
}

/** A run's outcome, with "Lost contact" taking precedence over its result. */
export function outcome(result: Result | null, endReason: string | null): { label: string; tone: Tone | null } | null {
  if (endReason === "stopped_reporting") return { label: "Lost contact", tone: "danger" }
  return result ? RESULT[result] : null
}

export const VALIDATION: Record<OperationStatus, { label: string; tone: Tone | null }> = {
  not_started: { label: "Not started", tone: null },
  in_progress: { label: "Did not finish", tone: "warning" },
  completed: { label: "Passed", tone: "success" },
  completed_with_item_issues: { label: "Passed with problems", tone: "warning" },
  failed_before_completion: { label: "Failed", tone: "danger" },
  stale_enumeration: { label: "Listing incomplete", tone: "warning" },
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
        return {
          title: `Lost contact with ${kindLower(r.kind)}${r.finished_at ? ` at ${clock(r.finished_at, now)}` : ""}`,
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
  }
}
