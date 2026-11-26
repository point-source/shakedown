// The settings being edited: a copy of the file's document, changed in place by
// path and saved whole (§spec:dashboard-settings "Save is a whole-document
// replace"). Problems and warnings are keyed by the `loc` they name.
import { createContext, useContext } from "react"
import type { ConfigDocument, Loc } from "@/api/types"

export const locKey = (loc: Loc) => loc.join(".")

export function setIn<T>(doc: T, path: Loc, value: unknown): T {
  const next = structuredClone(doc) as Record<string | number, unknown>
  let at = next
  for (const k of path.slice(0, -1)) at = at[k] as Record<string | number, unknown>
  at[path[path.length - 1]] = value
  return next as T
}

export function getIn(doc: unknown, path: Loc): unknown {
  let at = doc as Record<string | number, unknown> | null | undefined
  for (const k of path) at = at?.[k] as Record<string | number, unknown> | null | undefined
  return at
}

export interface Draft {
  doc: ConfigDocument
  /** False while the file is locked: settings are shown without controls. */
  editable: boolean
  /** Changes when the draft is replaced from outside (Discard, a new file): fields
   * holding text of their own start again from the draft. */
  generation: number
  set: (path: Loc, value: unknown) => void
  /** The words for the setting at a path: its error, and its warnings after a save. */
  problem: (path: Loc) => string | null
  warnings: (path: Loc) => string[]
}

export const DraftCtx = createContext<Draft | null>(null)
export const useDraft = () => useContext(DraftCtx)!
