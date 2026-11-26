// Pages inside the Settings tab, in the URL hash so Back works and a page can be
// linked: "#settings" (the list), "#settings/<source>/<collection>" (the editor),
// and "#settings/<source>?add[=<name>]" (a new collection, or a removed one added
// back by name). Adding uses a query, so a collection may be called anything.
import { useEffect, useState } from "react"

export type SettingsRoute =
  | { page: "list" }
  | { page: "collection"; source: string; collection: string }
  | { page: "new"; source: string; name: string }

export function readRoute(hash = location.hash): SettingsRoute {
  const h = hash.replace(/^#/, "")
  const [path, query = ""] = h.split("?")
  const [tab, source, collection] = path.split("/").map((p) => decodeURIComponent(p))
  if (tab !== "settings" || !source) return { page: "list" }
  const add = new URLSearchParams(query)
  if (add.has("add")) return { page: "new", source, name: add.get("add") ?? "" }
  if (collection) return { page: "collection", source, collection }
  return { page: "list" }
}

const enc = encodeURIComponent
export const collectionHref = (source: string, collection: string) => `#settings/${enc(source)}/${enc(collection)}`
export const addHref = (source: string, name = "") => `#settings/${enc(source)}?add${name ? `=${enc(name)}` : ""}`

export function useSettingsRoute(): SettingsRoute {
  const [route, setRoute] = useState(readRoute)
  useEffect(() => {
    const on = () => {
      setRoute((was) => {
        const next = readRoute()
        if (JSON.stringify(next) !== JSON.stringify(was)) window.scrollTo(0, 0)
        return next
      })
    }
    window.addEventListener("hashchange", on)
    window.addEventListener("popstate", on)
    return () => {
      window.removeEventListener("hashchange", on)
      window.removeEventListener("popstate", on)
    }
  }, [])
  return route
}
