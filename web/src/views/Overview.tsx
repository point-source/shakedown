import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { Dashboard } from "@/data/useDashboard"
import { Empty, RowsSkeleton, Stat, StatSkeleton, ToneBadge } from "@/dash/bits"
import { NeedsAttention } from "@/dash/NeedsAttention"
import { RunningNow } from "@/dash/RunningNow"
import { RunsList } from "@/dash/RunsList"
import { bytes, clock, count } from "@/lib/format"
import { VALIDATION } from "@/lib/words"

export function Overview({ d, now, onAllRuns }: { d: Dashboard; now: string; onAllRuns: () => void }) {
  const lib = d.library.data
  const runs = d.runs.data?.runs ?? null
  const finished = runs?.filter((r) => r.finished_at) ?? null
  const health = d.health.data
  // History is sorted by start; parallel runs can finish in another order.
  const lastRun = finished?.reduce((a, b) => (Date.parse(b.finished_at!) > Date.parse(a.finished_at!) ? b : a), finished[0]) ?? null
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 [&>*:nth-child(3)]:col-span-2 sm:[&>*:nth-child(3)]:col-span-1">
        {lib ? (
          <>
            <Stat label="Mirrored" value={count(lib.totals.counts.complete)} note={`${count(lib.totals.counts.pending)} pending`} />
            <Stat label="Archive" value={bytes(lib.totals.archive_bytes)} />
            <Stat label="Free space" value={bytes(lib.disk.free_bytes)} note={`of ${bytes(lib.disk.total_bytes)}`} />
          </>
        ) : [0, 1, 2].map((i) => <StatSkeleton key={i} />)}
      </div>
      <div className="grid gap-4 lg:grid-cols-5">
        {/* Phone: Needs attention first. Desktop: Running now on the left (operator, 2026-11-12). */}
        <div className="min-w-0 lg:order-2 lg:col-span-2"><NeedsAttention entries={d.attention.data?.entries ?? null} now={now} /></div>
        <div className="min-w-0 lg:order-1 lg:col-span-3">
          <RunningNow runs={d.activity.data?.runs ?? null} lastRun={lastRun} now={now} />
        </div>
      </div>
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>Recent runs</CardTitle>
          <CardAction>
            <button type="button" onClick={onAllRuns} className="text-sm text-primary underline-offset-4 hover:underline focus-visible:underline">
              All runs
            </button>
          </CardAction>
        </CardHeader>
        <CardContent>
          {finished === null ? <RowsSkeleton /> : finished.length === 0
            ? <Empty>Runs show up here once the first one ends.</Empty>
            : <RunsList runs={finished.slice(0, 5)} now={now} />}
        </CardContent>
      </Card>
      <Card className="min-w-0">
        <CardHeader><CardTitle>Shakedown</CardTitle></CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          {health === null ? <RowsSkeleton rows={2} /> : (
            <>
              <div className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2">
                <span className="text-muted-foreground">Config</span>
                <span>
                  {health.config.valid
                    ? <ToneBadge tone="success">Valid</ToneBadge>
                    : <ToneBadge tone="danger">Has an error</ToneBadge>}
                </span>
              </div>
              <div className="flex flex-col gap-1">
                <span className="text-muted-foreground">Last validation</span>
                <ul className="divide-y">
                  {health.validation.map((v) => (
                    <li key={`${v.source}:${v.collection}`} className="flex flex-wrap items-center justify-between gap-2 py-2 last:pb-0">
                      <span className="font-medium">{v.collection}</span>
                      {v.status ? (
                        <span className="flex items-center gap-2">
                          <span className="text-muted-foreground tabular-nums">{v.at && clock(v.at, now)}</span>
                          <ToneBadge tone={VALIDATION[v.status].tone}>{VALIDATION[v.status].label}</ToneBadge>
                        </span>
                      ) : <span className="text-muted-foreground">Never validated</span>}
                    </li>
                  ))}
                </ul>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
