// One card per source: its request ceiling and its collections, each opening the
// collection editor. No login: no shipped source needs one
// (§spec:dashboard-credentials, withdrawn 2026-11-25).
import { ChevronRight } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import type { ConfigAnswer, SourceConfig } from "@/api/types"
import { clock } from "@/lib/format"
import { scheduleLine } from "@/lib/schedule"
import { useDraft } from "@/settings/draft"
import { addHref, collectionHref } from "@/settings/route"
import { TextSetting } from "@/settings/fields"

export function SourceCard({ index, source, config, now, fresh }: {
  index: number; source: SourceConfig; config: ConfigAnswer; now: string
  /** The collection being added, not saved yet. */
  fresh: number | null
}) {
  const d = useDraft()
  const problemsAt = (j: number) =>
    config.errors.filter((e) => e.loc[0] === "sources" && e.loc[1] === index && e.loc[2] === "collections" && e.loc[3] === j).length
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="wrap-anywhere">{source.name}</CardTitle>
        <CardDescription>Type {source.type}</CardDescription>
        {d.editable && (
          <CardAction>
            <Button size="sm" variant="outline" asChild>
              <a href={addHref(source.name)}>Add collection</a>
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <div className="grid gap-6 lg:grid-cols-2">
          <TextSetting
            path={["sources", index, "max_concurrent_requests"]}
            number
            blank={null}
            label="Requests at once"
            placeholder="Same as downloads at once"
            help="The most connections Shakedown opens to this source, across all its collections."
          />
        </div>
        <div className="flex flex-col gap-2">
          <h3 className="text-sm text-muted-foreground">Collections</h3>
          {source.collections.length === 0 && <p className="text-sm text-muted-foreground">No collections yet.</p>}
          <ul className="divide-y rounded-md border empty:hidden">
            {source.collections.map((c, j) => {
              const next = config.next_syncs.find((n) => n.source === source.name && n.collection === c.name)?.next_at
              const errors = problemsAt(j)
              return (
                <li key={c.name}>
                  <a
                    href={j === fresh ? addHref(source.name) : collectionHref(source.name, c.name)}
                    className="flex items-center gap-3 px-3 py-2.5 outline-none hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50"
                  >
                    <div className="flex min-w-0 flex-1 flex-col gap-0.5 lg:flex-row lg:items-center lg:gap-6">
                      <span className="min-w-0 font-medium wrap-anywhere lg:w-56">{c.name || "New collection"}</span>
                      <span className="text-sm text-muted-foreground tabular-nums lg:w-64">{scheduleLine(c.schedule)}</span>
                      {j === fresh && <span className="text-sm text-muted-foreground">Not saved yet</span>}
                      {next && <span className="text-sm text-muted-foreground tabular-nums">Next sync {clock(next, now)}</span>}
                      {errors > 0 && (
                        <span className="text-sm text-destructive">{errors === 1 ? "1 setting has an error" : `${errors} settings have errors`}</span>
                      )}
                    </div>
                    <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                    <span className="sr-only">{d.editable ? "Edit" : "Show"} {c.name || "new collection"}</span>
                  </a>
                </li>
              )
            })}
          </ul>
        </div>
      </CardContent>
    </Card>
  )
}
