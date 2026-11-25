// One setting: a labelled control while the file is editable, the same label
// and its value as text while it is locked (§spec:edit-lock "shows the settings
// without editing controls"). Errors and warnings sit under the setting they name.
import type { ReactNode } from "react"
import { Field, FieldContent, FieldDescription, FieldError, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Switch } from "@/components/ui/switch"
import type { Loc } from "@/api/types"
import { getIn, locKey, useDraft } from "@/settings/draft"

const idFor = (path: Loc) => `set-${locKey(path)}`

function Notes({ path, help }: { path: Loc; help?: ReactNode }) {
  const d = useDraft()
  const problem = d.problem(path)
  const warnings = d.warnings(path)
  const id = idFor(path)
  return (
    <>
      {help && <FieldDescription id={`${id}-help`}>{help}</FieldDescription>}
      {problem && <FieldError id={`${id}-error`}>{problem}</FieldError>}
      {warnings.length > 0 && (
        <div id={`${id}-warning`} className="flex flex-col gap-1 text-sm text-warning">
          {warnings.map((w) => <p key={w}>{w}</p>)}
        </div>
      )}
    </>
  )
}

function describedBy(path: Loc, help: boolean, d: ReturnType<typeof useDraft>) {
  const id = idFor(path)
  return [help && `${id}-help`, d.problem(path) && `${id}-error`, d.warnings(path).length > 0 && `${id}-warning`].filter(Boolean).join(" ") || undefined
}

/** A text or whole-number setting. `blank` is what an empty field saves (null for an optional key). */
export function TextSetting({ path, label, help, number, multiline, blank = "", placeholder, readOnlyNote, get, put }: {
  path: Loc; label: string; help?: ReactNode; number?: boolean; blank?: string | null; placeholder?: string
  /** A long value (a search): wraps instead of scrolling sideways. */
  multiline?: boolean
  /** Read and write somewhere other than `path` (a value whose parent may be absent); `path` still names its problems. */
  get?: () => string | number | null
  put?: (v: unknown) => void
  /** Shown instead of a control even while editable (a value the file alone sets). */
  readOnlyNote?: string
}) {
  const d = useDraft()
  const value = get ? get() : (getIn(d.doc, path) as string | number | null)
  const write = put ?? ((v: unknown) => d.set(path, v))
  const id = idFor(path)
  const shown = value === null || value === undefined || value === "" ? null : String(value)
  if (!d.editable || readOnlyNote) {
    return (
      <Field data-invalid={d.problem(path) ? true : undefined}>
        <FieldLabel asChild><span>{label}</span></FieldLabel>
        <p className="wrap-anywhere">{shown ?? <span className="text-muted-foreground">{placeholder ?? "Not set"}</span>}</p>
        <Notes path={path} help={readOnlyNote ?? help} />
      </Field>
    )
  }
  return (
    <Field data-invalid={d.problem(path) ? true : undefined}>
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      {multiline ? (
        <Textarea
          id={id}
          rows={2}
          value={shown ?? ""}
          placeholder={placeholder}
          aria-invalid={d.problem(path) ? true : undefined}
          aria-describedby={describedBy(path, !!help, d)}
          onChange={(e) => write(e.target.value === "" ? blank : e.target.value)}
        />
      ) : (
        <Input
          id={id}
          inputMode={number ? "numeric" : undefined}
          value={shown ?? ""}
          placeholder={placeholder}
          aria-invalid={d.problem(path) ? true : undefined}
          aria-describedby={describedBy(path, !!help, d)}
          onChange={(e) => {
            const v = e.target.value
            // A whole number saves as a number; anything else is sent as typed so the
            // save's own check names the problem (§spec:dashboard-settings).
            write(v === "" ? blank : number && /^\d+$/.test(v) ? Number(v) : v)
          }}
        />
      )}
      <Notes path={path} help={help} />
    </Field>
  )
}

export function SwitchSetting({ path, label, help }: { path: Loc; label: string; help?: ReactNode }) {
  const d = useDraft()
  const on = getIn(d.doc, path) === true
  const id = idFor(path)
  return (
    <Field orientation="horizontal">
      <FieldContent>
        <FieldLabel htmlFor={d.editable ? id : undefined} asChild={!d.editable}>
          {d.editable ? label : <span>{label}</span>}
        </FieldLabel>
        <Notes path={path} help={help} />
      </FieldContent>
      {d.editable
        ? <Switch id={id} checked={on} onCheckedChange={(v) => d.set(path, v)} aria-describedby={describedBy(path, !!help, d)} />
        : <span className="text-sm">{on ? "On" : "Off"}</span>}
    </Field>
  )
}
