import { useEffect, useState } from "react"
import { AlertTriangle, CircleAlert } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { staleSince, useDashboard, useNow } from "@/data/useDashboard"
import { ago, clock } from "@/lib/format"
import { Overview } from "@/views/Overview"
import { RunsView } from "@/views/Runs"
import { SourcesView } from "@/views/Sources"
import { LibraryView } from "@/views/Library"

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "runs", label: "Runs" },
  { id: "sources", label: "Sources" },
  { id: "library", label: "Library" },
] as const
type TabId = (typeof TABS)[number]["id"]

function tabFromHash(): TabId {
  const h = location.hash.slice(1)
  return TABS.some((t) => t.id === h) ? (h as TabId) : "overview"
}

export function App() {
  const d = useDashboard()
  const nowMs = useNow()
  const now = new Date(nowMs).toISOString()
  const [tab, setTab] = useState<TabId>(tabFromHash)
  useEffect(() => {
    const on = () => setTab(tabFromHash())
    window.addEventListener("hashchange", on)
    return () => window.removeEventListener("hashchange", on)
  }, [])
  const go = (t: string) => {
    history.replaceState(null, "", `#${t}`)
    setTab(t as TabId)
    window.scrollTo(0, 0)
  }

  const slots = [d.activity, d.network, d.runs, d.library, d.attention, d.health]
  const noConfig = slots.some((s) => s.noConfig) && slots.every((s) => s.data === null)
  const neverReached = !noConfig && slots.every((s) => s.data === null && s.error !== null)
  const stale = staleSince(d, nowMs)
  const config = d.health.data?.config
  const configError = noConfig ? slots.find((s) => s.noConfig)?.error ?? null : config && !config.valid ? config.error : null
  const updated = d.activity.data?.generated_at ?? null

  return (
    <div className="mx-auto flex min-w-0 max-w-6xl flex-col gap-4 p-4 sm:p-6">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <h1 className="text-xl font-semibold">Shakedown</h1>
        {d.health.data && <span className="text-sm text-muted-foreground">Release {d.health.data.release}</span>}
        {updated && (
          <span className="ml-auto flex items-center gap-2 text-sm text-muted-foreground">
            <span className={`size-2 rounded-full ${stale !== null ? "bg-warning" : "bg-success"}`} aria-hidden />
            Updated {clock(stale !== null ? new Date(stale).toISOString() : updated, now)}
          </span>
        )}
      </header>

      {(stale !== null || neverReached) && (
        <Alert variant="warning">
          <AlertTriangle />
          <AlertTitle>Can't reach Shakedown</AlertTitle>
          <AlertDescription>
            {stale !== null ? `Showing data from ${ago(new Date(stale).toISOString(), now)} ago.` : "Nothing to show until it answers."}
          </AlertDescription>
        </Alert>
      )}
      {configError && (
        <Alert variant="destructive">
          <CircleAlert />
          <AlertTitle>shakedown.yaml has an error</AlertTitle>
          <AlertDescription>
            <p className="font-mono text-xs wrap-anywhere whitespace-pre-wrap">{configError}</p>
            <p>
              {noConfig
                ? "Nothing to show until it loads."
                : config?.last_loaded_at
                  ? `Showing data from the config loaded at ${clock(config.last_loaded_at, now)}.`
                  : null}
            </p>
          </AlertDescription>
        </Alert>
      )}

      {!noConfig && !neverReached && (
        <Tabs value={tab} onValueChange={go} className="gap-4">
          <TabsList className="w-full sm:w-fit sm:self-start">
            {TABS.map((t) => <TabsTrigger key={t.id} value={t.id}>{t.label}</TabsTrigger>)}
          </TabsList>
          <main className="min-w-0">
            <TabsContent value="overview">
              <Overview d={d} now={now} onAllRuns={() => go("runs")} />
            </TabsContent>
            <TabsContent value="runs"><RunsView runs={d.runs.data} library={d.library.data} now={now} /></TabsContent>
            <TabsContent value="sources"><SourcesView network={d.network.data} activity={d.activity.data} now={now} /></TabsContent>
            <TabsContent value="library"><LibraryView library={d.library.data} attention={d.attention.data} now={now} /></TabsContent>
          </main>
        </Tabs>
      )}
    </div>
  )
}
