// Opens the real page with a fixture scenario: its requests to ./api/* are
// answered from src/fixtures/scenarios.ts and the clock is fixed at the
// scenario's time. Shared by the render script and the screenshot tests.
import { SCENARIOS } from "../src/fixtures/scenarios.ts"

export const SCENARIO_NAMES = Object.keys(SCENARIOS)

/**
 * @param {import("playwright").Page} page
 * @param {string} baseURL  where the built page is served, ending in "/"
 * @param {string} name     scenario name
 * @param {{ tab?: string, query?: string }} [opts]
 */
export async function openScenario(page, baseURL, name, opts = {}) {
  const s = SCENARIOS[name]
  if (!s) throw new Error(`unknown scenario ${name}`)
  let gone = false
  // Answers wait until the bundled fonts have loaded, so charts measure their
  // labels with the real font (Recharts caches text widths).
  let release
  const fontsReady = new Promise((r) => { release = r })
  await page.route("**/api/**", async (route) => {
    await fontsReady
    const url = new URL(route.request().url())
    const key = url.pathname.replace(/^.*\/api\//, "") + url.search
    if (s.mode.kind === "hangs") return // never answers: the page stays in its loading state
    if (s.mode.kind === "refused") return route.abort("connectionrefused") // never reached
    if (s.mode.kind === "no_config") {
      return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: s.mode.detail }) })
    }
    if (gone && s.mode.kind === "restarts_broken") {
      return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: s.mode.detail }) })
    }
    if (gone) return route.abort("connectionrefused")
    const status = s.fail?.[key]
    if (status) return route.fulfill({ status, contentType: "application/json", body: JSON.stringify({ detail: "Internal Server Error" }) })
    const body = s.answers[key]
    if (!body) return route.fulfill({ status: 404, body: "{}" })
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) })
  })
  await page.clock.install({ time: new Date(s.now) })
  const q = opts.query ? `?${opts.query}` : ""
  await page.goto(`${baseURL}${q}${opts.tab && opts.tab !== "overview" ? `#${opts.tab}` : ""}`)
  await page.addStyleTag({ content: "*,*::before,*::after{transition:none!important;animation:none!important;caret-color:transparent!important}" })
  await page.evaluate(async () => {
    await document.fonts.load('400 12px "Inter Variable"')
    await document.fonts.load('600 12px "Inter Variable"')
    await document.fonts.load('400 12px "Geist Mono Variable"')
    await document.fonts.ready
  })
  release()
  await page.waitForTimeout(100)
  await page.clock.runFor(500)
  await page.evaluate(() => document.fonts.ready)
  if (s.mode.kind === "goes_away" || s.mode.kind === "restarts_broken") {
    gone = true
    await page.clock.runFor(s.mode.minutes * 60_000)
  }
  await page.waitForTimeout(100)
}
