// Settings tab (§spec:dashboard-settings): every setting in shakedown.yaml,
// saved whole under the file's revision. While the file is locked
// (§spec:edit-lock) the settings show as text, with the override offered.
import { useEffect, useMemo, useRef, useState } from "react"
import { CircleAlert, Lock } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button, buttonVariants } from "@/components/ui/button"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import type { CollectionConfig, ConfigAnswer, ConfigDocument, ConfigProblem, ConfigWarning, LibraryAnswer, Loc } from "@/api/types"
import { RowsSkeleton } from "@/dash/bits"
import { refusal, send, useNote } from "@/dash/actions"
import { problemWords, WARNING } from "@/lib/words"
import { staggeredDefault } from "@/lib/schedule"
import { CollectionEditor, type RemoveScope } from "@/settings/Collection"
import { AddSource } from "@/settings/AddSource"
import { collectionHref, useSettingsRoute } from "@/settings/route"
import { DraftCtx, locKey, setIn, type Draft } from "@/settings/draft"
import { FailureNoticeCard, FoldersCard, RunsCard } from "@/settings/General"
import { RemovedCard } from "@/settings/Removed"
import { SourceCard } from "@/settings/Sources"
import { Card, CardContent } from "@/components/ui/card"

/** Settings a field on this page shows; a problem anywhere else is listed at the top. */
function shownHere(loc: Loc, doc: ConfigDocument): boolean {
  const [k, i, sub, j] = loc
  if (loc.length === 1 && GLOBAL.includes(String(k))) return true
  if (k === "notifications") return true
  if (k === "sources" && typeof i === "number" && doc.sources[i]) {
    if (loc.length === 3 && sub === "max_concurrent_requests") return true
    if (sub === "collections" && typeof j === "number") return true // on the collection's row and in its editor
  }
  return false
}
const GLOBAL = ["archive_root", "library_root", "state_db", "max_concurrent_downloads", "max_concurrent_collections", "keep_run_errors_days", "timezone"]

const where = (loc: Loc) => (loc.length === 0 ? "shakedown.yaml" : loc.map((k) => (typeof k === "number" ? `[${k}]` : k)).join(" › "))

function Override({ onDone }: { onDone: () => void }) {
  const note = useNote()
  const [open, setOpen] = useState(false)
  const go = async () => {
    const a = await send<{ edit?: string; code?: string }>("POST", "api/config/override")
    if (a.status === 200) {
      note({ tone: "success", title: "Dashboard edits allowed" })
      onDone()
    } else note(refusal(a))
  }
  return (
    <AlertDialog open={open} onOpenChange={setOpen}>
      <Button size="sm" variant="outline" className="mt-2 w-fit text-foreground" onClick={() => setOpen(true)}>Allow dashboard edits</Button>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Allow dashboard edits?</AlertDialogTitle>
          <AlertDialogDescription>
            If shakedown.yaml is later changed where it came from, by hand or by a deploy, that version replaces what you change here. This stays on: the dashboard can't lock the file again.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction className={buttonVariants()} onClick={go}>Allow dashboard edits</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function SaveBar({ problems, saving, onDiscard, onSave }: { problems: number; saving: boolean; onDiscard: () => void; onSave: () => void }) {
  useEffect(() => {
    document.body.dataset.saveBar = ""
    return () => { delete document.body.dataset.saveBar }
  }, [])
  return (
    <div className="fixed inset-x-0 bottom-0 z-10 border-t bg-background">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 sm:px-6">
        <span className={`text-sm ${problems > 0 ? "text-destructive" : "text-muted-foreground"}`} role="status">
          {problems > 0 ? (problems === 1 ? "1 setting needs fixing before it saves" : `${problems} settings need fixing before they save`) : "Unsaved changes"}
        </span>
        <div className="ml-auto flex gap-2">
          <Button variant="outline" onClick={onDiscard} disabled={saving}>Discard</Button>
          <Button onClick={onSave} disabled={saving}>{saving ? "Saving…" : "Save"}</Button>
        </div>
      </div>
    </div>
  )
}

const newCollection = (name: string, schedule: string): CollectionConfig => ({
  name, query: "", library_layout: "passthrough", prune_disappeared: false, incremental_discovery: false,
  preserve_source_metadata: false, fill_tags: false, on_complete: null, schedule,
})

type Fresh = { source: string; j: number }

export function SettingsView({ config, library, now, onChanged }: {
  config: ConfigAnswer | null; library: LibraryAnswer | null; now: string; onChanged: () => void
}) {
  const note = useNote()
  const route = useSettingsRoute()
  // The collection being added (route "?add"), held in the draft until it saves.
  const [fresh, setFresh] = useState<Fresh | null>(null)
  const [doc, setDoc] = useState<ConfigDocument | null>(config?.document ?? null)
  const [revision, setRevision] = useState<string | null>(config?.revision ?? null)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [errors, setErrors] = useState<ConfigProblem[] | null>(null)
  const [warnings, setWarnings] = useState<ConfigWarning[]>([])
  const [conflict, setConflict] = useState(false)

  // A fresh answer replaces what is shown unless the user is mid-edit; an edit made
  // meanwhile is caught by the revision at save (409 changed).
  // Only a new revision of the file replaces it, so a save's values stay up until
  // the refresh that follows it brings the file back.
  const applied = useRef<string | null | undefined>(config?.revision)
  useEffect(() => {
    if (!config || dirty || config.revision === applied.current) return
    applied.current = config.revision
    setDoc(config.document)
    setRevision(config.revision)
    setErrors(null)
  }, [config, dirty])

  // Opening "add" puts a new collection with the staggered schedule into the draft.
  useEffect(() => {
    if (route.page !== "new" || !doc || config?.edit === "locked") return
    if (fresh && fresh.source === route.source) return
    const i = doc.sources.findIndex((s) => s.name === route.source)
    if (i < 0) return
    const all = doc.sources.flatMap((s) => s.collections.map((c) => c.schedule))
    const c = newCollection(route.name, staggeredDefault(all))
    let d = doc
    if (fresh) { // one new collection at a time: one left on another source goes
      const k = d.sources.findIndex((s) => s.name === fresh.source)
      if (k >= 0) d = setIn(d, ["sources", k, "collections"], d.sources[k].collections.filter((_, n) => n !== fresh.j))
    }
    setDoc(setIn(d, ["sources", i, "collections"], [...d.sources[i].collections, c]))
    setFresh({ source: route.source, j: d.sources[i].collections.length })
    setDirty(true)
  }, [route, doc, fresh, config?.edit])

  const shownErrors = errors ?? config?.errors ?? []
  const byLoc = useMemo(() => {
    const m = new Map<string, string>()
    for (const e of shownErrors) if (!m.has(locKey(e.loc))) m.set(locKey(e.loc), problemWords(e.code, e.loc))
    return m
  }, [shownErrors])
  const warnByLoc = useMemo(() => {
    const m = new Map<string, string[]>()
    for (const w of warnings) m.set(locKey(w.loc), [...(m.get(locKey(w.loc)) ?? []), WARNING[w.code] ?? "Check this setting."])
    return m
  }, [warnings])

  if (!config || !doc) return <Card><CardContent><RowsSkeleton rows={6} /></CardContent></Card>

  const editable = config.edit !== "locked"
  const draft: Draft = {
    doc,
    editable,
    set: (path, value) => { setDoc((d) => setIn(d!, path, value)); setDirty(true); setWarnings([]) },
    problem: (path) => byLoc.get(locKey(path)) ?? null,
    warnings: (path) => warnByLoc.get(locKey(path)) ?? [],
  }
  const elsewhere = shownErrors.filter((e) => !shownHere(e.loc, doc))
  const execUsesStaging = doc.sources.some((s) => s.collections.some((c) => c.on_complete && "exec" in c.on_complete && c.on_complete.exec.includes("{staging_root}")))

  const discard = () => {
    applied.current = config.revision; setDoc(config.document); setRevision(config.revision); setDirty(false); setErrors(null); setWarnings([])
    setFresh(null)
    if (route.page === "new") location.hash = "settings"
  }
  /** Writes a document: its warnings when it saved, null when refused. Every refusal is told here. */
  const put = async (next: ConfigDocument): Promise<ConfigWarning[] | null> => {
    setSaving(true)
    const a = await send<{ revision?: string; warnings?: ConfigWarning[]; errors?: ConfigProblem[]; code?: string; path?: string }>(
      "PUT", "api/config", { revision, document: next })
    setSaving(false)
    const b = a.body ?? {}
    if (a.status === 200) {
      setDoc(next)
      setRevision(b.revision ?? null)
      setDirty(false)
      setErrors([])
      setWarnings(b.warnings ?? [])
      setFresh(null)
      onChanged()
      return b.warnings ?? []
    }
    if (a.status === 422 && b.errors) {
      setErrors(b.errors)
      note({ tone: "danger", title: "Nothing saved", text: "Fix the settings marked in red, then save again." })
    } else if (a.status === 409 && b.code === "changed") setConflict(true)
    else if (a.status === 409 && b.code === "locked") { note({ tone: "warning", title: "Settings are read-only now", text: "Nothing saved." }); onChanged() }
    else if (a.status === 409 && b.code === "busy")
      note({ tone: "warning", title: "A run is in progress", text: "Shakedown can't switch databases during a run. Save again when it ends." })
    else if (a.status === 500 && b.code === "write_failed")
      note({ tone: "danger", title: "Shakedown couldn't write the file", text: `Check that ${b.path ?? config.path} isn't read-only. Nothing changed.` })
    else note(refusal(a))
    return null
  }
  const save = async () => {
    const added = fresh && route.page === "new" ? doc.sources.find((s) => s.name === fresh.source)?.collections[fresh.j] : undefined
    const w = await put(doc)
    if (w === null) return
    note(w.length > 0
      ? { tone: "warning", title: "Saved, with warnings", text: "Each one is under its setting." }
      : { tone: "success", title: "Settings saved", text: "Runs use them from their next start." })
    if (added && fresh) location.replace(collectionHref(fresh.source, added.name))
  }
  // Remove = save the file without it, then start the delete chosen
  // (§spec:collection-removal "Remove and delete in one go").
  const remove = async (source: string, name: string, scope: RemoveScope) => {
    const i = doc.sources.findIndex((s) => s.name === source)
    const w = await put(setIn(doc, ["sources", i, "collections"], doc.sources[i].collections.filter((c) => c.name !== name)))
    if (w === null) return
    location.hash = "settings"
    if (scope === "keep") {
      note({ tone: "success", title: `Removed ${name}`, text: "Its files stay, listed under Removed collections." })
      return
    }
    const a = await send<{ code?: string; detail?: string }>("POST", "api/collections/purge", { source, collection: name, scope })
    if (a.status === 202) note({ tone: "success", title: `Removed ${name}`, text: "Deleting its files. It shows under Running now." })
    else if (a.status === 409 && !a.body?.code)
      note({ tone: "warning", title: `Removed ${name}, files kept`, text: "A run is in progress, so nothing was deleted. Delete them under Removed collections when it ends." })
    else note({ tone: "warning", title: `Removed ${name}, files kept`, text: `${refusal(a).title}. Delete them under Removed collections.` })
    onChanged()
  }

  const at = route.page === "list" ? null : (() => {
    const i = doc.sources.findIndex((s) => s.name === route.source)
    if (i < 0) return null
    const j = route.page === "new"
      ? (fresh?.source === route.source ? fresh.j : -1)
      : doc.sources[i].collections.findIndex((c, n) => c.name === route.collection && !(fresh?.source === route.source && fresh.j === n))
    return j < 0 ? null : { i, j }
  })()

  const page = route.page === "list" ? null : at ? (
    <CollectionEditor
      key={`${at.i}.${at.j}`}
      config={config}
      library={library}
      now={now}
      i={at.i}
      j={at.j}
      fresh={route.page === "new"}
      otherChanges={dirty}
      problems={shownErrors}
      onRemove={(scope) => remove(doc.sources[at.i].name, doc.sources[at.i].collections[at.j].name, scope)}
    />
  ) : (
    <Card>
      <CardContent className="flex flex-col items-start gap-2">
        <p>{route.page === "new" && !editable
          ? "Settings are read-only, so collections can't be added here."
          : route.page === "new" ? `There's no source called ${route.source}.` : `There's no collection called ${route.collection} in ${route.source}.`}</p>
        <a href="#settings" className="text-sm text-primary underline-offset-4 hover:underline">Back to Settings</a>
      </CardContent>
    </Card>
  )

  return (
    <DraftCtx.Provider value={draft}>
      <div className={`flex flex-col gap-4 ${dirty ? "pb-20" : ""}`}>
        {page ?? <>
        <p className="text-sm text-muted-foreground wrap-anywhere">
          Saved to {config.path}. Runs use changes from their next start.
        </p>
        {config.edit === "locked" && (
          <Alert>
            <Lock />
            <AlertTitle>Settings are read-only</AlertTitle>
            <AlertDescription>
              <p>shakedown.yaml was written outside Shakedown, so the dashboard doesn't change it. Sync now, Validate, Trim history now, and deleting removed collections' files still work.</p>
              <Override onDone={onChanged} />
            </AlertDescription>
          </Alert>
        )}
        {elsewhere.length > 0 && (
          <Alert variant="destructive">
            <CircleAlert />
            <AlertTitle>{elsewhere.length === 1 ? "shakedown.yaml has an error" : `shakedown.yaml has ${elsewhere.length} errors`}</AlertTitle>
            <AlertDescription>
              <ul className="flex flex-col gap-1">
                {elsewhere.map((e, i) => (
                  <li key={i}><span className="font-mono text-xs wrap-anywhere">{where(e.loc)}</span>: {problemWords(e.code, e.loc)}</li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}
        <div className="grid gap-4 lg:grid-cols-2">
          <div className="flex min-w-0 flex-col gap-4">
            <FoldersCard execUsesStaging={execUsesStaging} />
            <FailureNoticeCard />
          </div>
          <RunsCard />
        </div>
        <div className="mt-2 flex items-center gap-4">
          <h2 className="text-base font-semibold">Sources</h2>
          {editable && <AddSource types={config.source_types.map((t) => t.type)} />}
        </div>
        {doc.sources.map((s, i) => (
          <SourceCard key={i} index={i} source={s} config={config} now={now} fresh={fresh?.source === s.name ? fresh.j : null} />
        ))}
        <RemovedCard removed={config.unconfigured} onDone={onChanged} />
        </>}
      </div>
      {dirty && editable && <SaveBar problems={errors?.length ?? 0} saving={saving} onDiscard={discard} onSave={save} />}
      <AlertDialog open={conflict} onOpenChange={setConflict}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>shakedown.yaml changed</AlertDialogTitle>
            <AlertDialogDescription>
              Someone changed the file after you opened Settings, so nothing was saved. Reload to see their version, then make your changes again.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Not now</AlertDialogCancel>
            <AlertDialogAction className={buttonVariants()} onClick={() => { discard(); onChanged() }}>Reload settings</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </DraftCtx.Provider>
  )
}
