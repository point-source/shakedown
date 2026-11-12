// Representative screen for comparing token sets (Direction & Kit review).
// Not the approved page layout: the screens are designed in the Screens step.
import { Area, AreaChart, CartesianGrid, XAxis, YAxis } from "recharts"
import { Badge } from "@/components/ui/badge"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { type ChartConfig, ChartContainer } from "@/components/ui/chart"
import { Progress } from "@/components/ui/progress"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import type { ActivityRun, AttentionEntry, HistoryRun, Result, RunKind } from "@/api/types"
import { activity, attention, health, library, network, runs, NOW } from "@/fixtures/default"
import { ago, bytes, clock, count, duration, plural, speed } from "@/lib/format"

const KIND: Record<RunKind, string> = {
  sync: "Sync", catch_up: "Catch-up sync", refetch: "Refetch", reconform: "Reconform",
  restage: "Restage", verify: "Verify", reconcile: "Reconcile",
}
const PHASE: Record<string, string> = {
  starting: "Starting", listing: "Listing", discovering: "Discovering", downloading: "Downloading",
  tagging: "Tagging", staging: "Updating library", notifying: "Sending notices", verifying: "Verifying",
  finishing: "Finishing",
}
const RESULT: Record<Result, { label: string; tone: "success" | "warning" | "danger" }> = {
  completed: { label: "Completed", tone: "success" },
  completed_with_item_issues: { label: "Completed, some shows failed", tone: "warning" },
  failed_before_completion: { label: "Failed", tone: "danger" },
  stale_enumeration: { label: "Listing incomplete", tone: "warning" },
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <Card className="min-w-0 gap-1 py-4">
      <CardHeader className="px-4">
        <CardDescription>{label}</CardDescription>
        <div className="text-2xl leading-none font-semibold tabular-nums">{value}</div>
      </CardHeader>
      {note && <CardContent className="px-4 text-sm text-muted-foreground">{note}</CardContent>}
    </Card>
  )
}

function RunRow({ run }: { run: ActivityRun }) {
  const stalledFor = (Date.parse(NOW) - Date.parse(run.last_progress_at)) / 1000
  const stalled = run.state === "running" && stalledFor > 60
  const pct = run.items_to_fetch ? (100 * (run.items_done ?? 0)) / run.items_to_fetch : 0
  return (
    <div className="flex flex-col gap-2 py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{run.collection}</span>
        <span className="text-muted-foreground">· {KIND[run.kind]}</span>
        <span className="ml-auto">
          {run.state === "ended" ? (
            <Badge variant="danger">Stopped reporting at {clock(run.finished_at!, NOW)}</Badge>
          ) : stalled ? (
            <Badge variant="warning">No progress for {duration(stalledFor)}</Badge>
          ) : (
            <Badge variant="secondary">{PHASE[run.phase!]}</Badge>
          )}
        </span>
      </div>
      {run.state === "running" && run.items_to_fetch !== null && <Progress value={pct} />}
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
        {run.phase === "discovering" ? (
          <Fact label="Discovered" value={`${count(run.items_looked_up ?? 0)} of ${count(run.items_to_look_up ?? 0)}`} />
        ) : (
          <Fact label="Shows" value={`${count(run.items_done ?? 0)} of ${run.items_to_fetch === null ? "estimating" : count(run.items_to_fetch)}`} />
        )}
        <Fact label="Downloaded" value={run.bytes_to_fetch === null ? bytes(run.bytes_downloaded) : `${bytes(run.bytes_downloaded)} of ${bytes(run.bytes_to_fetch)}`} />
        <Fact label="Speed" value={speed(run.speed_bytes_per_second)} />
        <Fact label="Time left" value={run.state === "ended" ? "—" : run.eta_seconds === null ? "Estimating" : duration(run.eta_seconds)} />
      </div>
      {run.state === "running" && run.current_items.length > 0 && (
        <p className="truncate font-mono text-xs text-muted-foreground">Now: {run.current_items.join(", ")}</p>
      )}
    </div>
  )
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="text-muted-foreground">{label}</div>
      <div className="truncate tabular-nums">{value}</div>
    </div>
  )
}

function attentionText(e: AttentionEntry): { title: string; next: string } {
  switch (e.kind) {
    case "failed_shows":
      return { title: `${plural(e.identifiers.length, "show", "shows")} failed`, next: "The next sync retries them." }
    case "rescheduled":
      return { title: `Catch-up sync waiting since ${clock(e.since!, NOW)}`, next: "Runs when the current sync ends." }
    case "handoff_pending":
      return { title: `Handoff notice not sent for ${plural(Number(e.detail.shows), "show", "shows")}`, next: "The next run sends it again." }
    default:
      return { title: e.kind, next: "" }
  }
}

const chartConfig = { speed: { label: "Download speed", color: "var(--chart-1)" } } satisfies ChartConfig

export function KitPreview() {
  const ia = network.sources[0]
  const chartData = ia.series.map((p) => ({ t: p.t, speed: p.bytes / network.bucket_seconds / 1e6 }))
  const lastRuns: HistoryRun[] = runs.runs.filter((r) => r.finished_at)
  return (
    <main className="mx-auto flex min-w-0 max-w-6xl flex-col gap-4 p-4 sm:p-6">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h1 className="text-xl font-semibold">Shakedown</h1>
        <span className="text-sm text-muted-foreground">Release {health.release}</span>
        <span className="ml-auto flex items-center gap-2 text-sm text-muted-foreground">
          <span className="size-2 rounded-full bg-success" aria-hidden />
          Updated {clock(activity.generated_at, NOW)}
        </span>
      </header>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
        <Stat label="Mirrored" value={count(library.totals.counts.complete)} note={`${count(library.totals.counts.pending)} pending`} />
        <Stat label="Archive" value={bytes(library.totals.archive_bytes)} note={`Library ${bytes(library.totals.library_bytes)}, no extra space`} />
        <Stat label="Free space" value={bytes(library.disk.free_bytes)} note={`of ${bytes(library.disk.total_bytes)}`} />
      </div>

      <div className="grid gap-4 lg:grid-cols-5">
        <Card className="min-w-0 lg:col-span-3">
          <CardHeader>
            <CardTitle>Running now</CardTitle>
            <CardDescription>And runs that ended in the last 10 minutes</CardDescription>
          </CardHeader>
          <CardContent className="divide-y">
            {activity.runs.map((r) => <RunRow key={r.activity_id} run={r} />)}
          </CardContent>
        </Card>

        <Card className="min-w-0 lg:col-span-2">
          <CardHeader>
            <CardTitle>Needs attention</CardTitle>
            <CardAction><Badge variant="secondary">{attention.entries.length}</Badge></CardAction>
          </CardHeader>
          <CardContent>
            <ul className="divide-y">
              {attention.entries.map((e) => {
                const t = attentionText(e)
                return (
                  <li key={e.kind + e.collection} className="flex flex-col gap-0.5 py-3 first:pt-0 last:pb-0">
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="font-medium">{t.title}</span>
                      <span className="shrink-0 text-xs text-muted-foreground">{e.collection}</span>
                    </div>
                    <span className="text-sm text-muted-foreground">{t.next}</span>
                  </li>
                )
              })}
            </ul>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Download speed</CardTitle>
          <CardDescription>{ia.source} · MB/s, last 30 days</CardDescription>
          <CardAction className="text-right text-sm">
            <div className="tabular-nums">{speed(activity.network[0].speed_bytes_per_second)}</div>
            <div className="text-muted-foreground">{plural(activity.network[0].requests_in_flight, "request", "requests")} in flight</div>
          </CardAction>
        </CardHeader>
        <CardContent>
          <ChartContainer config={chartConfig} className="aspect-auto h-48 w-full" role="img" aria-label={`Download speed from ${ia.source} over the last 30 days`}>
            <AreaChart data={chartData} margin={{ left: 0, right: 8, top: 8 }}>
              <CartesianGrid vertical={false} />
              <XAxis dataKey="t" tickLine={false} axisLine={false} minTickGap={48}
                tickFormatter={(v: string) => new Date(v).toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })} />
              <YAxis width={32} tickLine={false} axisLine={false} />
              <Area dataKey="speed" type="step" isAnimationActive={false} stroke="var(--color-speed)" fill="var(--color-speed)" fillOpacity={0.2} />
            </AreaChart>
          </ChartContainer>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Recent runs</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Collection</TableHead>
                <TableHead>Run</TableHead>
                <TableHead>Started</TableHead>
                <TableHead className="text-right">New</TableHead>
                <TableHead className="text-right">Downloaded</TableHead>
                <TableHead>Result</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {lastRuns.map((r) => (
                <TableRow key={r.activity_id}>
                  <TableCell className="font-medium">{r.collection}</TableCell>
                  <TableCell>{KIND[r.kind]}</TableCell>
                  <TableCell className="tabular-nums">{clock(r.started_at, NOW)} · {ago(r.started_at, NOW)} ago</TableCell>
                  <TableCell className="text-right tabular-nums">{count(r.items_new ?? 0)}</TableCell>
                  <TableCell className="text-right tabular-nums">{r.bytes_downloaded === null ? "—" : bytes(r.bytes_downloaded)}</TableCell>
                  <TableCell><Badge variant={RESULT[r.result!].tone}>{r.end_reason ? "Stopped reporting" : RESULT[r.result!].label}</Badge></TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </main>
  )
}
