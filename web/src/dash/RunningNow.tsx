import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import type { ActivityRun, HistoryRun } from "@/api/types"
import { Empty, Fact, RowsSkeleton, ShowIds, ToneBadge } from "@/dash/bits"
import { ago, bytes, clock, count, duration, speed } from "@/lib/format"
import { KIND, PHASE, outcome } from "@/lib/words"

// A running run whose counts have not moved for this long gets an amber
// "No progress for <time>" badge (operator decision 2026-11-12, §spec:ui-kit).
const STALL_S = 5 * 60

function RunRow({ run, now }: { run: ActivityRun; now: string }) {
  const nowMs = Date.parse(now)
  const stalledFor = (nowMs - Date.parse(run.last_progress_at)) / 1000
  const ended = run.state === "ended"
  const end = outcome(run.result, run.end_reason)
  const pct = run.items_to_fetch ? (100 * (run.items_done ?? 0)) / run.items_to_fetch : null
  const discovering = run.phase === "discovering" || run.phase === "listing"
  return (
    <li className="flex flex-col gap-2 py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 font-medium wrap-anywhere">{run.collection}</span>
        <span className="text-muted-foreground">· {KIND[run.kind]}</span>
        <span className="ml-auto flex flex-wrap gap-1">
          {ended && end ? (
            <ToneBadge tone={end.tone}>{end.label} at {clock(run.finished_at!, now)}</ToneBadge>
          ) : (
            <>
              {run.phase && <ToneBadge tone={null}>{PHASE[run.phase]}</ToneBadge>}
              {stalledFor > STALL_S && <ToneBadge tone="warning">No progress for {duration(stalledFor)}</ToneBadge>}
            </>
          )}
        </span>
      </div>
      {!ended && pct !== null && !discovering && <Progress value={pct} aria-label={`${count(run.items_done ?? 0)} of ${count(run.items_to_fetch!)} shows`} />}
      <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:flex sm:flex-wrap">
        {run.phase === "listing" && run.items_listed !== null ? (
          <Fact label="Listed" value={count(run.items_listed)} />
        ) : run.phase === "discovering" ? (
          <Fact label="Discovered" value={`${count(run.items_looked_up ?? 0)} of ${count(run.items_to_look_up ?? 0)}`} />
        ) : (
          <Fact label="Shows" value={`${count(run.items_done ?? 0)} of ${run.items_to_fetch === null ? "estimating" : count(run.items_to_fetch)}`} />
        )}
        {run.bytes_downloaded !== null && (
          <Fact label="Downloaded" value={run.bytes_to_fetch === null ? bytes(run.bytes_downloaded) : `${bytes(run.bytes_downloaded)} of ${bytes(run.bytes_to_fetch)}`} />
        )}
        {!ended && run.speed_bytes_per_second !== null && <Fact label="Speed" value={speed(run.speed_bytes_per_second)} />}
        {!ended && <Fact label="Time left" value={run.eta_seconds === null ? "Estimating" : duration(run.eta_seconds)} />}
        {(run.items_failed ?? 0) > 0 && <Fact label="Failed" value={count(run.items_failed!)} tone="danger" />}
        {ended && <Fact label="Started" value={`${clock(run.started_at, now)}`} />}
      </div>
      {!ended && run.current_items.length > 0 && (
        <div className="flex gap-1 text-xs">
          <span className="shrink-0 text-muted-foreground">Now:</span>
          <ShowIds ids={run.current_items} max={4} />
        </div>
      )}
      {!ended && (
        <p className="text-xs text-muted-foreground">
          Started {clock(run.started_at, now)} · {ago(run.started_at, now)} ago
        </p>
      )}
    </li>
  )
}

// Only running runs: an ended run moves to Recent runs, and a killed one also to
// Needs attention (operator decision 2026-11-12, §spec:ui-kit).
export function RunningNow({ runs, lastRun, now }: {
  runs: ActivityRun[] | null
  lastRun: HistoryRun | null
  now: string
}) {
  const shown = runs?.filter((r) => r.state === "running") ?? null
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>Running now</CardTitle>
      </CardHeader>
      <CardContent>
        {shown === null ? (
          <RowsSkeleton />
        ) : shown.length === 0 ? (
          <Empty>
            Nothing is running.
            {lastRun?.finished_at && ` The last run ended ${ago(lastRun.finished_at, now)} ago.`}
          </Empty>
        ) : (
          <ul className="divide-y">{shown.map((r) => <RunRow key={r.activity_id} run={r} now={now} />)}</ul>
        )}
      </CardContent>
    </Card>
  )
}
