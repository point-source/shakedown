// Settings for the whole install: folders, how much runs at once, the schedule
// clock, run error retention with Trim history now, and the failure notice.
import { useState } from "react"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { FieldGroup } from "@/components/ui/field"
import { refusal, send, useNote } from "@/dash/actions"
import { useDraft } from "@/settings/draft"
import { TextSetting } from "@/settings/fields"

function TrimNow() {
  const note = useNote()
  const [busy, setBusy] = useState(false)
  const run = async () => {
    setBusy(true)
    const a = await send<{ code?: string }>("POST", "api/history/trim")
    setBusy(false)
    note(a.status === 202 ? { tone: "success", title: "Trim history started", text: "It shows under Running now." } : refusal(a))
  }
  return (
    <div className="flex flex-col items-start gap-1.5">
      <Button size="sm" variant="outline" onClick={run} disabled={busy}>{busy ? "Starting…" : "Trim history now"}</Button>
      <p className="text-sm text-muted-foreground">Drops older error details now and gives the space back to the disk.</p>
    </div>
  )
}

export function FoldersCard({ execUsesStaging }: { execUsesStaging: boolean }) {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>Folders</CardTitle>
        <CardDescription>Paths inside the Shakedown container.</CardDescription>
      </CardHeader>
      <CardContent>
        <FieldGroup>
          <TextSetting path={["archive_root"]} label="Archive folder" help="Where Shakedown keeps every downloaded file." />
          <TextSetting
            path={["library_root"]}
            label="Library folder"
            help="The folders your music tools read."
            readOnlyNote={execUsesStaging ? "Set in shakedown.yaml: a collection's command uses this path." : undefined}
          />
          <TextSetting path={["state_db"]} label="Database file" blank={null} placeholder="In the archive folder" help="Shakedown's records of every show and run." />
        </FieldGroup>
      </CardContent>
    </Card>
  )
}

export function RunsCard() {
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>Runs</CardTitle>
      </CardHeader>
      <CardContent>
        <FieldGroup>
          <div className="grid gap-6 sm:grid-cols-2">
            <TextSetting path={["max_concurrent_collections"]} number label="Collections at once" help="How many collections sync at the same time." />
            <TextSetting path={["max_concurrent_downloads"]} number label="Downloads at once" help="Per collection." />
          </div>
          <TextSetting
            path={["timezone"]}
            label="Time zone"
            blank={null}
            placeholder="The container's clock"
            help="The clock schedules follow, such as Europe/London."
          />
          <TextSetting path={["keep_run_errors_days"]} number label="Keep run errors for (days)" help="At least 30. After that a run keeps its result and counts." />
          <TrimNow />
        </FieldGroup>
      </CardContent>
    </Card>
  )
}

export function FailureNoticeCard() {
  const d = useDraft()
  const n = d.doc.notifications
  return (
    <Card className="min-w-0">
      <CardHeader>
        <CardTitle>Failure notice</CardTitle>
        <CardDescription>Shakedown posts to this address when a run fails.</CardDescription>
      </CardHeader>
      <CardContent>
        <TextSetting
          path={["notifications", "on_failure", "webhook"]}
          label="Webhook URL"
          blank={null}
          placeholder="None"
          get={() => n?.on_failure?.webhook ?? null}
          put={(v) => d.set(["notifications"], v === null ? null : { ...n, on_failure: { webhook: v } })}
        />
      </CardContent>
    </Card>
  )
}
