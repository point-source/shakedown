// Small building blocks shared by every view. Kit components only.
import type { ReactNode } from "react"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import type { Tone } from "@/lib/words"

export function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
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

export function StatSkeleton() {
  return (
    <Card className="min-w-0 gap-2 py-4" aria-hidden>
      <CardHeader className="px-4">
        <Skeleton className="h-4 w-20" />
        <Skeleton className="h-6 w-24" />
      </CardHeader>
      <CardContent className="px-4"><Skeleton className="h-4 w-16" /></CardContent>
    </Card>
  )
}

/** Loading body for a card whose first answer has not arrived (§spec:ui-kit). */
export function RowsSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-3" aria-hidden>
      {Array.from({ length: rows }, (_, i) => <Skeleton key={i} className="h-10 w-full" />)}
    </div>
  )
}

export function Fact({ label, value, tone }: { label: string; value: string; tone?: Tone }) {
  const color = tone === "danger" ? "text-destructive" : tone === "warning" ? "text-warning" : ""
  return (
    <div className="min-w-0">
      <div className="text-muted-foreground">{label}</div>
      <div className={`tabular-nums sm:whitespace-nowrap ${color}`}>{value}</div>
    </div>
  )
}

export function ToneBadge({ tone, children }: { tone: Tone | null; children: ReactNode }) {
  return <Badge variant={tone ?? "secondary"}>{children}</Badge>
}

/** Show IDs as plain text, wrapping on any character; the rest counted. */
export function ShowIds({ ids, max = 3 }: { ids: string[]; max?: number }) {
  if (ids.length === 0) return null
  const shown = ids.slice(0, max)
  const more = ids.length - shown.length
  return (
    <p className="font-mono text-xs wrap-anywhere text-muted-foreground">
      {shown.join(", ")}
      {more > 0 && <span className="font-sans"> and {more} more</span>}
    </p>
  )
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted-foreground">{children}</p>
}
