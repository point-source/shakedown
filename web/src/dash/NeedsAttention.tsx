import { Badge } from "@/components/ui/badge"
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { AttentionEntry, Recovery } from "@/api/types"
import { Empty, RowsSkeleton, ShowIds } from "@/dash/bits"
import { clock } from "@/lib/format"
import { attentionWords } from "@/lib/words"

function Entry({ e, now }: { e: AttentionEntry; now: string }) {
  const w = attentionWords(e, now)
  const d = e.detail as Record<string, unknown>
  const recovery = (e.kind === "recovery" ? d : d.recovery) as Recovery | undefined
  const reason = (d.last_reason ?? d.failure_notice_error) as string | undefined
  const files = (d.unplaced_files ?? []) as string[]
  return (
    <li className="flex flex-col gap-1 py-3 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <span className="font-medium">{w.title}</span>
        <span className="min-w-0 text-xs wrap-anywhere text-muted-foreground">{e.collection}</span>
      </div>
      {w.next && <span className="text-sm text-muted-foreground">{w.next}</span>}
      {recovery?.safe_next_action && (
        <span className="text-sm">
          <span className="text-muted-foreground">Next step: </span>
          {recovery.safe_next_action}
        </span>
      )}
      {reason && <p className="font-mono text-xs wrap-anywhere whitespace-pre-wrap text-muted-foreground">{reason}</p>}
      <ShowIds ids={e.kind === "unplaced_files" ? files : e.identifiers} />
      {e.since && e.kind !== "rescheduled" && !e.kind.startsWith("ended") && (
        <span className="text-xs text-muted-foreground">Since {clock(e.since, now)}</span>
      )}
    </li>
  )
}

export function NeedsAttention({ entries, now }: { entries: AttentionEntry[] | null; now: string }) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>Needs attention</CardTitle>
        {entries && entries.length > 0 && <CardAction><Badge variant="secondary">{entries.length}</Badge></CardAction>}
      </CardHeader>
      <CardContent>
        {entries === null ? (
          <RowsSkeleton />
        ) : entries.length === 0 ? (
          <Empty>Nothing needs your attention.</Empty>
        ) : (
          <ul className="divide-y">{entries.map((e) => <Entry key={`${e.kind}:${e.source}:${e.collection}`} e={e} now={now} />)}</ul>
        )}
      </CardContent>
    </Card>
  )
}
