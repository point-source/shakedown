// Render the page headlessly for review: scenario x tab x size x theme, plus the
// kit sheet. Usage: node scripts/render.mjs <outDir> [filter]
// `filter` matches file names (e.g. "default-overview"). Serves the built dist/
// with `vite preview` on 127.0.0.1 for the duration of the run only.
import { spawn } from "node:child_process"
import { mkdirSync } from "node:fs"
import { chromium } from "playwright"
import { openScenario } from "./fixture-page.mjs"

const out = process.argv[2] ?? "../.ui-review/screens"
const filter = process.argv[3] ?? ""
const SIZES = [[390, 844], [768, 1024], [1440, 900]]
const THEMES = ["light", "dark"]
const ALL = ["overview", "runs", "sources", "library"]
const MATRIX = [
  ...ALL.map((tab) => ({ scenario: "default", tab })),
  ...ALL.map((tab) => ({ scenario: "long_content", tab })),
  ...ALL.map((tab) => ({ scenario: "first_install", tab })),
  ...["quiet", "config_broken", "no_config", "unreachable", "loading"].map((scenario) => ({ scenario, tab: "overview" })),
  { scenario: "loading", tab: "library" },
]
const PORT = 4179

mkdirSync(out, { recursive: true })
const server = spawn("npx", ["vite", "preview", "--host", "127.0.0.1", "--port", String(PORT), "--strictPort"], { stdio: "ignore" })
try {
  for (let i = 0; i < 50; i++) {
    try { await fetch(`http://127.0.0.1:${PORT}/`); break } catch { await new Promise((r) => setTimeout(r, 200)) }
  }
  const browser = await chromium.launch()
  for (const m of MATRIX) for (const theme of THEMES) for (const [w, h] of SIZES) {
    const file = `${out}/${m.scenario}-${m.tab}${m.label ? `-${m.label}` : ""}-${w}-${theme}.png`
    if (filter && !file.includes(filter)) continue
    const ctx = await browser.newContext({
      viewport: { width: w, height: h }, deviceScaleFactor: 1, colorScheme: theme,
      reducedMotion: "reduce", locale: "en-US", timezoneId: "UTC",
    })
    const page = await ctx.newPage()
    await openScenario(page, `http://127.0.0.1:${PORT}/`, m.scenario, { tab: m.tab, query: m.query })
    await page.screenshot({ path: file, fullPage: true })
    console.log(file)
    await ctx.close()
  }
  await browser.close()
} finally {
  server.kill()
}
