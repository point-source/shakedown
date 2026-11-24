// One card per source: its login (§spec:dashboard-credentials, available while
// the file is locked), its request ceiling, and its collections, each opening
// the collection editor.
import { useState } from "react"
import { ChevronRight } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import type { ConfigAnswer, CredentialStatus, SourceConfig } from "@/api/types"
import { refusal, send, useNote, type Note } from "@/dash/actions"
import { clock } from "@/lib/format"
import { scheduleLine } from "@/lib/schedule"
import { CREDENTIALS } from "@/lib/words"
import { useDraft } from "@/settings/draft"
import { TextSetting } from "@/settings/fields"

type Check = "accepted" | "rejected" | "unchecked"
const CHECK: Record<Check, (source: string) => Note> = {
  accepted: (s) => ({ tone: "success", title: "Login accepted", text: `${s} accepted the login.` }),
  rejected: (s) => ({ tone: "warning", title: "Login rejected", text: `${s} rejected it. It's saved; check it and enter it again.` }),
  unchecked: (s) => ({ tone: "default", title: "Login saved, not checked", text: `Shakedown couldn't reach ${s}. Validate checks it later.` }),
}

function LoginDialog({ source, change, onDone }: { source: string; change: boolean; onDone: () => void }) {
  const note = useNote()
  const [open, setOpen] = useState(false)
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [busy, setBusy] = useState(false)
  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    const a = await send<{ check?: Check; code?: string }>("PUT", "api/credentials", { source, email, password })
    setBusy(false)
    if (a.status === 200 && a.body?.check) {
      setOpen(false)
      setEmail("")
      setPassword("")
      note(CHECK[a.body.check](source))
      onDone()
    } else note(refusal(a))
  }
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>{change ? "Change login" : "Add login"}</Button>
      <DialogContent>
        <form onSubmit={save} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>Login for {source}</DialogTitle>
            <DialogDescription>
              For shows that need an account to download. Shakedown keeps it in its database and never shows it again.
            </DialogDescription>
          </DialogHeader>
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="login-email">Email</FieldLabel>
              <Input id="login-email" type="email" autoComplete="off" required value={email} onChange={(e) => setEmail(e.target.value)} />
            </Field>
            <Field>
              <FieldLabel htmlFor="login-password">Password</FieldLabel>
              <Input id="login-password" type="password" autoComplete="new-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
            </Field>
          </FieldGroup>
          <DialogFooter>
            <DialogClose asChild><Button type="button" variant="outline">Cancel</Button></DialogClose>
            <Button type="submit" disabled={busy}>{busy ? "Checking…" : "Save login"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function Login({ source, status, onDone }: { source: string; status: CredentialStatus; onDone: () => void }) {
  const note = useNote()
  const forget = async () => {
    const a = await send<{ code?: string }>("DELETE", `api/credentials?source=${encodeURIComponent(source)}`)
    if (a.status === 200 || a.status === 204) {
      note({ tone: "success", title: "Login forgotten", text: source })
      onDone()
    } else note(refusal(a))
  }
  return (
    <div className="flex min-w-0 flex-col items-start gap-1.5">
      <span className="text-sm leading-snug font-medium">Login</span>
      <span>{CREDENTIALS[status]}</span>
      {status === "environment"
        ? <span className="text-sm text-muted-foreground">Set in the container. Environment variables win over a login entered here.</span>
        : (
          <div className="flex gap-2">
            <LoginDialog source={source} change={status === "dashboard"} onDone={onDone} />
            {status === "dashboard" && <Button size="sm" variant="ghost" onClick={forget}>Forget</Button>}
          </div>
        )}
    </div>
  )
}

export function SourceCard({ index, source, config, now, onChanged }: {
  index: number; source: SourceConfig; config: ConfigAnswer; now: string; onChanged: () => void
}) {
  const d = useDraft()
  const type = config.source_types.find((t) => t.type === source.type)
  const status = config.credentials.find((c) => c.source === source.name)?.credentials ?? "none"
  const problemsAt = (j: number) =>
    config.errors.filter((e) => e.loc[0] === "sources" && e.loc[1] === index && e.loc[2] === "collections" && e.loc[3] === j).length
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle className="wrap-anywhere">{source.name}</CardTitle>
        <CardDescription>Type {source.type}</CardDescription>
        {d.editable && (
          <CardAction>
            <Button size="sm" variant="outline" onClick={() => { location.hash = `settings/${encodeURIComponent(source.name)}/new` }}>
              Add collection
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <div className="grid gap-6 lg:grid-cols-2">
          {type?.takes_credentials && <Login source={source.name} status={status} onDone={onChanged} />}
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
          <ul className="divide-y rounded-md border">
            {source.collections.map((c, j) => {
              const next = config.next_syncs.find((n) => n.source === source.name && n.collection === c.name)?.next_at
              const errors = problemsAt(j)
              return (
                <li key={c.name}>
                  <a
                    href={`#settings/${encodeURIComponent(source.name)}/${encodeURIComponent(c.name)}`}
                    className="flex items-center gap-3 px-3 py-2.5 outline-none hover:bg-accent focus-visible:ring-[3px] focus-visible:ring-ring/50"
                  >
                    <div className="flex min-w-0 flex-1 flex-col gap-0.5 lg:flex-row lg:items-center lg:gap-6">
                      <span className="min-w-0 font-medium wrap-anywhere lg:w-56">{c.name}</span>
                      <span className="text-sm text-muted-foreground tabular-nums lg:w-64">{scheduleLine(c.schedule)}</span>
                      {next && <span className="text-sm text-muted-foreground tabular-nums">Next sync {clock(next, now)}</span>}
                      {errors > 0 && (
                        <span className="text-sm text-destructive">{errors === 1 ? "1 setting has an error" : `${errors} settings have errors`}</span>
                      )}
                    </div>
                    <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
                    <span className="sr-only">{d.editable ? "Edit" : "Show"} {c.name}</span>
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
