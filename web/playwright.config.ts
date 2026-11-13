// Screenshot (golden) tests for the dashboard: every target size in both themes.
// Baselines are committed only for approved screens (§spec:ui-kit "Screens").
// Environment: baselines are generated where CI renders them; a mismatch from
// fonts or renderer is a finding, never a reason to loosen the threshold.
import { defineConfig } from "playwright/test"

const SIZES = [
  { name: "390", viewport: { width: 390, height: 844 } },
  { name: "768", viewport: { width: 768, height: 1024 } },
  { name: "1440", viewport: { width: 1440, height: 900 } },
]
const THEMES = ["light", "dark"] as const
const PORT = 4180

export default defineConfig({
  testDir: "tests",
  snapshotPathTemplate: "{testDir}/__screenshots__/{arg}-{projectName}{ext}",
  fullyParallel: true,
  reporter: "list",
  expect: { toHaveScreenshot: { maxDiffPixels: 0, threshold: 0.05, animations: "disabled", caret: "hide" } },
  use: {
    baseURL: `http://127.0.0.1:${PORT}/`,
    deviceScaleFactor: 1,
    locale: "en-US",
    timezoneId: "UTC",
    reducedMotion: "reduce",
  },
  projects: SIZES.flatMap((s) =>
    THEMES.map((theme) => ({
      name: `${s.name}-${theme}`,
      use: { viewport: s.viewport, colorScheme: theme },
    })),
  ),
  webServer: {
    command: `npx vite build && npx vite preview --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}/`,
    reuseExistingServer: false,
  },
})
