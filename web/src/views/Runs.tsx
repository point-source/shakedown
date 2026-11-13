import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import type { LibraryAnswer, RunsAnswer } from "@/api/types"
import { Empty, RowsSkeleton } from "@/dash/bits"
import { RunsList } from "@/dash/RunsList"
import { ago, clock } from "@/lib/format"

export function LastCompletedSync({ library, now }: { library: LibraryAnswer | null; now: string }) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>Last completed sync</CardTitle>
      </CardHeader>
      <CardContent>
        {library === null ? <RowsSkeleton rows={2} /> : (
          <ul className="divide-y">
            {library.collections.map((c) => (
              <li key={`${c.source}:${c.collection}`} className="flex items-baseline justify-between gap-3 py-2 first:pt-0 last:pb-0">
                <span className="min-w-0 truncate font-medium" title={c.collection}>{c.collection}</span>
                <span className="shrink-0 text-sm tabular-nums">
                  {c.last_successful_sync_at
                    ? <>{ago(c.last_successful_sync_at, now)} ago <span className="text-muted-foreground">· {clock(c.last_successful_sync_at, now)}</span></>
                    : <span className="text-muted-foreground">None yet</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

export function RunsView({ runs, library, now }: { runs: RunsAnswer | null; library: LibraryAnswer | null; now: string }) {
  return (
    <div className="flex flex-col gap-4">
      <LastCompletedSync library={library} now={now} />
      <Card className="min-w-0">
        <CardHeader>
          <CardTitle>Runs</CardTitle>
          <CardDescription>Last 30 days, newest first</CardDescription>
        </CardHeader>
        <CardContent>
          {runs === null ? <RowsSkeleton rows={6} /> : runs.runs.length === 0
            ? <Empty>Runs show up here once the first one starts.</Empty>
            : <RunsList runs={runs.runs} now={now} />}
        </CardContent>
      </Card>
    </div>
  )
}
