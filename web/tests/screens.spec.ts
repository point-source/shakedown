// Screenshot tests for every approved screen and state (§spec:ui-kit "Screens").
// Each renders the real page with a fixture scenario at a fixed clock.
import { expect, test } from "playwright/test"
import { openScenario } from "../scripts/fixture-page.mjs"

const ALL = ["overview", "runs", "sources", "library"]
const CASES: { scenario: string; tab: string }[] = [
  ...ALL.map((tab) => ({ scenario: "default", tab })),
  ...ALL.map((tab) => ({ scenario: "long_content", tab })),
  ...ALL.map((tab) => ({ scenario: "first_install", tab })),
  ...["quiet", "config_broken", "no_config", "unreachable", "loading"].map((scenario) => ({ scenario, tab: "overview" })),
  { scenario: "loading", tab: "library" },
]

for (const { scenario, tab } of CASES) {
  test(`${scenario} · ${tab}`, async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, scenario, { tab })
    await expect(page).toHaveScreenshot(`${scenario}-${tab}.png`, { fullPage: true })
  })
}
