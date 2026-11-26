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
import { SettingsView } from "@/views/Settings"
import { SetupView } from "@/views/Setup"
import { Notes } from "@/dash/actions"

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "runs", label: "Runs" },
  { id: "sources", label: "Sources" },
  { id: "library", label: "Library" },
  { id: "settings", label: "Settings" },
] as const
type TabId = (typeof TABS)[number]["id"]

function tabFromHash(): TabId {
  // "#settings/<source>/<collection>" opens a page inside a tab.
  const h = location.hash.slice(1).split("/")[0]
  return TABS.some((t) => t.id === h) ? (h as TabId) : "overview"
}

export function App() {
  const d = useDashboard()
  const nowMs = useNow()
  const now = new Date(nowMs).toISOString()
  const [tab, setTab] = useState<TabId>(tabFromHash)
  useEffect(() => {
    const on = () => setTab(tabFromHash())
    // Back/forward: hashchange for typed hashes, popstate for entries the tabs pushed.
    window.addEventListener("hashchange", on)
    window.addEventListener("popstate", on)
    return () => {
      window.removeEventListener("hashchange", on)
      window.removeEventListener("popstate", on)
    }
  }, [])
  const go = (t: string) => {
    // One history entry per tab change (the tab control reports a click twice).
    if (location.hash !== `#${t}`) history.pushState(null, "", `#${t}`)
    setTab(t as TabId)
    window.scrollTo(0, 0)
  }

  const slots = [d.activity, d.network, d.runs, d.library, d.attention, d.health]
  // 503: serve has no valid config. With no data yet the page shows nothing; with data
  // from before (serve restarted with a broken config) it keeps that data.
  const configDown = slots.find((s) => s.noConfig) ?? null
  const noConfig = configDown !== null && slots.every((s) => s.data === null)
  // With no config loaded, the config resource says why (§spec:first-run-setup,
  // §spec:dashboard-settings "A broken file"): no file leads to setup, a file with
  // errors opens in Settings, and a file Shakedown can't read is fixed by hand.
  const fileState = noConfig ? d.config.data?.state ?? null : null
  const setup = fileState === "missing"
  const fixHere = fileState === "invalid"
  const asking = noConfig && d.config.data === null && d.config.error === null
  const refreshAll = () => [...slots, d.config].forEach((s) => s.refresh?.())
  const neverReached = !noConfig && slots.every((s) => s.data === null && s.error !== null)
  const stale = staleSince(d, nowMs)
  const config = d.health.data?.config
  const configError = setup || fixHere || asking ? null : configDown ? configDown.error : config && !config.valid ? config.error : null
  const updated = d.activity.data?.generated_at ?? null

  return (
    <Notes>
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
            <p className="wrap-anywhere">
              {fileState === "unreadable"
                ? `Shakedown can't read the file, so the dashboard can't change it. Fix ${d.config.data!.path} by hand. Nothing to show until it loads.`
                : noConfig
                ? "Nothing to show until it loads."
                : config?.last_loaded_at
                  ? `Showing data from the config loaded at ${clock(config.last_loaded_at, now)}.`
                  : null}
            </p>
          </AlertDescription>
        </Alert>
      )}

      {setup && d.config.data && (
        <main className="min-w-0">
          <SetupView config={d.config.data} onDone={(hash) => { history.replaceState(null, "", hash ? `#${hash}` : location.pathname + location.search); setTab(tabFromHash()); refreshAll(); window.scrollTo(0, 0) }} />
        </main>
      )}
      {fixHere && (
        <>
          <Alert variant="destructive">
            <CircleAlert />
            <AlertTitle>Shakedown can't load shakedown.yaml</AlertTitle>
            <AlertDescription>
              <p>Nothing runs until it loads. Fix the settings marked below, or edit the file by hand. The other tabs open once it loads.</p>
            </AlertDescription>
          </Alert>
          <main className="min-w-0">
            <SettingsView config={d.config.data} library={null} now={now} onChanged={refreshAll} />
          </main>
        </>
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
            <TabsContent value="settings">
              <SettingsView config={d.config.data} library={d.library.data} now={now} onChanged={refreshAll} />
            </TabsContent>
          </main>
        </Tabs>
      )}
    </div>
    </Notes>
  )
}
