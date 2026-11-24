import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import { App } from "./App"
import { KitSheet } from "./KitSheet"

// ?view=kit shows the kit sheet (covered by its own screenshot test).
const params = new URLSearchParams(location.search)
const kit = params.get("view") === "kit"
const overlay = params.get("overlay") === "1"

createRoot(document.getElementById("root")!).render(
  <StrictMode>{kit ? <KitSheet overlay={overlay} /> : <App />}</StrictMode>,
)
