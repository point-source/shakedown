// The page's writes (§spec:dashboard-run-controls) and the toasts that report
// them. Every write is a same-origin JSON request, so the write gate lets it
// through with no token.
import { createContext, useCallback, useContext, useState, type ReactNode } from "react"
import { Toast, ToastDescription, ToastProvider, ToastTitle, ToastViewport } from "@/components/ui/toast"

export interface Answer<T = unknown> { status: number; body: T | null }

export async function send<T = unknown>(method: "POST" | "PUT" | "DELETE", path: string, body?: unknown): Promise<Answer<T>> {
  try {
    const r = await fetch(`./${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
      cache: "no-store",
    })
    return { status: r.status, body: (await r.json().catch(() => null)) as T | null }
  } catch {
    return { status: 0, body: null }
  }
}

export interface Note { tone: "success" | "warning" | "danger" | "default"; title: string; text?: string }

const Ctx = createContext<(n: Note) => void>(() => {})

/** Toasts for the results of actions; each stays until replaced or closed. */
export function Notes({ children }: { children: ReactNode }) {
  const [notes, setNotes] = useState<(Note & { id: number })[]>([])
  const push = useCallback((n: Note) => setNotes((all) => [...all.slice(-2), { ...n, id: Date.now() + Math.random() }]), [])
  return (
    <Ctx.Provider value={push}>
      <ToastProvider duration={8000}>
        {children}
        {notes.map((n) => (
          <Toast key={n.id} variant={n.tone} onOpenChange={(open) => !open && setNotes((all) => all.filter((x) => x.id !== n.id))}>
            <div className="grid gap-1">
              <ToastTitle>{n.title}</ToastTitle>
              {n.text && <ToastDescription>{n.text}</ToastDescription>}
            </div>
          </Toast>
        ))}
        <ToastViewport />
      </ToastProvider>
    </Ctx.Provider>
  )
}

export const useNote = () => useContext(Ctx)

/** The words for a refused or failed write that every action shares. */
export function refusal(a: Answer<{ code?: string; detail?: string }>): Note {
  // The busy refusal is a 409 with prose and no code (§spec:api-sync-runs).
  if (a.status === 409 && !a.body?.code)
    return { tone: "warning", title: "A run is in progress", text: "Try again when it ends." }
  if (a.status === 403) return { tone: "danger", title: "Shakedown refused the change", text: "Open the dashboard at its usual address and try again." }
  if (a.status === 0) return { tone: "danger", title: "Can't reach Shakedown", text: "Nothing changed. Try again." }
  return { tone: "danger", title: "That didn't work", text: `Shakedown answered ${a.status}.` }
}
