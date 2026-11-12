// How the page writes numbers, sizes, times, and durations.
// Sizes: decimal units (1 GB = 10^9 bytes) — provisional, see plan Q-units.

const nf = new Intl.NumberFormat("en-US")
export const count = (n: number) => nf.format(n)
export const plural = (n: number, one: string, many: string) => `${count(n)} ${n === 1 ? one : many}`

const UNITS = ["B", "kB", "MB", "GB", "TB", "PB"]
export function bytes(n: number): string {
  let i = 0
  let v = n
  while (v >= 1000 && i < UNITS.length - 1) { v /= 1000; i++ }
  const digits = i === 0 ? 0 : v >= 100 ? 0 : v >= 10 ? 1 : 2
  return `${v.toFixed(digits)} ${UNITS[i]}`
}
export const speed = (bps: number) => `${bytes(bps)}/s`

export function duration(seconds: number): string {
  const s = Math.round(seconds)
  if (s < 60) return `${s} s`
  const m = Math.round(s / 60)
  if (m < 60) return `${m} min`
  const h = Math.floor(m / 60)
  const rest = m % 60
  if (h < 24) return rest ? `${h} h ${rest} min` : `${h} h`
  const d = Math.floor(h / 24)
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`
}

export const ago = (iso: string, now: string) =>
  duration(Math.max(0, (Date.parse(now) - Date.parse(iso)) / 1000))

const tf = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" })
const df = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" })
export function clock(iso: string, now: string): string {
  const d = new Date(iso)
  const sameDay = new Date(now).toDateString() === d.toDateString()
  return sameDay ? tf.format(d) : `${df.format(d)}, ${tf.format(d)}`
}
