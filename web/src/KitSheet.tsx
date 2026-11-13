// Kit sheet: every token and component state the screens are built from.
// Review artifact for Direction & Kit; also the subject of the kit screenshot test.
import { AlertTriangle, CircleAlert } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"

const COLORS = [
  "background", "foreground", "card", "muted", "muted-foreground", "border",
  "primary", "primary-foreground", "success", "warning", "destructive", "ring",
] as const

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-sm font-medium text-muted-foreground">{title}</h2>
      {children}
    </section>
  )
}

export function KitSheet() {
  return (
    <div className="mx-auto flex min-w-0 max-w-5xl flex-col gap-8 p-4 sm:p-6">
      <h1 className="text-xl font-semibold">Kit</h1>

      <Section title="Colors">
        <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
          {COLORS.map((c) => (
            <div key={c} className="flex min-w-0 flex-col gap-1">
              <div className="h-10 rounded-md border" style={{ background: `var(--${c})` }} />
              <span className="truncate font-mono text-xs text-muted-foreground">{c}</span>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Type">
        <div className="flex flex-col gap-2">
          <span className="text-xl font-semibold">Page title · text-xl semibold</span>
          <span className="text-2xl font-semibold tabular-nums">31,204 · figure · text-2xl semibold</span>
          <span className="font-semibold">Card title · text-base semibold</span>
          <span>Body · text-base</span>
          <span className="text-sm text-muted-foreground">Label and secondary · text-sm muted</span>
          <span className="font-mono text-xs text-muted-foreground">gd1977-05-08.sbd.hicks.4982.sbeok.shnf · identifier · mono xs</span>
        </div>
      </Section>

      <Section title="Radius and spacing">
        <div className="flex flex-wrap items-end gap-4">
          {["rounded-sm", "rounded-md", "rounded-lg", "rounded-xl", "rounded-full"].map((r) => (
            <div key={r} className="flex flex-col items-center gap-1">
              <div className={`size-12 border bg-muted ${r}`} />
              <span className="font-mono text-xs text-muted-foreground">{r}</span>
            </div>
          ))}
          {[1, 2, 3, 4, 6, 8].map((n) => (
            <div key={n} className="flex flex-col items-center gap-1">
              <div className="bg-primary" style={{ width: `calc(var(--density) * ${n})`, height: "3rem" }} />
              <span className="font-mono text-xs text-muted-foreground">{n}</span>
            </div>
          ))}
        </div>
      </Section>

      <Section title="Badges">
        <div className="flex flex-wrap gap-2">
          <Badge variant="success">Completed</Badge>
          <Badge variant="warning">No progress for 41 min</Badge>
          <Badge variant="danger">Stopped reporting</Badge>
          <Badge variant="secondary">3</Badge>
          <Badge variant="secondary">Downloading</Badge>
        </div>
      </Section>

      <Section title="Progress">
        <div className="flex flex-col gap-3">
          <Progress value={36} />
          <Progress value={100} />
          <Progress value={0} />
        </div>
      </Section>

      <Section title="Notices">
        <Alert variant="warning">
          <AlertTriangle />
          <AlertTitle>Can't reach Shakedown</AlertTitle>
          <AlertDescription>Showing data from 4 min ago.</AlertDescription>
        </Alert>
        <Alert variant="destructive">
          <CircleAlert />
          <AlertTitle>shakedown.yaml has an error</AlertTitle>
          <AlertDescription>sources[0].collections[1].layout: unknown field "{'{'}show{'}'}"</AlertDescription>
        </Alert>
      </Section>

      <Section title="Card, stat, and loading">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Card className="min-w-0 gap-1 py-4">
            <CardHeader className="px-4">
              <CardDescription>Free space</CardDescription>
              <div className="text-2xl leading-none font-semibold tabular-nums">5.20 TB</div>
            </CardHeader>
            <CardContent className="px-4 text-sm text-muted-foreground">of 16.0 TB</CardContent>
          </Card>
          <Card className="min-w-0 gap-2 py-4">
            <CardHeader className="px-4">
              <Skeleton className="h-4 w-20" />
              <Skeleton className="h-7 w-24" />
            </CardHeader>
            <CardContent className="px-4"><Skeleton className="h-4 w-16" /></CardContent>
          </Card>
        </div>
        <Card>
          <CardHeader>
            <CardTitle>Needs attention</CardTitle>
            <CardDescription>Card with header action</CardDescription>
            <CardAction><Badge variant="secondary">0</Badge></CardAction>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">Nothing needs your attention.</CardContent>
        </Card>
      </Section>

      <Section title="Table">
        <Card>
          <CardContent>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Collection</TableHead>
                  <TableHead>Run</TableHead>
                  <TableHead className="text-right">Downloaded</TableHead>
                  <TableHead>Result</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                <TableRow>
                  <TableCell className="font-medium">phish</TableCell>
                  <TableCell>Sync</TableCell>
                  <TableCell className="text-right tabular-nums">8.72 GB</TableCell>
                  <TableCell><Badge variant="success">Completed</Badge></TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-medium">etree-lma</TableCell>
                  <TableCell>Sync</TableCell>
                  <TableCell className="text-right tabular-nums">6.31 GB</TableCell>
                  <TableCell><Badge variant="warning">Completed, some shows failed</Badge></TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </Section>
    </div>
  )
}
