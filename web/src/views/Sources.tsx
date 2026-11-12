import { Bar, BarChart, Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts"
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { type ChartConfig, ChartContainer } from "@/components/ui/chart"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import type { ActivityAnswer, NetworkAnswer, NetworkEvent } from "@/api/types"
import { Empty, RowsSkeleton } from "@/dash/bits"
import { clock, count, duration, plural, speed } from "@/lib/format"
import { EVENT } from "@/lib/words"

const speedConfig = { mbps: { label: "Download speed", color: "var(--chart-1)" } } satisfies ChartConfig
const problemConfig = { problems: { label: "Rate limits and errors", color: "var(--destructive)" } } satisfies ChartConfig
const day = (t: string) => new Date(t).toLocaleDateString("en-GB", { day: "numeric", month: "short" })

function eventText(e: NetworkEvent) {
  const status = e.http_status ? ` (${e.http_status})` : ""
  const wait = e.wait_seconds ? ` · waited ${duration(e.wait_seconds)}` : ""
  return `${EVENT[e.kind]}${status}${wait}`
}

function Events({ events, total, now }: { events: NetworkEvent[]; total: number; now: string }) {
  const shown = events.slice(0, 10)
  if (shown.length === 0) return <Empty>No rate limits, errors, or retries in the last 30 days.</Empty>
  return (
    <>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>When</TableHead>
            <TableHead>What</TableHead>
            <TableHead className="hidden sm:table-cell">Show</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {shown.map((e, i) => (
            <TableRow key={i} className="align-top">
              <TableCell className="tabular-nums">{clock(e.at, now)}</TableCell>
              <TableCell className="whitespace-normal">
                <div>{eventText(e)}</div>
                {e.detail && <div className="font-mono text-xs wrap-anywhere text-muted-foreground">{e.detail}</div>}
                {e.identifier && <div className="font-mono text-xs wrap-anywhere text-muted-foreground sm:hidden">{e.identifier}</div>}
              </TableCell>
              <TableCell className="hidden font-mono text-xs wrap-anywhere whitespace-normal text-muted-foreground sm:table-cell">{e.identifier ?? "—"}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {total > shown.length && <p className="pt-2 text-sm text-muted-foreground">and {count(total - shown.length)} more in the last 30 days</p>}
    </>
  )
}

export function SourcesView({ network, activity, now }: { network: NetworkAnswer | null; activity: ActivityAnswer | null; now: string }) {
  if (network === null) return <Card><CardContent><RowsSkeleton rows={6} /></CardContent></Card>
  if (network.sources.length === 0) return <Card><CardContent><Empty>Sources show up here once one is configured.</Empty></CardContent></Card>
  return (
    <div className="flex flex-col gap-4">
      {network.sources.map((s) => {
        const live = activity?.network.find((n) => n.source === s.source) ?? null
        const data = s.series.map((p) => ({
          t: p.t,
          mbps: p.bytes / network.bucket_seconds / 1e6,
          problems: p.rate_limited + p.server_errors + p.timeouts + p.connection_errors,
        }))
        return (
          <Card key={s.source} className="min-w-0">
            <CardHeader>
              <CardTitle>{s.source}</CardTitle>
              <CardAction className="text-right text-sm">
                <div className="tabular-nums">{speed(live?.speed_bytes_per_second ?? 0)}</div>
                <div className="text-muted-foreground">{plural(live?.requests_in_flight ?? 0, "request", "requests")} in flight</div>
              </CardAction>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              {data.length === 0 ? <Empty>No downloads in the last 30 days.</Empty> : (
                <div className="flex flex-col gap-1">
                  <h3 className="text-sm text-muted-foreground">Download speed, MB/s, last 30 days</h3>
                  <ChartContainer config={speedConfig} className="aspect-auto h-40 w-full" role="img"
                    aria-label={`Download speed from ${s.source} over the last 30 days`}>
                    <AreaChart data={data} margin={{ left: 0, right: 8, top: 8 }}>
                      <CartesianGrid vertical={false} />
                      <XAxis dataKey="t" tickLine={false} axisLine={false} minTickGap={48} tickFormatter={day} />
                      <YAxis width={32} tickLine={false} axisLine={false} />
                      <Area dataKey="mbps" type="step" isAnimationActive={false} stroke="var(--color-mbps)" fill="var(--color-mbps)" fillOpacity={0.2} />
                    </AreaChart>
                  </ChartContainer>
                  <h3 className="pt-2 text-sm text-muted-foreground">Rate limits and errors per hour</h3>
                  <ChartContainer config={problemConfig} className="aspect-auto h-16 w-full" role="img"
                    aria-label={`Rate limits and errors from ${s.source} per hour over the last 30 days`}>
                    <BarChart data={data} margin={{ left: 0, right: 8, top: 4 }}>
                      <XAxis dataKey="t" hide />
                      <YAxis width={32} tickLine={false} axisLine={false} allowDecimals={false} tickCount={2} />
                      <Bar dataKey="problems" isAnimationActive={false} fill="var(--color-problems)" />
                    </BarChart>
                  </ChartContainer>
                </div>
              )}
              <div className="flex flex-col gap-1">
                <h3 className="text-sm text-muted-foreground">Rate limits, errors, and retries</h3>
                <Events events={s.events} total={s.events_total} now={now} />
              </div>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}
