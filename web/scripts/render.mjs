// Render the page headlessly for review: every theme x size.
// Usage: node scripts/render.mjs <outDir> [view=kit]
// Serves the built dist/ with `vite preview` on 127.0.0.1 and captures full pages.
import { spawn } from "node:child_process"
import { mkdirSync } from "node:fs"
import { chromium } from "playwright"

const out = process.argv[2] ?? "../.ui-review/kit"
const view = process.argv[3] ?? ""
const SIZES = [[390, 844], [768, 1024], [1440, 900]]
const THEMES = ["light", "dark"]
const PORT = 4179

mkdirSync(out, { recursive: true })
const server = spawn("npx", ["vite", "preview", "--host", "127.0.0.1", "--port", String(PORT), "--strictPort"], { stdio: "ignore" })
try {
  for (let i = 0; i < 50; i++) {
    try { await fetch(`http://127.0.0.1:${PORT}/`); break } catch { await new Promise((r) => setTimeout(r, 200)) }
  }
  const browser = await chromium.launch()
  for (const theme of THEMES) for (const [w, h] of SIZES) {
    const ctx = await browser.newContext({
      viewport: { width: w, height: h }, deviceScaleFactor: 1, colorScheme: theme,
      reducedMotion: "reduce", locale: "en-US", timezoneId: "UTC",
    })
    const page = await ctx.newPage()
    await page.goto(`http://127.0.0.1:${PORT}/${view ? `?view=${view}` : ""}`)
    await page.addStyleTag({ content: "*,*::before,*::after{transition:none!important;animation:none!important;caret-color:transparent!important}" })
    await page.evaluate(() => document.fonts.ready)
    await page.waitForTimeout(150)
    const file = `${out}/${view || "preview"}-${theme}-${w}.png`
    await page.screenshot({ path: file, fullPage: true })
    console.log(file)
    await ctx.close()
  }
  await browser.close()
} finally {
  server.kill()
}
