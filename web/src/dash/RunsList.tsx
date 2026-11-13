import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import type { HistoryRun } from "@/api/types"
import { ToneBadge } from "@/dash/bits"
import { bytes, clock, count, duration } from "@/lib/format"
import { KIND, outcome } from "@/lib/words"

function Result({ r }: { r: HistoryRun }) {
  const o = outcome(r.result, r.end_reason)
  return o ? <ToneBadge tone={o.tone}>{o.label}</ToneBadge> : <ToneBadge tone={null}>Running</ToneBadge>
}

function Problems({ r }: { r: HistoryRun }) {
  const errors = r.errors ?? []
  if (errors.length === 0 && !r.failure_notice_error) return null
  return (
    <div className="flex flex-col gap-0.5 font-mono text-xs wrap-anywhere whitespace-pre-wrap text-muted-foreground">
      {errors.slice(0, 2).map((e, i) => <span key={i}>{e}</span>)}
      {errors.length > 2 && <span className="font-sans">and {errors.length - 2} more</span>}
      {r.failure_notice_error && <span><span className="font-sans">Failure notice not sent: </span>{r.failure_notice_error}</span>}
    </div>
  )
}

const n = (v: number | null) => (v === null ? "—" : count(v))

/** Phone: one stacked row per run. sm and up: a table. */
export function RunsList({ runs, now }: { runs: HistoryRun[]; now: string }) {
  return (
    <>
      <ul className="divide-y lg:hidden">
        {runs.map((r) => (
          <li key={`${r.run_id}:${r.activity_id}`} className="flex flex-col gap-1 py-3 first:pt-0 last:pb-0">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="min-w-0 font-medium wrap-anywhere">{r.collection}</span>
              <span className="text-muted-foreground">· {KIND[r.kind]}</span>
              <span className="ml-auto"><Result r={r} /></span>
            </div>
            <div className="text-sm text-muted-foreground tabular-nums">
              {clock(r.started_at, now)}
              {r.duration_seconds !== null && ` · ${duration(r.duration_seconds)}`}
              {` · ${n(r.items_new)} new, ${n(r.items_updated)} updated`}
              {(r.items_failed ?? 0) > 0 && `, ${count(r.items_failed!)} failed`}
              {r.bytes_downloaded !== null && ` · ${bytes(r.bytes_downloaded)}`}
            </div>
            <Problems r={r} />
          </li>
        ))}
      </ul>
      <Table className="hidden lg:table">
        <TableHeader>
          <TableRow>
            <TableHead>Started</TableHead>
            <TableHead>Collection</TableHead>
            <TableHead>Run</TableHead>
            <TableHead className="text-right">Took</TableHead>
            <TableHead className="text-right">New</TableHead>
            <TableHead className="text-right">Updated</TableHead>
            <TableHead className="text-right">Failed</TableHead>
            <TableHead className="text-right">Downloaded</TableHead>
            <TableHead>Result</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {runs.map((r) => (
            <TableRow key={`${r.run_id}:${r.activity_id}`} className="align-top">
              <TableCell className="tabular-nums">{clock(r.started_at, now)}</TableCell>
              <TableCell className="max-w-48 font-medium whitespace-normal wrap-anywhere">{r.collection}</TableCell>
              <TableCell>{KIND[r.kind]}</TableCell>
              <TableCell className="text-right tabular-nums">{r.duration_seconds === null ? "—" : duration(r.duration_seconds)}</TableCell>
              <TableCell className="text-right tabular-nums">{n(r.items_new)}</TableCell>
              <TableCell className="text-right tabular-nums">{n(r.items_updated)}</TableCell>
              <TableCell className={`text-right tabular-nums ${(r.items_failed ?? 0) > 0 ? "text-destructive" : ""}`}>{n(r.items_failed)}</TableCell>
              <TableCell className="text-right tabular-nums">{r.bytes_downloaded === null ? "—" : bytes(r.bytes_downloaded)}</TableCell>
              <TableCell className="max-w-80 whitespace-normal">
                <div className="flex flex-col items-start gap-1"><Result r={r} /><Problems r={r} /></div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </>
  )
}
