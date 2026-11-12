import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import { KitPreview } from "./KitPreview"
import { KitSheet } from "./KitSheet"

// ?view=kit shows the kit sheet; otherwise the representative screen (until Screens).
const params = new URLSearchParams(location.search)

createRoot(document.getElementById("root")!).render(
  <StrictMode>{params.get("view") === "kit" ? <KitSheet /> : <KitPreview />}</StrictMode>,
)
