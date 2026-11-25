// A link pasted into a collection's Search, read by the collection's source
// plugin as the search it stands for (§spec:dashboard-settings "Pasted links";
// operator decisions, Design round S3). The page knows no link rules of its own.
import type { SourceLinkAnswer } from "@/api/types"

export type LinkReading = { kind: "read"; query: string } | { kind: "unreadable" } | { kind: "failed" }

/** Only text that is a web link is sent for reading; anything else is a search. */
export const isLink = (text: string) => /^https?:\/\/\S+$/i.test(text.trim())

export async function readLink(type: string, url: string): Promise<LinkReading> {
  try {
    const r = await fetch(`./api/source-link?${new URLSearchParams({ type, url: url.trim() })}`, { cache: "no-store" })
    if (!r.ok) return { kind: "failed" }
    const a = (await r.json()) as SourceLinkAnswer
    return a.query ? { kind: "read", query: a.query } : { kind: "unreadable" }
  } catch {
    return { kind: "failed" }
  }
}
