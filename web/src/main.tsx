import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import { App } from "./App"
import { KitSheet } from "./KitSheet"

// ?view=kit shows the kit sheet (covered by its own screenshot test).
const kit = new URLSearchParams(location.search).get("view") === "kit"

createRoot(document.getElementById("root")!).render(
  <StrictMode>{kit ? <KitSheet /> : <App />}</StrictMode>,
)
