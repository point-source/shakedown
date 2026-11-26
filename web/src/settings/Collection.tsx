// The collection editor (§spec:dashboard-settings "collections with every option"):
// what it mirrors, when it syncs (§spec:schedules "Presets and cron"), how its
// library is laid out, and who hears about new shows. It edits the same draft as
// the Settings list, so the save bar saves both. Names are fixed once saved.
import { useEffect, useRef, useState } from "react"
import { ChevronLeft } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button, buttonVariants } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { CircleAlert, Lock } from "lucide-react"
import type { ConfigAnswer, ConfigProblem, LibraryAnswer, Loc } from "@/api/types"
import { bytes, clock, plural } from "@/lib/format"
import { DAYS, hhmm, readSchedule, scheduleLine, writeSchedule, type Schedule } from "@/lib/schedule"
import { problemWords } from "@/lib/words"
import { isLink, readLink, type LinkReading } from "@/lib/sourceLink"
import { getIn, locKey, useDraft } from "@/settings/draft"
import { SwitchSetting, TextSetting } from "@/settings/fields"

export type RemoveScope = "keep" | "library" | "everything"

const KINDS: { value: Schedule["kind"]; label: string }[] = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "manual", label: "Manual only" },
  { value: "cron", label: "Custom" },
]

/** A 24-hour time, as the rest of the page writes times; held as typed until it reads as one. */
function TimeInput({ id, hour, minute, onTime }: { id: string; hour: number; minute: number; onTime: (h: number, m: number) => void }) {
  const [text, setText] = useState(hhmm(hour, minute))
  // Discard or a reload changes the time from outside: show it. While typing, the
  // text already reads as the time it set, or as none.
  useEffect(() => {
    setText((t) => {
      const m = /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(t)
      return m && Number(m[1]) === hour && Number(m[2]) === minute ? t : hhmm(hour, minute)
    })
  }, [hour, minute])
  const bad = !/^([01]?\d|2[0-3]):[0-5]\d$/.test(text)
  return (
    <Field data-invalid={bad ? true : undefined}>
      <FieldLabel htmlFor={id}>Time</FieldLabel>
      <Input
        id={id}
        inputMode="numeric"
        className="tabular-nums"
        value={text}
        aria-invalid={bad ? true : undefined}
        aria-describedby={bad ? `${id}-error` : undefined}
        onChange={(e) => {
          const v = e.target.value
          setText(v)
          const m = /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(v)
          if (m) onTime(Number(m[1]), Number(m[2]))
        }}
      />
      {bad && <FieldError id={`${id}-error`}>Enter a 24-hour time, such as 03:30.</FieldError>}
    </Field>
  )
}

export function ScheduleSetting({ path, saved, next, now, timezone, clockNote: clockText, setup }: {
  path: Loc; saved: string | null | undefined; next: string | null | undefined; now: string; timezone: string | null
  /** Replaces "Times follow <tz>." (setup names where its zone came from). */
  clockNote?: string
  /** First-run setup: the first sync starts when setup finishes, then the schedule applies. */
  setup?: boolean
}) {
  const d = useDraft()
  const expr = (getIn(d.doc, path) as string | null | undefined) ?? null
  const read = readSchedule(expr)
  // Custom stays chosen while its expression happens to read as a preset.
  const [custom, setCustom] = useState(read.kind === "cron")
  const kind = custom ? "cron" : read.kind
  const id = `set-${locKey(path)}`
  const problem = d.problem(path)
  const at = read.kind === "daily" || read.kind === "weekly" ? { hour: read.hour, minute: read.minute } : { hour: 3, minute: 0 }
  const set = (s: Schedule) => d.set(path, writeSchedule(s))
  const clockNote = clockText ?? `Times follow ${timezone ?? "the container's clock"}.`
  const nextLine = setup
    ? kind === "manual"
      ? "The first sync starts when you finish. After that, it syncs only when someone presses Sync now."
      : "The first sync starts when you finish. After that, it follows this schedule."
    : kind === "manual"
      ? "Syncs only when someone presses Sync now."
      : expr === saved && next ? `Next sync ${clock(next, now)}` : "Next sync is worked out when you save."

  if (!d.editable) {
    return (
      <Field data-invalid={problem ? true : undefined}>
        <FieldLabel asChild><span>Syncs</span></FieldLabel>
        <p className="tabular-nums">{scheduleLine(expr)}</p>
        <FieldDescription>{nextLine}</FieldDescription>
        {problem && <FieldError>{problem}</FieldError>}
      </Field>
    )
  }

  const choose = (k: string) => {
    setCustom(k === "cron")
    if (k === "daily") set({ kind: "daily", ...at })
    if (k === "weekly") set({ kind: "weekly", day: read.kind === "weekly" ? read.day : 0, ...at })
    if (k === "manual") set({ kind: "manual" })
    if (k === "cron") set({ kind: "cron", expr: expr ?? "0 3 * * 0" })
  }
  const time = (hour: number, minute: number) => {
    const t = { hour, minute }
    if (read.kind === "weekly") set({ kind: "weekly", day: read.day, ...t })
    else set({ kind: "daily", ...t })
  }
  const notes = [kind !== "manual" && `${id}-help`, problem && `${id}-error`].filter(Boolean).join(" ") || undefined
  return (
    <FieldGroup>
      <div className="grid gap-4 sm:grid-cols-3">
        <Field>
          <FieldLabel htmlFor={`${id}-kind`}>Repeat</FieldLabel>
          <Select value={kind} onValueChange={choose}>
            <SelectTrigger id={`${id}-kind`} className="w-full" aria-describedby={notes}><SelectValue /></SelectTrigger>
            <SelectContent>
              {KINDS.map((k) => <SelectItem key={k.value} value={k.value}>{k.label}</SelectItem>)}
            </SelectContent>
          </Select>
        </Field>
        {kind === "weekly" && read.kind === "weekly" && (
          <Field>
            <FieldLabel htmlFor={`${id}-day`}>Day</FieldLabel>
            <Select value={String(read.day)} onValueChange={(v) => set({ ...read, day: Number(v) })}>
              <SelectTrigger id={`${id}-day`} className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                {DAYS.map((day, i) => <SelectItem key={day} value={String(i)}>{day}</SelectItem>)}
              </SelectContent>
            </Select>
          </Field>
        )}
        {(kind === "daily" || kind === "weekly") && (
          <TimeInput key={d.generation} id={`${id}-time`} hour={at.hour} minute={at.minute} onTime={time} />
        )}
        {kind === "cron" && (
          <Field className="sm:col-span-2" data-invalid={problem ? true : undefined}>
            <FieldLabel htmlFor={id}>Cron expression</FieldLabel>
            <Input
              id={id}
              className="font-mono"
              value={expr ?? ""}
              aria-invalid={problem ? true : undefined}
              aria-describedby={notes}
              onChange={(e) => d.set(path, e.target.value)}
            />
          </Field>
        )}
      </div>
      <div className="flex flex-col gap-1">
        {kind !== "manual" && (
          <FieldDescription id={`${id}-help`}>
            {kind === "cron" ? "Five fields: minute, hour, day of month, month, day of week. " : ""}{clockNote}
          </FieldDescription>
        )}
        {problem && <FieldError id={`${id}-error`}>{problem}</FieldError>}
        <p className="text-sm tabular-nums">{nextLine}</p>
      </div>
    </FieldGroup>
  )
}

function RemoveDialog({ name, shows, archiveBytes, otherChanges, onRemove }: {
  name: string; shows: number; archiveBytes: number; otherChanges: boolean; onRemove: (scope: RemoveScope) => Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [scope, setScope] = useState<RemoveScope>("keep")
  const [busy, setBusy] = useState(false)
  const held = shows > 0 ? `${plural(shows, "show", "shows")} (${bytes(archiveBytes)})` : "its shows"
  const go = async (e: React.MouseEvent) => {
    e.preventDefault()
    setBusy(true)
    await onRemove(scope)
    setBusy(false)
    setOpen(false)
  }
  const choices: { value: RemoveScope; label: string; text: string }[] = [
    { value: "keep", label: "Keep files", text: `${held} stay on disk, listed under Removed collections. Adding ${name} back reuses them.` },
    { value: "library", label: "Delete library folders", text: `The archive stays, so adding ${name} back rebuilds them with no download. Frees almost no space: library files are links into the archive.` },
    { value: "everything", label: "Delete library folders and archived shows", text: `Also deletes ${held} from the archive. Adding ${name} back downloads them all again.` },
  ]
  return (
    <AlertDialog open={open} onOpenChange={(o) => { setOpen(o); if (o) setScope("keep") }}>
      <Button variant="outline" className="w-fit text-destructive" onClick={() => setOpen(true)}>Remove collection…</Button>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Remove {name}?</AlertDialogTitle>
          <AlertDialogDescription>
            Shakedown stops syncing it. Deleted files can't be brought back.{otherChanges && " Your other unsaved changes are saved with it."}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <RadioGroup value={scope} onValueChange={(v) => setScope(v as RemoveScope)} className="gap-4">
          {choices.map((c) => (
            <div key={c.value} className="flex items-start gap-3">
              <RadioGroupItem value={c.value} id={`remove-${c.value}`} className="mt-0.5" />
              <div className="grid gap-1">
                <Label htmlFor={`remove-${c.value}`}>{c.label}</Label>
                <p className="text-sm text-muted-foreground">{c.text}</p>
              </div>
            </div>
          ))}
        </RadioGroup>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <AlertDialogAction className={buttonVariants({ variant: scope === "keep" ? "default" : "destructive" })} onClick={go} disabled={busy}>
            {busy ? "Removing…" : scope === "keep" ? "Remove, keep files" : scope === "library" ? "Remove, delete library folders" : "Remove, delete everything"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** A collection's search. A pasted link is sent to the source's plugin; its answer
 *  replaces the link with the search it stands for, and the field says so. An
 *  answer for a link that has since been edited away is dropped. */
export function SearchSetting({ path, type, readsLinks, finishing }: {
  path: Loc; type: string; readsLinks: boolean
  /** In setup, which finishes rather than saves. */
  finishing?: boolean
}) {
  const d = useDraft()
  const [link, setLink] = useState<LinkReading | null>(null)
  const typed = useRef("")
  // The draft's value now: a reading that comes back after Discard must not undo it.
  const current = useRef<unknown>(null)
  current.current = getIn(d.doc, path)
  // Leaving the editor drops a reading still on its way: the draft moved on without it.
  useEffect(() => () => { typed.current = "" }, [])
  const put = (v: unknown) => {
    const text = String(v ?? "")
    typed.current = text
    d.set(path, v ?? "")
    setLink(null)
    if (!readsLinks || !isLink(text)) return
    void readLink(type, text).then((r) => {
      if (typed.current !== text || current.current !== text) return
      if (r.kind === "read") d.set(path, r.query)
      setLink(r)
    })
  }
  return (
    <TextSetting
      path={path}
      multiline
      label="Search"
      put={put}
      help={link?.kind === "unreadable"
        ? <span className="text-warning">Shakedown can't read this link for this source. Paste a collection or search page from the source's site, or type the search.</span>
        : link?.kind === "failed"
          ? <span className="text-warning">Shakedown couldn't read this link just now. Paste it again, or type the search.</span>
          : link?.kind === "read"
            ? `Read from the link you pasted. Check it before you ${finishing ? "finish" : "save"}.`
            : readsLinks
              ? "Paste a collection or search link from the source's site, or type the search."
              : "The source's own search for the shows to mirror."}
    />
  )
}

/** The fields this page shows, under a collection's loc; a problem anywhere else under it is listed at the top. */
const FIELDS = ["name", "query", "library_layout", "schedule", "on_complete.webhook", "prune_disappeared", "incremental_discovery", "preserve_source_metadata", "fill_tags"]

export function CollectionEditor({ config, library, now, i, j, fresh, otherChanges, problems, onRemove }: {
  config: ConfigAnswer; library: LibraryAnswer | null; now: string; i: number; j: number
  /** Not saved yet: its name can still change. */
  fresh: boolean
  otherChanges: boolean
  problems: ConfigProblem[]
  onRemove: (scope: RemoveScope) => Promise<void>
}) {
  const d = useDraft()
  const source = d.doc.sources[i]
  const c = source.collections[j]
  const at = (...k: (string | number)[]): Loc => ["sources", i, "collections", j, ...k]
  const type = config.source_types.find((t) => t.type === source.type)
  const savedSource = config.document?.sources.find((s) => s.name === source.name)
  const saved = fresh ? undefined : savedSource?.collections.find((x) => x.name === c.name)
  const next = config.next_syncs.find((n) => n.source === source.name && n.collection === c.name)?.next_at
  const removed = config.unconfigured.find((u) => u.source === source.name && u.collection === c.name)
  const held = library?.collections.find((x) => x.source === source.name && x.collection === c.name)
  const shows = held ? held.counts.complete + held.counts.disappeared : 0
  const exec = c.on_complete && "exec" in c.on_complete ? c.on_complete.exec : null
  const webhook = c.on_complete && "webhook" in c.on_complete ? c.on_complete.webhook : null
  const readsLinks = type?.reads_links ?? false
  const prefix = locKey(at())
  const elsewhere = problems.filter((p) => {
    const k = locKey(p.loc)
    return k.startsWith(prefix + ".") || k === prefix ? !FIELDS.includes(k.slice(prefix.length + 1)) : false
  })

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <a href="#settings" className="-my-1 flex w-fit items-center gap-1 rounded-sm py-1 text-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50">
          <ChevronLeft className="size-4" aria-hidden /> Settings
        </a>
        <h2 className="text-lg font-semibold wrap-anywhere">{fresh ? "New collection" : c.name}</h2>
        <p className="text-sm text-muted-foreground wrap-anywhere">{fresh ? `In ${source.name}` : `${source.name} · type ${source.type}`}</p>
      </div>
      {!d.editable && (
        <Alert>
          <Lock />
          <AlertTitle>Settings are read-only</AlertTitle>
          <AlertDescription>
            shakedown.yaml was written outside Shakedown. To change this collection here, allow dashboard edits on the{" "}
            <a href="#settings" className="text-foreground underline underline-offset-4">Settings page</a>.
          </AlertDescription>
        </Alert>
      )}
      {elsewhere.length > 0 && (
        <Alert variant="destructive">
          <CircleAlert />
          <AlertTitle>{elsewhere.length === 1 ? "This collection has an error" : `This collection has ${elsewhere.length} errors`}</AlertTitle>
          <AlertDescription>
            <ul className="flex flex-col gap-1">
              {elsewhere.map((p, n) => (
                <li key={n}><span className="font-mono text-xs wrap-anywhere">{p.loc.slice(4).join(" › ")}</span>: {problemWords(p.code, p.loc)}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-4">
          <Card className="min-w-0">
            <CardHeader>
              <CardTitle>What to mirror</CardTitle>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                <TextSetting
                  path={at("name")}
                  label="Name"
                  readOnlyNote={fresh ? undefined : "Fixed once created: its shows are filed under it. To rename, remove the collection and add a new one."}
                  help={removed
                    ? `Shakedown still holds ${plural(removed.shows, "show", "shows")} (${bytes(removed.archive_bytes)}) for ${c.name}. They're reused: nothing downloads again.`
                    : "Fixed once saved: its shows are filed under it."}
                />
                <SearchSetting path={at("query")} type={source.type} readsLinks={readsLinks} />
              </FieldGroup>
            </CardContent>
          </Card>
          <Card className="min-w-0">
            <CardHeader>
              <CardTitle>Library folders</CardTitle>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                <TextSetting
                  path={at("library_layout")}
                  label="Folder layout"
                  help={
                    <>
                      Each show's folder, built from fields such as <code className="font-mono">{"{year}"}</code>. End it with{" "}
                      <code className="font-mono">{"{identifier}"}</code>: each recording needs its own folder.{" "}
                      <code className="font-mono">passthrough</code> copies the archive's folders as they are.
                      {type && (
                        <span className="mt-1 block">
                          Fields: <span className="font-mono">{type.layout_fields.join(", ")}</span>. Filters, as{" "}
                          <code className="font-mono">{"{venue|short}"}</code>: <span className="font-mono">{type.layout_filters.join(", ")}</span>.
                        </span>
                      )}
                    </>
                  }
                />
                <SwitchSetting
                  path={at("fill_tags")}
                  label="Fill in missing tags"
                  help="Adds missing artist, album, date, venue, and track title tags from the source. Tags already there are never changed, and the archive copy gets them too."
                />
                <SwitchSetting
                  path={at("preserve_source_metadata")}
                  label="Keep the source's notes"
                  help="Saves each show's notes from the source as metadata.json beside its files."
                />
              </FieldGroup>
            </CardContent>
          </Card>
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <Card className="min-w-0">
            <CardHeader>
              <CardTitle>Schedule</CardTitle>
            </CardHeader>
            <CardContent>
              <ScheduleSetting key={`${i}.${j}`} path={at("schedule")} saved={saved?.schedule} next={next} now={now} timezone={d.doc.timezone} />
            </CardContent>
          </Card>
          <Card className="min-w-0">
            <CardHeader>
              <CardTitle>Syncing</CardTitle>
            </CardHeader>
            <CardContent>
              <FieldGroup>
                <SwitchSetting
                  path={at("incremental_discovery")}
                  label="Quick change check"
                  help="Skips looking a show up again when the source says it hasn't changed. Faster on large collections."
                />
                <SwitchSetting
                  path={at("prune_disappeared")}
                  label="Delete shows the source removes"
                  help="When the source stops listing a show, its files are deleted too. Off, Shakedown keeps its copy and marks it Disappeared."
                />
              </FieldGroup>
            </CardContent>
          </Card>
          <Card className="min-w-0">
            <CardHeader>
              <CardTitle>Handoff notice</CardTitle>
              <CardDescription>Tells your music tools that new shows arrived.</CardDescription>
            </CardHeader>
            <CardContent>
              {exec !== null ? (
                <Field>
                  <FieldLabel asChild><span>Command</span></FieldLabel>
                  <p className="font-mono text-sm wrap-anywhere">{exec}</p>
                  <FieldDescription>Set in shakedown.yaml. The dashboard can't change a command.</FieldDescription>
                </Field>
              ) : (
                <TextSetting
                  path={at("on_complete", "webhook")}
                  label="Webhook URL"
                  blank={null}
                  placeholder="None"
                  help={<>Shakedown posts here after a sync with new shows. <code className="font-mono">{"{collection}"}</code> becomes the collection's name.</>}
                  get={() => webhook}
                  put={(v) => d.set(at("on_complete"), v === null ? null : { webhook: v })}
                />
              )}
            </CardContent>
          </Card>
        </div>
      </div>
      {d.editable && !fresh && (
        <Card className="min-w-0">
          <CardHeader>
            <CardTitle>Remove collection</CardTitle>
            <CardDescription>Stops syncing {c.name}. You choose what happens to its files.</CardDescription>
          </CardHeader>
          <CardContent>
            <RemoveDialog name={c.name} shows={shows} archiveBytes={held?.archive_bytes ?? 0} otherChanges={otherChanges} onRemove={onRemove} />
          </CardContent>
        </Card>
      )}
    </div>
  )
}
