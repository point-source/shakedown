// First-run setup and the shell with no config loaded (§spec:first-run-setup,
// §spec:dashboard-settings "A broken file"; Design round S4): each state as
// goldens, and what finishing does as behaviour tests.
import { expect, test, type Page } from "playwright/test"
import { openScenario } from "../scripts/fixture-page.mjs"

const id = (loc: string) => `[id='set-${loc}']`
const NAME = id("sources.0.collections.0.name")
const QUERY = id("sources.0.collections.0.query")
const SOURCE_NAME = id("sources.0.name")
const LIBRARY = id("library_root")

const chooseSource = async (page: Page, type: string) => {
  await page.locator("#setup-type").click()
  await page.getByRole("option", { name: type, exact: true }).click()
}
const fill = async (page: Page) => {
  await chooseSource(page, "ia")
  await page.locator(NAME).fill("phish")
  await page.locator(QUERY).fill("collection:Phish")
}
const answer = (page: Page, path: string, status: number, body: unknown) =>
  page.route(`**/api/${path}`, (r) => (r.request().method() === "POST"
    ? r.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) })
    : r.fallback()))

const PAGES: [string, string][] = [
  ["setup", "setup"],
  ["setup-warnings", "setup_warnings"],
  ["config-unreadable", "config_unreadable"],
  ["config-invalid", "config_invalid"],
]
for (const [name, scenario] of PAGES) {
  test(`setup ${name}`, async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, scenario)
    // Wait for the config's answer: every state but unreadable has a main landmark.
    await expect(page.locator(scenario === "config_unreadable" ? "[role=alert]" : "main").first()).toBeVisible()
    await expect(page).toHaveScreenshot(`${name}.png`, { fullPage: true })
  })
}

const STATES: { name: string; go: (page: Page) => Promise<void> }[] = [
  { name: "required", go: async (page) => {
    await page.getByRole("button", { name: "Finish setup" }).click()
    await expect(page.getByText("Setup isn't finished", { exact: true })).toBeVisible()
  } },
  { name: "link", go: async (page) => {
    await chooseSource(page, "ia")
    await page.locator(NAME).fill("bobweir")
    await page.locator(QUERY).fill("https://archive.org/details/BobWeir")
    await expect(page.locator(QUERY)).toHaveValue("collection:BobWeir")
  } },
]
for (const s of STATES) {
  test(`setup ${s.name}`, async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "setup")
    await s.go(page)
    await expect(page).toHaveScreenshot(`setup-${s.name}.png`, { fullPage: true })
  })
}

test.describe("behaviour", () => {
  test.beforeEach(({}, info) => {
    test.skip(info.project.name !== "1440-light", "behaviour, not layout: one project is enough")
  })

  test("finishing sends only what setup sets, with the browser's time zone, and opens Overview", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "setup", { tab: "settings" })
    await expect(page.getByRole("tab")).toHaveCount(0)
    await fill(page)
    const sent = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/api/setup"))
    await page.getByRole("button", { name: "Finish setup" }).click()
    expect((await sent).postDataJSON()).toEqual({ document: {
      archive_root: "/data/archive", library_root: "/data/library", timezone: "UTC",
      sources: [{ name: "ia", type: "ia", collections: [{ name: "phish", query: "collection:Phish", schedule: "0 3 * * 0" }] }],
    } })
    await expect(page.getByText("Shakedown is set up", { exact: true })).toBeVisible()
    await expect(page.getByText("The first sync of phish has started. It shows under Running now.", { exact: true })).toBeVisible()
    await expect(page).not.toHaveURL(/#/)
  })

  test("the source name follows the source until someone types one", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "setup")
    await chooseSource(page, "etree")
    await expect(page.locator(SOURCE_NAME)).toHaveValue("etree")
    await chooseSource(page, "ia")
    await expect(page.locator(SOURCE_NAME)).toHaveValue("ia")
    await page.locator(SOURCE_NAME).fill("internetarchive")
    await chooseSource(page, "etree")
    await expect(page.locator(SOURCE_NAME)).toHaveValue("internetarchive")
  })

  test("the folders are checked on opening and again after one changes", async ({ page, baseURL }) => {
    const checks: unknown[] = []
    page.on("request", (r) => { if (r.url().endsWith("/api/config/check-paths")) checks.push(r.postDataJSON()) })
    await openScenario(page, baseURL!, "setup")
    await expect(page.getByText("Both folders work, and library files can link to the archive.")).toBeVisible()
    expect(checks).toEqual([{ archive_root: "/data/archive", library_root: "/data/library" }])
    await page.locator(LIBRARY).fill("/media/music")
    await page.locator(NAME).focus()
    await expect.poll(() => checks.length).toBe(2)
    expect(checks[1]).toEqual({ archive_root: "/data/archive", library_root: "/media/music" })
    await page.locator(QUERY).focus()
    expect(checks.length).toBe(2)
  })

  test("a check that fails warns and offers Check again; setup can still finish", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "setup")
    await answer(page, "config/check-paths", 500, { detail: "Internal Server Error" })
    await page.locator(LIBRARY).fill("/media/music")
    await page.locator(NAME).focus()
    await expect(page.getByText("Shakedown couldn't check the folders. You can still finish setup.")).toBeVisible()
    await expect(page.getByRole("button", { name: "Check again" })).toBeVisible()
    await expect(page.getByRole("button", { name: "Finish setup" })).toBeEnabled()
  })

  test("a folder warning sits under its folder and a sync that can't start says why", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "setup_warnings")
    await expect(page.getByText("Library files can't link to the archive here. Put both folders on the same drive.")).toBeVisible()
    await fill(page)
    await page.getByRole("button", { name: "Finish setup" }).click()
    await expect(page.getByText("The first sync of phish didn't start: another run is going. Press Sync now when it ends.", { exact: true })).toBeVisible()
  })

  test("a refused document marks its settings and lists the rest", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "setup")
    await answer(page, "setup", 422, { errors: [
      { loc: ["sources", 0, "collections", 0, "schedule"], code: "schedule_invalid", message: "bad cron" },
      { loc: ["timezone"], code: "timezone_unknown", message: "unknown timezone" },
    ] })
    await fill(page)
    await page.getByRole("button", { name: "Finish setup" }).click()
    await expect(page.getByText("Setup isn't finished", { exact: true })).toBeVisible()
    await expect(page.getByText("Shakedown can't read this schedule.", { exact: false })).toBeVisible()
    await expect(page.getByText("Setup has an error")).toBeVisible()
    await expect(page.getByText("Not a time zone Shakedown knows.", { exact: false })).toBeVisible()
  })

  test("a file that appeared meanwhile is never overwritten: the page moves to Settings", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "setup")
    await answer(page, "setup", 409, { code: "exists" })
    await fill(page)
    await page.getByRole("button", { name: "Finish setup" }).click()
    await expect(page.getByText("shakedown.yaml already exists", { exact: true })).toBeVisible()
    await expect(page).toHaveURL(/#settings$/)
  })

  test("a file that can't be written leaves setup open", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "setup")
    await answer(page, "setup", 500, { code: "write_failed", path: "/data/config/shakedown.yaml" })
    await fill(page)
    await page.getByRole("button", { name: "Finish setup" }).click()
    await expect(page.getByText("Check that /data/config/shakedown.yaml isn't read-only. Nothing was set up.", { exact: true })).toBeVisible()
    await expect(page.getByRole("button", { name: "Finish setup" })).toBeVisible()
  })

  test("a file Shakedown can't read is fixed by hand, with no tabs", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "config_unreadable", { tab: "settings" })
    await expect(page.getByText("Fix /data/config/shakedown.yaml by hand.", { exact: false })).toBeVisible()
    await expect(page.getByRole("tab")).toHaveCount(0)
    await expect(page.getByRole("button", { name: "Finish setup" })).toHaveCount(0)
  })

  test("a file with errors opens in Settings with no tabs", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "config_invalid")
    await expect(page.getByText("Shakedown can't load shakedown.yaml")).toBeVisible()
    await expect(page.getByRole("tab")).toHaveCount(0)
    await expect(page.getByText("Not a time zone Shakedown knows.", { exact: false })).toBeVisible()
  })
})
