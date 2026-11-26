// Add a source to the draft: a name (fixed once saved, like a collection's) and
// one of the installed source types. Its first collection opens next.
import { useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import type { SourceConfig } from "@/api/types"
import { useDraft } from "@/settings/draft"
import { addHref } from "@/settings/route"

export function AddSource({ types }: { types: string[] }) {
  const d = useDraft()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [type, setType] = useState(types[0] ?? "")
  const taken = d.doc.sources.some((s) => s.name === name.trim())
  const add = (e: React.FormEvent) => {
    e.preventDefault()
    const n = name.trim()
    if (!n || taken || !type) return
    const source: SourceConfig = { name: n, type, collections: [], max_concurrent_requests: null }
    d.set(["sources"], [...d.doc.sources, source])
    setOpen(false)
    setName("")
    location.hash = addHref(n).slice(1)
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>Add source</Button>
      <DialogContent>
        <form onSubmit={add} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>Add source</DialogTitle>
            <DialogDescription>A site to mirror from. Its first collection comes next.</DialogDescription>
          </DialogHeader>
          <FieldGroup>
            <Field data-invalid={taken ? true : undefined}>
              <FieldLabel htmlFor="source-name">Name</FieldLabel>
              <Input
                id="source-name"
                required
                value={name}
                aria-invalid={taken ? true : undefined}
                aria-describedby="source-name-help"
                onChange={(e) => setName(e.target.value)}
              />
              <FieldDescription id="source-name-help">Fixed once saved: its shows are filed under it.</FieldDescription>
              {taken && <FieldError>There's already a source called {name.trim()}.</FieldError>}
            </Field>
            <Field>
              <FieldLabel htmlFor="source-type">Type</FieldLabel>
              <Select value={type} onValueChange={setType}>
                <SelectTrigger id="source-type" className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {types.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
                </SelectContent>
              </Select>
              <FieldDescription>The plugins installed in this Shakedown.</FieldDescription>
            </Field>
          </FieldGroup>
          <DialogFooter>
            <DialogClose asChild><Button type="button" variant="outline">Cancel</Button></DialogClose>
            <Button type="submit" disabled={!name.trim() || taken}>Add source</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
