import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { type ChartConfig, ChartContainer } from "@/components/ui/chart"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import type { AttentionAnswer, LibraryAnswer } from "@/api/types"
import { Empty, RowsSkeleton, ShowIds, Stat, StatSkeleton } from "@/dash/bits"
import { ago, bytes, count, plural } from "@/lib/format"

const growthConfig = {
  shows: { label: "Mirrored", color: "var(--chart-1)" },
  tb: { label: "Archive", color: "var(--chart-2)" },
} satisfies ChartConfig
const day = (d: string) => new Date(`${d}T00:00:00Z`).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })

export function LibraryStats({ library }: { library: LibraryAnswer | null }) {
  if (library === null) return <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">{[0, 1, 2, 3].map((i) => <StatSkeleton key={i} />)}</div>
  const t = library.totals
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <Stat label="Mirrored" value={count(t.counts.complete)} note={`${count(t.counts.pending)} pending`} />
      <Stat label="Archive" value={bytes(t.archive_bytes)} note={plural(t.counts.complete, "show", "shows")} />
      <Stat label="Library" value={bytes(t.library_bytes)} note="No extra space" />
      <Stat label="Free space" value={bytes(library.disk.free_bytes)} note={`of ${bytes(library.disk.total_bytes)}`} />
    </div>
  )
}

function Growth({ library }: { library: LibraryAnswer }) {
  const byDate = new Map<string, { shows: number; tb: number }>()
  for (const g of library.growth) {
    const row = byDate.get(g.date) ?? { shows: 0, tb: 0 }
    row.shows += g.complete
    row.tb += g.archive_bytes / 1e12
    byDate.set(g.date, row)
  }
  const data = [...byDate.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([date, v]) => ({ date, ...v }))
  if (data.length < 2) return <Empty>Growth shows up here after a second day of runs.</Empty>
  const chart = (key: "shows" | "tb", title: string) => (
    <div className="flex min-w-0 flex-col gap-1">
      <h3 className="text-sm text-muted-foreground">{title}</h3>
      <ChartContainer config={growthConfig} className="aspect-auto h-36 w-full" role="img" aria-label={`${title}, last 30 days`}>
        <AreaChart data={data} margin={{ left: 0, right: 8, top: 8 }}>
          <CartesianGrid vertical={false} />
          <XAxis dataKey="date" tickLine={false} axisLine={false} minTickGap={40} tickFormatter={day} />
          <YAxis width={48} tickLine={false} axisLine={false} domain={["auto", "auto"]} tickCount={3} interval={0}
            tickFormatter={(v: number) => (key === "tb" ? v.toFixed(1) : count(v))} />
          <Area dataKey={key} type="monotone" isAnimationActive={false} stroke={`var(--color-${key})`} fill={`var(--color-${key})`} fillOpacity={0.15} />
        </AreaChart>
      </ChartContainer>
    </div>
  )
  return <div className="grid gap-4 sm:grid-cols-2">{chart("shows", "Mirrored shows")}{chart("tb", "Archive, TB")}</div>
}

function Collections({ library }: { library: LibraryAnswer }) {
  const cols = ["Mirrored", "Pending", "Failed", "Restricted", "Disappeared", "Archive", "Library"]
  return (
    <>
    <ul className="divide-y lg:hidden">
      {library.collections.map((c) => (
        <li key={`${c.source}:${c.collection}`} className="flex flex-col gap-0.5 py-3 first:pt-0 last:pb-0">
          <span className="font-medium wrap-anywhere">{c.collection} <span className="text-xs font-normal text-muted-foreground">· {c.source}</span></span>
          <span className="text-sm text-muted-foreground tabular-nums">
            {count(c.counts.complete)} mirrored · {count(c.counts.pending)} pending
            {c.counts.failed > 0 && <> · <span className="text-destructive">{count(c.counts.failed)} failed</span></>}
            {` · ${count(c.restricted)} restricted · ${count(c.counts.disappeared)} disappeared`}
          </span>
          <span className="text-sm text-muted-foreground tabular-nums">Archive {bytes(c.archive_bytes)} · Library {bytes(c.library_bytes)}</span>
        </li>
      ))}
    </ul>
    <Table className="hidden lg:table">
      <TableHeader>
        <TableRow>
          <TableHead>Collection</TableHead>
          {cols.map((c) => <TableHead key={c} className="text-right">{c}</TableHead>)}
        </TableRow>
      </TableHeader>
      <TableBody>
        {library.collections.map((c) => (
          <TableRow key={`${c.source}:${c.collection}`}>
            <TableCell>
              <div className="max-w-48 font-medium whitespace-normal wrap-anywhere">{c.collection}</div>
              <div className="text-xs text-muted-foreground">{c.source}</div>
            </TableCell>
            <TableCell className="text-right tabular-nums">{count(c.counts.complete)}</TableCell>
            <TableCell className="text-right tabular-nums">{count(c.counts.pending)}</TableCell>
            <TableCell className={`text-right tabular-nums ${c.counts.failed ? "text-destructive" : ""}`}>{count(c.counts.failed)}</TableCell>
            <TableCell className="text-right tabular-nums">{count(c.restricted)}</TableCell>
            <TableCell className="text-right tabular-nums">{count(c.counts.disappeared)}</TableCell>
            <TableCell className="text-right tabular-nums">{bytes(c.archive_bytes)}</TableCell>
            <TableCell className="text-right tabular-nums">{bytes(c.library_bytes)}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
    </>
  )
}

function Health({ attention }: { attention: AttentionAnswer }) {
  const rows = attention.health.filter((h) =>
    h.restricted.length || h.removed_upstream.length || h.drift_files || h.not_playable.length || h.playable_copies.length)
  if (rows.length === 0) return <Empty>Every mirrored show is complete and playable.</Empty>
  return (
    <ul className="divide-y">
      {rows.map((h) => (
        <li key={`${h.source}:${h.collection}`} className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
          <span className="font-medium wrap-anywhere">{h.collection}</span>
          {h.restricted.length > 0 && (
            <div className="flex flex-col gap-0.5 text-sm">
              <span>{plural(h.restricted.length, "show", "shows")} restricted</span>
              <ul className="font-mono text-xs wrap-anywhere text-muted-foreground">
                {h.restricted.slice(0, 3).map((r) => <li key={r.identifier}>{r.identifier}: {r.reason}</li>)}
              </ul>
              {h.restricted.length > 3 && <span className="text-xs text-muted-foreground">and {h.restricted.length - 3} more</span>}
            </div>
          )}
          {h.removed_upstream.length > 0 && (
            <div className="flex flex-col gap-0.5 text-sm">
              <span>{plural(h.removed_upstream.length, "show", "shows")} disappeared from the source, kept here</span>
              <ul className="font-mono text-xs wrap-anywhere text-muted-foreground">
                {h.removed_upstream.slice(0, 3).map((r) => <li key={r.identifier}>{r.identifier}<span className="font-sans"> · missing since {day(r.removed_on)}</span></li>)}
              </ul>
              {h.removed_upstream.length > 3 && <span className="text-xs text-muted-foreground">and {h.removed_upstream.length - 3} more</span>}
            </div>
          )}
          {h.drift_files > 0 && <span className="text-sm">Drift: {plural(h.drift_files, "file", "files")} changed since Shakedown recorded them</span>}
          {h.not_playable.length > 0 && (
            <div className="flex flex-col gap-0.5 text-sm">
              <span>{plural(h.not_playable.length, "show has", "shows have")} no playable copy</span>
              <ul className="font-mono text-xs wrap-anywhere text-muted-foreground">
                {h.not_playable.slice(0, 3).map((r) => <li key={r.identifier}>{r.identifier}: {r.reason}</li>)}
              </ul>
              {h.not_playable.length > 3 && <span className="text-xs text-muted-foreground">and {h.not_playable.length - 3} more</span>}
            </div>
          )}
          {h.playable_copies.length > 0 && (
            <div className="flex flex-col gap-0.5 text-sm">
              <span>{plural(h.playable_copies.length, "show is", "shows are")} kept as the original plus a playable copy</span>
              <ShowIds ids={h.playable_copies} />
            </div>
          )}
        </li>
      ))}
    </ul>
  )
}

export function LibraryView({ library, attention, now }: { library: LibraryAnswer | null; attention: AttentionAnswer | null; now: string }) {
  return (
    <div className="flex flex-col gap-4">
      <LibraryStats library={library} />
      <Card className="min-w-0">
        <CardHeader><CardTitle>Collections</CardTitle></CardHeader>
        <CardContent>{library ? <Collections library={library} /> : <RowsSkeleton rows={3} />}</CardContent>
      </Card>
      <Card className="min-w-0">
        <CardHeader><CardTitle>Growth</CardTitle><CardDescription>Last 30 days, all collections</CardDescription></CardHeader>
        <CardContent>{library ? <Growth library={library} /> : <RowsSkeleton rows={2} />}</CardContent>
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="min-w-0">
          <CardHeader><CardTitle>Recently mirrored</CardTitle></CardHeader>
          <CardContent>
            {library === null ? <RowsSkeleton /> : library.recently_mirrored.length === 0
              ? <Empty>Shows show up here as they are mirrored.</Empty>
              : (
                <ul className="divide-y">
                  {library.recently_mirrored.slice(0, 10).map((m) => (
                    <li key={`${m.source}:${m.identifier}`} className="flex flex-col gap-0.5 py-2 first:pt-0 last:pb-0">
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="min-w-0 break-words">{m.title ?? m.identifier}</span>
                        <span className="shrink-0 text-xs text-muted-foreground tabular-nums">{ago(m.downloaded_at, now)} ago</span>
                      </div>
                      <span className="text-xs text-muted-foreground">
                        {m.collection}
                        {m.title && <span className="font-mono wrap-anywhere"> · {m.identifier}</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
          </CardContent>
        </Card>
        <Card className="min-w-0">
          <CardHeader><CardTitle>Library health</CardTitle></CardHeader>
          <CardContent>{attention ? <Health attention={attention} /> : <RowsSkeleton />}</CardContent>
        </Card>
      </div>
    </div>
  )
}
