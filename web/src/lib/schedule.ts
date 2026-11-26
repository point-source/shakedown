// A collection's schedule as the dashboard reads it back (§spec:schedules
// "Presets and cron"): an expression of exactly a preset's form reads as that
// preset, any other as cron, and no key as Manual only.

export const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"] as const

export type Schedule =
  | { kind: "daily"; hour: number; minute: number }
  | { kind: "weekly"; day: number; hour: number; minute: number }
  | { kind: "manual" }
  | { kind: "cron"; expr: string }

const whole = (s: string, max: number) => (/^\d{1,2}$/.test(s) && Number(s) <= max ? Number(s) : null)

export function readSchedule(value: unknown): Schedule {
  if (value === null || value === undefined) return { kind: "manual" }
  // A file with errors keeps its values as written, so a schedule may not be text.
  const expr = String(value)
  const f = expr.trim().split(/\s+/)
  if (f.length === 5 && f[2] === "*" && f[3] === "*") {
    const minute = whole(f[0], 59)
    const hour = whole(f[1], 23)
    if (minute !== null && hour !== null) {
      if (f[4] === "*") return { kind: "daily", hour, minute }
      const day = whole(f[4], 6)
      if (day !== null) return { kind: "weekly", day, hour, minute }
    }
  }
  return { kind: "cron", expr }
}

export function writeSchedule(s: Schedule): string | null {
  switch (s.kind) {
    case "daily": return `${s.minute} ${s.hour} * * *`
    case "weekly": return `${s.minute} ${s.hour} * * ${s.day}`
    case "manual": return null
    case "cron": return s.expr
  }
}

export const hhmm = (h: number, m: number) => `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`

/** One line for a collection row: "Daily at 03:30", "Weekly, Sunday at 03:00". */
export function scheduleLine(expr: unknown): string {
  const s = readSchedule(expr)
  switch (s.kind) {
    case "daily": return `Daily at ${hhmm(s.hour, s.minute)}`
    case "weekly": return `Weekly, ${DAYS[s.day]} at ${hhmm(s.hour, s.minute)}`
    case "manual": return "Manual only"
    case "cron": return `Custom: ${s.expr}`
  }
}

/** A new collection's schedule (§spec:schedules "Staggered default"): Weekly on
 * Sunday at the first whole hour from 03:00 at which no other collection starts. */
export function staggeredDefault(existing: unknown[]): string {
  const taken = new Set<number>()
  for (const e of existing) {
    const s = readSchedule(e)
    if (s.kind === "daily" && s.minute === 0) taken.add(s.hour)
    if (s.kind === "weekly" && s.day === 0 && s.minute === 0) taken.add(s.hour)
  }
  let hour = 3
  while (taken.has(hour) && hour < 23) hour++
  return writeSchedule({ kind: "weekly", day: 0, hour, minute: 0 })!
}
