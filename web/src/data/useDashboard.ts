// Polls the dashboard endpoints (§spec:dashboard): the live view every few
// seconds, the others about every 30 seconds, nothing while the page is hidden.
// Every request is a GET to a relative URL, so the page works under a proxy prefix.
import { useEffect, useRef, useState } from "react"
import type {
  ActivityAnswer, AttentionAnswer, HealthAnswer, LibraryAnswer, NetworkAnswer, RunsAnswer,
} from "@/api/types"

export const LIVE_MS = 5_000
export const VIEW_MS = 30_000

export interface Slot<T> {
  data: T | null
  /** Text of the last failure, or null when the last request succeeded. */
  error: string | null
  /** True when the last failure was a 503: the config has never loaded. */
  noConfig: boolean
  /** When `data` arrived, by the browser's clock; null before the first answer. */
  receivedAt: number | null
}

function useEndpoint<T extends { generated_at: string }>(path: string, intervalMs: number): Slot<T> {
  const [slot, setSlot] = useState<Slot<T>>({ data: null, error: null, noConfig: false, receivedAt: null })
  const timer = useRef<number | undefined>(undefined)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const r = await fetch(`./api/${path}`, { cache: "no-store" })
        if (!r.ok) {
          const body = await r.json().catch(() => null)
          const detail = typeof body?.detail === "string" ? body.detail : `HTTP ${r.status}`
          if (!cancelled) setSlot((s) => ({ ...s, error: detail, noConfig: r.status === 503 }))
        } else {
          const data = (await r.json()) as T
          if (!cancelled) setSlot({ data, error: null, noConfig: false, receivedAt: Date.now() })
        }
      } catch (e) {
        if (!cancelled) setSlot((s) => ({ ...s, error: String(e), noConfig: false }))
      }
    }
    const schedule = () => {
      window.clearInterval(timer.current)
      if (document.hidden) return
      timer.current = window.setInterval(load, intervalMs)
    }
    const onVisibility = () => {
      if (!document.hidden) load()
      schedule()
    }
    load()
    schedule()
    document.addEventListener("visibilitychange", onVisibility)
    return () => {
      cancelled = true
      window.clearInterval(timer.current)
      document.removeEventListener("visibilitychange", onVisibility)
    }
  }, [path, intervalMs])

  return slot
}

/** A clock that ticks so "x min ago" and staleness stay current between answers. */
export function useNow(stepMs = 5_000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), stepMs)
    return () => window.clearInterval(t)
  }, [stepMs])
  return now
}

export function useDashboard() {
  const activity = useEndpoint<ActivityAnswer>("activity", LIVE_MS)
  const network = useEndpoint<NetworkAnswer>("network?days=30", VIEW_MS)
  const runs = useEndpoint<RunsAnswer>("runs?days=30", VIEW_MS)
  const library = useEndpoint<LibraryAnswer>("library", VIEW_MS)
  const attention = useEndpoint<AttentionAnswer>("attention", VIEW_MS)
  const health = useEndpoint<HealthAnswer>("health", VIEW_MS)
  return { activity, network, runs, library, attention, health }
}

export type Dashboard = ReturnType<typeof useDashboard>

/**
 * Old data is labelled, never passed off as current: the page cannot reach
 * Shakedown when a refresh failed or an answer arrived more than twice its
 * interval ago. Ages count from arrival, by the browser's clock, so a server
 * clock that differs from the browser's never reads as unreachable.
 * Returns the arrival time of the oldest answer on screen when that is the case.
 */
export function staleSince(d: Dashboard, now: number): number | null {
  const slots: [Slot<unknown>, number][] = [
    [d.activity, LIVE_MS], [d.network, VIEW_MS], [d.runs, VIEW_MS],
    [d.library, VIEW_MS], [d.attention, VIEW_MS], [d.health, VIEW_MS],
  ]
  let stale = false
  let oldest: number | null = null
  for (const [slot, interval] of slots) {
    if (slot.noConfig) continue
    const at = slot.receivedAt
    if (at !== null) oldest = oldest === null ? at : Math.min(oldest, at)
    if ((slot.error && slot.data) || (at !== null && now - at > 2 * interval)) stale = true
  }
  return stale ? oldest : null
}
