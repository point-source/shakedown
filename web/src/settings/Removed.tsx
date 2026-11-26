// Collections the file no longer names whose files Shakedown still holds
// (§spec:collection-removal "Unconfigured collections"): add one back, or delete
// its library folders or everything. Deletes stay available while locked.
import { useState } from "react"
import { Button, buttonVariants } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter,
  AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Label } from "@/components/ui/label"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import type { UnconfiguredCollection } from "@/api/types"
import { refusal, send, useNote } from "@/dash/actions"
import { bytes, count, plural } from "@/lib/format"
import { useDraft } from "@/settings/draft"
import { addHref } from "@/settings/route"

type Scope = "library" | "everything"

function DeleteFiles({ c, onDone }: { c: UnconfiguredCollection; onDone: () => void }) {
  const note = useNote()
  const [open, setOpen] = useState(false)
  const [scope, setScope] = useState<Scope>("library")
  const shows = plural(c.shows, "show", "shows")
  const go = async () => {
    const a = await send<{ code?: string }>("POST", "api/collections/purge", { source: c.source, collection: c.collection, scope })
    if (a.status === 202) {
      note({ tone: "success", title: "Deleting files", text: `${c.collection}. It shows under Running now.` })
      onDone()
    } else if (a.status === 409 && a.body?.code === "configured") {
      note({ tone: "warning", title: `${c.collection} is in shakedown.yaml again`, text: "Nothing was deleted." })
      onDone()
    } else if (a.status === 404) {
      note({ tone: "default", title: "Nothing left to delete", text: c.collection })
      onDone()
    } else note(refusal(a))
  }
  return (
    <AlertDialog open={open} onOpenChange={(o) => { setOpen(o); if (o) setScope("library") }}>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>Delete files…</Button>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete files for {c.collection}?</AlertDialogTitle>
          <AlertDialogDescription>Deleted files can't be brought back.</AlertDialogDescription>
        </AlertDialogHeader>
        <RadioGroup value={scope} onValueChange={(v) => setScope(v as Scope)} className="gap-4">
          <div className="flex items-start gap-3">
            <RadioGroupItem value="library" id="scope-library" className="mt-0.5" />
            <div className="grid gap-1">
              <Label htmlFor="scope-library">Library folders</Label>
              <p className="text-sm text-muted-foreground">
                The archive stays, so adding {c.collection} back rebuilds them with no download. Frees almost no space: library files are links into the archive.
              </p>
            </div>
          </div>
          <div className="flex items-start gap-3">
            <RadioGroupItem value="everything" id="scope-everything" className="mt-0.5" />
            <div className="grid gap-1">
              <Label htmlFor="scope-everything">Library folders and archived shows</Label>
              <p className="text-sm text-muted-foreground">
                Also deletes {shows} ({bytes(c.archive_bytes)}) from the archive. Adding {c.collection} back downloads them all again.
              </p>
            </div>
          </div>
        </RadioGroup>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction className={buttonVariants({ variant: "destructive" })} onClick={go}>
            {scope === "library" ? "Delete library folders" : "Delete everything"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

export function RemovedCard({ removed, onDone }: { removed: UnconfiguredCollection[]; onDone: () => void }) {
  const d = useDraft()
  if (removed.length === 0) return null
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>Removed collections</CardTitle>
        <CardDescription>No longer synced. Their files are still on disk.</CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y">
          {removed.map((c) => (
            <li key={`${c.source}:${c.collection}`} className="flex flex-wrap items-center gap-x-6 gap-y-2 py-3 first:pt-0 last:pb-0">
              <div className="flex min-w-0 flex-col gap-0.5">
                <span className="font-medium wrap-anywhere">{c.collection}</span>
                <span className="text-sm text-muted-foreground tabular-nums">
                  {c.source} · {count(c.shows)} {c.shows === 1 ? "show" : "shows"} · {bytes(c.archive_bytes)}
                  {c.library_bytes === 0 && " · library folders deleted"}
                </span>
              </div>
              <div className="ml-auto flex gap-2">
                {d.editable && (
                  <Button size="sm" variant="ghost" asChild>
                    <a href={addHref(c.source, c.collection)}>Add back</a>
                  </Button>
                )}
                <DeleteFiles c={c} onDone={onDone} />
              </div>
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}
