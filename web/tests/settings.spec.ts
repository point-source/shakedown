// Settings tab (§spec:dashboard-settings; Design round S2, 2026-11-24): the page in
// each file state, the save states and dialogs as goldens, and what each answer
// does as behaviour tests.
import { expect, test, type Page } from "playwright/test"
import { openScenario } from "../scripts/fixture-page.mjs"

for (const scenario of ["default", "settings_locked", "settings_invalid"]) {
  test(`${scenario} · settings`, async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, scenario, { tab: "settings" })
    await expect(page).toHaveScreenshot(`${scenario}-settings.png`, { fullPage: true })
  })
}

const save = (page: Page) => page.getByRole("button", { name: "Save", exact: true }).click()
const days = (page: Page) => page.locator("#set-keep_run_errors_days")

// Interaction states, shot at the viewport (the bar and dialogs sit over the page).
const STATES: { name: string; scenario: string; go: (page: Page) => Promise<void> }[] = [
  { name: "dirty", scenario: "settings_saved", go: async (page) => {
    await days(page).fill("90")
    await expect(page.getByText("Unsaved changes")).toBeVisible()
  } },
  { name: "saved-warnings", scenario: "settings_saved", go: async (page) => {
    await page.locator("#set-library_root").fill("/share/music")
    await save(page)
    await expect(page.getByText("Saved, with warnings", { exact: true })).toBeVisible()
  } },
  { name: "refused", scenario: "settings_refused", go: async (page) => {
    await days(page).fill("20")
    await save(page)
    await expect(page.getByText("Nothing saved", { exact: true })).toBeVisible()
    await days(page).click()
  } },
  { name: "conflict", scenario: "settings_conflict", go: async (page) => {
    await days(page).fill("90")
    await save(page)
    await expect(page.getByRole("alertdialog")).toBeVisible()
  } },
  { name: "login", scenario: "settings_saved", go: async (page) => {
    await page.getByRole("button", { name: "Change login" }).click()
    await expect(page.getByRole("dialog")).toBeVisible()
  } },
  { name: "delete", scenario: "settings_saved", go: async (page) => {
    await page.getByRole("button", { name: "Delete files…" }).click()
    await page.locator("label[for=scope-everything]").click()
    await expect(page.getByRole("button", { name: "Delete everything" })).toBeVisible()
  } },
  { name: "override", scenario: "settings_locked", go: async (page) => {
    await page.getByRole("button", { name: "Allow dashboard edits" }).click()
    await expect(page.getByRole("alertdialog")).toBeVisible()
  } },
]

for (const s of STATES) {
  test(`settings ${s.name}`, async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, s.scenario, { tab: "settings" })
    await s.go(page)
    await expect(page).toHaveScreenshot(`settings-${s.name}.png`)
  })
}

test.describe("behaviour", () => {
  test.beforeEach(({}, info) => {
    test.skip(info.project.name !== "1440-light", "behaviour, not layout: one project is enough")
  })

  test("a save sends the whole document under the file's revision, and the bar goes", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_saved", { tab: "settings" })
    const sent = page.waitForRequest((r) => r.method() === "PUT" && r.url().endsWith("/api/config"))
    await days(page).fill("90")
    await save(page)
    const body = (await sent).postDataJSON()
    expect(body.revision).toMatch(/^sha256:/)
    expect(body.document.keep_run_errors_days).toBe(90)
    expect(body.document.sources).toHaveLength(2)
    await expect(page.getByText("Unsaved changes")).toHaveCount(0)
    await expect(days(page)).toHaveValue("90")
    await expect(page.getByText("Library files can't link to the archive here", { exact: false })).toBeVisible()
  })

  test("Discard puts the file's values back", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_saved", { tab: "settings" })
    await days(page).fill("90")
    await page.getByRole("button", { name: "Discard" }).click()
    await expect(days(page)).toHaveValue("180")
    await expect(page.getByText("Unsaved changes")).toHaveCount(0)
  })

  test("a refused save marks the setting and keeps the edit", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_refused", { tab: "settings" })
    await days(page).fill("20")
    await save(page)
    await expect(days(page)).toHaveAttribute("aria-invalid", "true")
    await expect(page.getByText("Enter 30 or more.")).toBeVisible()
    await expect(page.getByText("1 setting needs fixing before it saves")).toBeVisible()
    await expect(days(page)).toHaveValue("20")
  })

  test("a file that can't be written says where, and nothing changes", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_write_failed", { tab: "settings" })
    await days(page).fill("90")
    await save(page)
    await expect(page.getByText("Shakedown couldn't write the file", { exact: true })).toBeVisible()
    await expect(page.getByText("Check that /data/config/shakedown.yaml isn't read-only. Nothing changed.", { exact: true })).toBeVisible()
    await expect(page.getByText("Unsaved changes")).toBeVisible()
  })

  test("Reload settings after a conflict drops the edit", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_conflict", { tab: "settings" })
    await days(page).fill("90")
    await save(page)
    await page.getByRole("button", { name: "Reload settings" }).click()
    await expect(days(page)).toHaveValue("180")
  })

  test("a locked file shows settings as text, with logins and deletes still offered", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_locked", { tab: "settings" })
    await expect(page.getByText("Settings are read-only")).toBeVisible()
    await expect(page.locator("main input")).toHaveCount(0)
    await expect(page.getByRole("button", { name: "Add collection" })).toHaveCount(0)
    await expect(page.getByRole("button", { name: "Add back" })).toHaveCount(0)
    await expect(page.getByRole("button", { name: "Delete files…" })).toBeVisible()
    await expect(page.getByRole("button", { name: "Trim history now" })).toBeVisible()
    await expect(page.getByText("From environment variables")).toBeVisible()
    const sent = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/api/config/override"))
    await page.getByRole("button", { name: "Allow dashboard edits" }).click()
    await page.getByRole("alertdialog").getByRole("button", { name: "Allow dashboard edits" }).click()
    await sent
    await expect(page.getByText("Dashboard edits allowed", { exact: true })).toBeVisible()
  })

  test("a login is sent once and the source's answer is shown", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_saved", { tab: "settings" })
    await page.getByRole("button", { name: "Change login" }).click()
    await page.getByLabel("Email").fill("someone@example.net")
    await page.getByLabel("Password").fill("pw")
    const sent = page.waitForRequest((r) => r.method() === "PUT" && r.url().endsWith("/api/credentials"))
    await page.getByRole("button", { name: "Save login" }).click()
    expect((await sent).postDataJSON()).toEqual({ source: "internetarchive", email: "someone@example.net", password: "pw" })
    await expect(page.getByText("Login rejected", { exact: true })).toBeVisible()
    await expect(page.getByRole("dialog")).toHaveCount(0)
  })

  test("deleting a removed collection's files sends the chosen scope", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_saved", { tab: "settings" })
    await page.getByRole("button", { name: "Delete files…" }).click()
    await expect(page.getByRole("button", { name: "Delete library folders" })).toBeVisible()
    await page.locator("label[for=scope-everything]").click()
    const sent = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/api/collections/purge"))
    await page.getByRole("button", { name: "Delete everything" }).click()
    expect((await sent).postDataJSON()).toEqual({ source: "internetarchive", collection: "dmb", scope: "everything" })
    await expect(page.getByText("Deleting files", { exact: true })).toBeVisible()
  })

  test("Trim history now starts a trim", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_saved", { tab: "settings" })
    await page.getByRole("button", { name: "Trim history now" }).click()
    await expect(page.getByText("Trim history started", { exact: true })).toBeVisible()
  })

  test("a collection row opens its editor", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: "settings" })
    await page.getByRole("link", { name: /phish/ }).click()
    await expect(page).toHaveURL(/#settings\/internetarchive\/phish$/)
  })
})
