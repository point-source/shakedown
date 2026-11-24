// Overview's "Shakedown" card: config, the scheduler, and per collection its next
// sync, last validation, and Sync now; Validate in the header
// (§spec:dashboard-health, §spec:schedules, §spec:dashboard-run-controls).
import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { ConfigAnswer, HealthAnswer, ValidationReport } from "@/api/types"
import { RowsSkeleton, ToneBadge } from "@/dash/bits"
import { refusal, send, useNote } from "@/dash/actions"
import { clock } from "@/lib/format"
import { SCHEDULER, VALIDATION } from "@/lib/words"

function SyncNow({ source, collection }: { source: string; collection: string }) {
  const note = useNote()
  const [busy, setBusy] = useState(false)
  const start = async () => {
    setBusy(true)
    const a = await send<{ code?: string; detail?: string }>("POST", `sync?source=${encodeURIComponent(source)}&collection=${encodeURIComponent(collection)}`)
    setBusy(false)
    note(a.status === 202 ? { tone: "success", title: "Sync started", text: collection } : refusal(a))
  }
  return (
    <Button size="sm" variant="outline" onClick={start} disabled={busy}>
      {busy ? "Starting…" : "Sync now"}
    </Button>
  )
}

function Validate() {
  const note = useNote()
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    const a = await send<ValidationReport & { code?: string; detail?: string }>("POST", "validate")
    setBusy(false)
    if (a.status !== 200 || !a.body) return note(refusal(a))
    note(a.body.ready
      ? { tone: "success", title: "Validation passed" }
      : { tone: "warning", title: "Validation found problems", text: "Each collection's result is under Last validation." })
  }
  return (
    <Button size="sm" variant="outline" onClick={run} disabled={busy}>
      {busy ? "Validating…" : "Validate"}
    </Button>
  )
}

export function ShakedownCard({ health, config, now }: { health: HealthAnswer | null; config: ConfigAnswer | null; now: string }) {
  const next = (source: string, collection: string) =>
    config?.next_syncs.find((n) => n.source === source && n.collection === collection) ?? null
  const sched = health ? SCHEDULER[health.scheduler.state] : null
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>Shakedown</CardTitle>
        {health && <CardAction><Validate /></CardAction>}
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {health === null ? <RowsSkeleton rows={2} /> : (
          <>
            <div className="grid grid-cols-[auto_1fr] items-center gap-x-6 gap-y-2">
              <span className="text-muted-foreground">Config</span>
              <span>
                {health.config.valid
                  ? <ToneBadge tone="success">Valid</ToneBadge>
                  : <ToneBadge tone="danger">Has an error</ToneBadge>}
              </span>
              <span className="text-muted-foreground">Scheduler</span>
              <span className="flex flex-wrap items-center gap-2">
                <ToneBadge tone={sched!.tone}>{sched!.label}</ToneBadge>
                {health.scheduler.state === "stale" && health.scheduler.last_seen && (
                  <span className="text-muted-foreground tabular-nums">since {clock(health.scheduler.last_seen, now)}</span>
                )}
              </span>
            </div>
            <ul className="divide-y lg:grid lg:grid-cols-[minmax(0,1fr)_auto_auto_auto] lg:gap-x-6">
              {health.validation.map((v) => {
                const n = next(v.source, v.collection)
                return (
                  <li key={`${v.source}:${v.collection}`} className="flex flex-col gap-1 py-3 last:pb-0 lg:col-span-4 lg:grid lg:grid-cols-subgrid lg:items-center lg:gap-x-6">
                    <div className="flex items-center justify-between gap-2 lg:contents">
                      <span className="min-w-0 font-medium wrap-anywhere">{v.collection}</span>
                      <span className="lg:order-last"><SyncNow source={v.source} collection={v.collection} /></span>
                    </div>
                    <span className="text-muted-foreground tabular-nums">
                      {config && (n?.next_at ? <>Next sync <span className="text-foreground">{clock(n.next_at, now)}</span></> : "Manual only")}
                    </span>
                    <span className="flex flex-wrap items-center gap-2 text-muted-foreground tabular-nums">
                      {v.status ? (
                        <>
                          Last validation <span className="text-foreground">{v.at && clock(v.at, now)}</span>
                          <ToneBadge tone={VALIDATION[v.status].tone}>{VALIDATION[v.status].label}</ToneBadge>
                        </>
                      ) : "Never validated"}
                    </span>
                  </li>
                )
              })}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  )
}
