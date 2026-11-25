// First-run setup (§spec:first-run-setup): shown while there is no shakedown.yaml.
// Folders with the path checks (a failure warns, never blocks), one source with one
// collection, and its schedule; finishing writes the file and starts the first sync.
// It asks for no login (§spec:dashboard-credentials). More is added in Settings.
import { useEffect, useRef, useState } from "react"
import { CircleAlert, CircleCheck } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import type { ConfigAnswer, ConfigDocument, ConfigProblem, ConfigWarning, Loc, SetupAnswer, SetupDone } from "@/api/types"
import { RowsSkeleton } from "@/dash/bits"
import { refusal, send, useNote, type Note } from "@/dash/actions"
import { problemWords, WARNING } from "@/lib/words"
import { DraftCtx, getIn, locKey, setIn, type Draft } from "@/settings/draft"
import { TextSetting } from "@/settings/fields"
import { ScheduleSetting, SearchSetting } from "@/settings/Collection"

type Check = { kind: "checking" } | { kind: "done"; warnings: ConfigWarning[] } | { kind: "failed" }

const SOURCE: Loc = ["sources", 0]
const COLLECTION: Loc = ["sources", 0, "collections", 0]
const at = (...k: (string | number)[]): Loc => [...COLLECTION, ...k]
/** Settings this page shows a field for; a problem anywhere else is listed at the top. */
const SHOWN = ["archive_root", "library_root", "sources.0.name", "sources.0.type", ...["name", "query", "schedule"].map((k) => locKey(at(k)))]
const REQUIRED: Loc[] = [[...SOURCE, "type"], [...SOURCE, "name"], at("name"), at("query")]
const where = (loc: Loc) => (loc.length === 0 ? "shakedown.yaml" : loc.map((k) => (typeof k === "number" ? `[${k}]` : k)).join(" › "))

/** The browser's time zone: the schedule's clock until it's changed in Settings. */
const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || null

/** Only what setup sets; the file leaves every other key at its default
 *  (§spec:first-run-setup "The written file"). */
function firstDocument(s: SetupAnswer): ConfigDocument {
  // One plugin is chosen for the user; with several, they pick (no ranking of our own).
  const type = s.source_types.length === 1 ? s.source_types[0].type : ""
  return {
    archive_root: s.archive_root,
    library_root: s.library_root,
    timezone: browserZone(),
    sources: [{ name: type, type, collections: [{ name: "", query: "", schedule: s.schedule }] }],
  } as unknown as ConfigDocument
}

function FolderCheck({ check, onAgain }: { check: Check | null; onAgain: () => void }) {
  if (check === null || check.kind === "checking") {
    return <p role="status" className="text-sm text-muted-foreground">Checking the folders…</p>
  }
  if (check.kind === "failed") {
    return (
      <div role="status" className="flex flex-col items-start gap-2">
        <p className="text-sm text-warning">Shakedown couldn't check the folders. You can still finish setup.</p>
        <Button size="sm" variant="outline" onClick={onAgain}>Check again</Button>
      </div>
    )
  }
  if (check.warnings.length > 0) {
    return <p role="status" className="text-sm text-muted-foreground">You can still finish setup, and change the folders later in Settings.</p>
  }
  return (
    <p role="status" className="flex items-center gap-2 text-sm">
      <CircleCheck className="size-4 shrink-0 text-success" aria-hidden />
      Both folders work, and library files can link to the archive.
    </p>
  )
}

export function SetupView({ config, onDone }: {
  config: ConfigAnswer
  /** Setup wrote the file (or found one): reload everything and leave setup. */
  onDone: (hash: string) => void
}) {
  const note = useNote()
  const [defaults, setDefaults] = useState<SetupAnswer | null>(null)
  const [doc, setDoc] = useState<ConfigDocument | null>(null)
  const [check, setCheck] = useState<Check | null>(null)
  const [errors, setErrors] = useState<ConfigProblem[]>([])
  const [busy, setBusy] = useState(false)
  const [nameTouched, setNameTouched] = useState(false)
  const checked = useRef("")
  const asked = useRef(0)

  useEffect(() => {
    let live = true
    void fetch("./api/setup", { cache: "no-store" })
      .then((r) => (r.ok ? (r.json() as Promise<SetupAnswer>) : null))
      .catch(() => null)
      .then((s) => {
        if (!live || !s) return
        setDefaults(s)
        setDoc(firstDocument(s))
      })
    return () => { live = false }
  }, [])

  // The path checks run on opening and whenever a folder changes; they create the
  // folders when missing, as setup would anyway (§spec:first-run-setup).
  const runCheck = async (d: ConfigDocument) => {
    const key = `${d.archive_root}\n${d.library_root}`
    checked.current = key
    const n = ++asked.current
    setCheck({ kind: "checking" })
    const a = await send<{ warnings?: ConfigWarning[] }>("POST", "api/config/check-paths", {
      archive_root: d.archive_root, library_root: d.library_root,
    })
    if (n !== asked.current) return
    setCheck(a.status === 200 && a.body?.warnings ? { kind: "done", warnings: a.body.warnings } : { kind: "failed" })
  }
  // Once, when the defaults arrive.
  const opened = doc !== null
  useEffect(() => {
    if (opened) void runCheck(doc)
  }, [opened])

  if (!defaults || !doc) return <Card className="w-full max-w-2xl"><CardContent><RowsSkeleton rows={6} /></CardContent></Card>

  const byLoc = new Map(errors.map((e) => [locKey(e.loc), problemWords(e.code, e.loc)]))
  const warnings = check?.kind === "done" ? check.warnings : []
  const draft: Draft = {
    doc,
    editable: true,
    set: (path, value) => {
      setDoc((d) => setIn(d!, path, value))
      setErrors((all) => all.filter((e) => locKey(e.loc) !== locKey(path)))
      if (locKey(path) === locKey([...SOURCE, "name"])) setNameTouched(true)
    },
    problem: (path) => byLoc.get(locKey(path)) ?? null,
    warnings: (path) => warnings.filter((w) => locKey(w.loc) === locKey(path)).map((w) => WARNING[w.code] ?? w.code),
  }
  const source = doc.sources[0]
  const type = defaults.source_types.find((t) => t.type === source.type)
  const elsewhere = errors.filter((e) => !SHOWN.includes(locKey(e.loc)))
  const typeProblem = draft.problem([...SOURCE, "type"])

  const chooseType = (t: string) => {
    let next = setIn(doc, [...SOURCE, "type"], t)
    // The name follows the type until someone types one.
    if (!nameTouched) next = setIn(next, [...SOURCE, "name"], t)
    setDoc(next)
    setErrors((all) => all.filter((e) => !["type", "name"].some((k) => locKey(e.loc) === locKey([...SOURCE, k]))))
  }

  const finish = async () => {
    // With no source chosen, its name (which follows the choice) isn't marked too.
    const missing = REQUIRED.filter((p) => String(getIn(doc, p) ?? "").trim() === "")
      .filter((p) => !(doc.sources[0].type === "" && locKey(p) === locKey([...SOURCE, "name"])))
    if (missing.length > 0) {
      setErrors(missing.map((loc) => ({ loc, code: "missing", message: "" })))
      note({ tone: "danger", title: "Setup isn't finished", text: "Fill in the settings marked in red." })
      return
    }
    setBusy(true)
    const a = await send<Partial<SetupDone> & { errors?: ConfigProblem[]; code?: string; path?: string }>(
      "POST", "api/setup", { document: doc })
    setBusy(false)
    const b = a.body ?? {}
    const name = source.collections[0].name
    if (a.status === 201) {
      onDone("")
      note(b.started
        ? { tone: "success", title: "Shakedown is set up", text: `The first sync of ${name} has started. It shows under Running now.` }
        : { tone: "warning", title: "Shakedown is set up", text: b.reason === "busy"
            ? `The first sync of ${name} didn't start: another run is going. Press Sync now when it ends.`
            : `The first sync of ${name} didn't start. Press Sync now to try again.` })
      return
    }
    let n: Note
    if (a.status === 422 && b.errors) {
      setErrors(b.errors)
      n = { tone: "danger", title: "Setup isn't finished", text: "Fix the settings marked in red, then finish again." }
    } else if (a.status === 409 && b.code === "exists") {
      onDone("settings")
      n = { tone: "warning", title: "shakedown.yaml already exists", text: "It was made while you were setting up, so nothing was written. Here are its settings." }
    } else if (a.status === 500 && b.code === "write_failed" && b.path && b.path !== config.path) {
      // The file was written; the state database that records it failed.
      onDone("settings")
      n = { tone: "danger", title: "Shakedown couldn't write its database", text: `Check that ${b.path} isn't read-only. shakedown.yaml was written; the first sync didn't start.` }
    } else if (a.status === 500 && b.code === "write_failed") {
      n = { tone: "danger", title: "Shakedown couldn't write the file", text: `Check that ${b.path ?? config.path} isn't read-only. Nothing was set up.` }
    } else n = refusal(a)
    note(n)
  }

  return (
    <DraftCtx.Provider value={draft}>
      <div className="flex w-full max-w-2xl flex-col gap-4">
        <div className="flex flex-col gap-1">
          <h2 className="text-lg font-semibold">Set up Shakedown</h2>
          <p className="text-sm text-muted-foreground">
            There's no shakedown.yaml yet. Choose where shows go and a first collection to mirror. Shakedown writes the file and starts the first sync.
          </p>
        </div>
        {elsewhere.length > 0 && (
          <Alert variant="destructive">
            <CircleAlert />
            <AlertTitle>{elsewhere.length === 1 ? "Setup has an error" : `Setup has ${elsewhere.length} errors`}</AlertTitle>
            <AlertDescription>
              <ul className="flex flex-col gap-1">
                {elsewhere.map((e, i) => (
                  <li key={i}><span className="font-mono text-xs wrap-anywhere">{where(e.loc)}</span>: {problemWords(e.code, e.loc)}</li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>Folders</CardTitle>
            <CardDescription>Paths inside the Shakedown container. The defaults suit the example setup.</CardDescription>
          </CardHeader>
          <CardContent>
            <FieldGroup>
              <div
                className="contents"
                onBlur={() => {
                  if (`${doc.archive_root}\n${doc.library_root}` !== checked.current) void runCheck(doc)
                }}
              >
                <TextSetting path={["archive_root"]} label="Archive folder" help="Where Shakedown keeps every downloaded file." />
                <TextSetting path={["library_root"]} label="Library folder" help="The folders your music tools read." />
              </div>
              <FolderCheck check={check} onAgain={() => void runCheck(doc)} />
            </FieldGroup>
          </CardContent>
        </Card>
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>What to mirror</CardTitle>
            <CardDescription>One collection to start. Add more in Settings.</CardDescription>
          </CardHeader>
          <CardContent>
            <FieldGroup>
              <Field data-invalid={typeProblem ? true : undefined}>
                <FieldLabel htmlFor="setup-type">Source</FieldLabel>
                <Select value={source.type || undefined} onValueChange={chooseType}>
                  <SelectTrigger
                    id="setup-type"
                    className="w-full"
                    aria-invalid={typeProblem ? true : undefined}
                    aria-describedby={typeProblem ? "setup-type-help setup-type-error" : "setup-type-help"}
                  >
                    <SelectValue placeholder="Choose a source" />
                  </SelectTrigger>
                  <SelectContent>
                    {defaults.source_types.map((t) => <SelectItem key={t.type} value={t.type}>{t.type}</SelectItem>)}
                  </SelectContent>
                </Select>
                <FieldDescription id="setup-type-help">The plugins installed in this Shakedown.</FieldDescription>
                {typeProblem && <FieldError id="setup-type-error">{typeProblem}</FieldError>}
              </Field>
              <TextSetting path={[...SOURCE, "name"]} label="Source name" help="Fixed once saved: shows are filed under it." />
              <TextSetting path={at("name")} label="Collection name" help="Fixed once saved: its shows are filed under it." />
              <SearchSetting key={source.type} path={at("query")} type={source.type} readsLinks={type?.reads_links ?? false} finishing />
            </FieldGroup>
          </CardContent>
        </Card>
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>Schedule</CardTitle>
          </CardHeader>
          <CardContent>
            <ScheduleSetting
              path={at("schedule")}
              saved={undefined}
              next={undefined}
              now={defaults.generated_at}
              timezone={doc.timezone}
              clockNote={doc.timezone ? `Times follow ${doc.timezone}, this browser's time zone. Change it in Settings.` : undefined}
              setup
            />
          </CardContent>
        </Card>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
          <Button className="w-full sm:w-fit" onClick={finish} disabled={busy}>{busy ? "Setting up…" : "Finish setup"}</Button>
          <p className="text-sm text-muted-foreground wrap-anywhere">Writes {config.path}. Change anything later in Settings.</p>
        </div>
      </div>
    </DraftCtx.Provider>
  )
}
