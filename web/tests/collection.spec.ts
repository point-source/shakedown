// The collection editor inside Settings (§spec:dashboard-settings, §spec:schedules,
// §spec:collection-removal; Design round S3): the editor in each state as
// goldens, and what each action does as behaviour tests.
import { expect, test, type Page } from "playwright/test"
import { openScenario } from "../scripts/fixture-page.mjs"

const GD = "settings/internetarchive/grateful-dead"
const sched = "#set-sources\\.0\\.collections\\.0\\.schedule"
const putBody = async (page: Page, act: () => Promise<void>) => {
  const sent = page.waitForRequest((r) => r.method() === "PUT" && r.url().endsWith("/api/config"))
  await act()
  return (await sent).postDataJSON()
}

const PAGES: [string, string, string][] = [
  ["editor", "default", GD],
  ["new", "default", "settings/internetarchive?add"],
  ["add-back", "default", "settings/internetarchive?add=dmb"],
  ["manual-command", "settings_variety", "settings/internetarchive/phish"],
  ["custom", "settings_variety", "settings/etree-lma/etree-lma"],
  ["variety-list", "settings_variety", "settings"],
  ["invalid", "settings_invalid", "settings/internetarchive/phish"],
  ["locked", "settings_locked", GD],
]
for (const [name, scenario, tab] of PAGES) {
  test(`collection ${name}`, async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, scenario, { tab })
    await expect(page).toHaveScreenshot(`collection-${name}.png`, { fullPage: true })
  })
}

const NEW_QUERY = "#set-sources\\.0\\.collections\\.2\\.query"
// Interaction states, shot at the viewport (menus, dialogs and toasts sit over the page).
const STATES: { name: string; scenario: string; tab: string; go: (page: Page) => Promise<void> }[] = [
  { name: "daily", scenario: "default", tab: GD, go: async (page) => {
    await page.locator(`${sched}-kind`).click()
    await page.getByRole("option", { name: "Daily" }).click()
    await expect(page.locator(`${sched}-time`)).toBeVisible()
  } },
  { name: "remove", scenario: "settings_clean", tab: GD, go: async (page) => {
    await page.getByRole("button", { name: "Remove collection…" }).click()
    await page.locator("label[for=remove-everything]").click()
    await expect(page.getByRole("button", { name: "Remove, delete everything" })).toBeVisible()
  } },
  { name: "remove-busy", scenario: "settings_remove_busy", tab: "settings/internetarchive/phish", go: async (page) => {
    await page.getByRole("button", { name: "Remove collection…" }).click()
    await page.locator("label[for=remove-library]").click()
    await page.getByRole("button", { name: "Remove, delete library folders" }).click()
    await expect(page.getByText("Removed phish, files kept", { exact: true })).toBeVisible()
  } },
  { name: "add-source", scenario: "settings_clean", tab: "settings", go: async (page) => {
    await page.getByRole("button", { name: "Add source" }).click()
    await page.locator("#source-name").fill("internetarchive")
    await expect(page.getByText("There's already a source called internetarchive.")).toBeVisible()
  } },
  { name: "link-read", scenario: "default", tab: "settings/internetarchive?add", go: async (page) => {
    await page.locator("#set-sources\\.0\\.collections\\.2\\.name").fill("bobby")
    await page.locator(NEW_QUERY).fill("https://archive.org/details/BobWeir")
    await expect(page.locator(NEW_QUERY)).toHaveValue("collection:BobWeir")
  } },
  { name: "link-unreadable", scenario: "default", tab: "settings/internetarchive?add", go: async (page) => {
    await page.locator("#set-sources\\.0\\.collections\\.2\\.name").fill("bobby")
    await page.locator(NEW_QUERY).fill("https://example.net/bob-weir")
    await expect(page.getByText("Shakedown can't read this link for this source.", { exact: false })).toBeVisible()
  } },
]
for (const s of STATES) {
  test(`collection ${s.name}`, async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, s.scenario, { tab: s.tab })
    await s.go(page)
    await expect(page).toHaveScreenshot(`collection-${s.name}.png`)
  })
}

test.describe("behaviour", () => {
  test.beforeEach(({}, info) => {
    test.skip(info.project.name !== "1440-light", "behaviour, not layout: one project is enough")
  })

  test("a preset is written as its cron expression, and Manual only removes it", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_clean", { tab: GD })
    await page.locator(`${sched}-kind`).click()
    await page.getByRole("option", { name: "Daily" }).click()
    await page.locator(`${sched}-time`).fill("03:30")
    let body = await putBody(page, () => page.getByRole("button", { name: "Save", exact: true }).click())
    expect(body.document.sources[0].collections[0].schedule).toBe("30 3 * * *")
    await page.locator(`${sched}-kind`).click()
    await page.getByRole("option", { name: "Manual only" }).click()
    await expect(page.getByText("Syncs only when someone presses Sync now.")).toBeVisible()
    body = await putBody(page, () => page.getByRole("button", { name: "Save", exact: true }).click())
    expect(body.document.sources[0].collections[0].schedule).toBeNull()
  })

  test("Custom keeps an expression that reads as a preset", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: GD })
    await page.locator(`${sched}-kind`).click()
    await page.getByRole("option", { name: "Custom" }).click()
    await expect(page.locator(sched)).toHaveValue("0 3 * * 0")
    await expect(page.locator(`${sched}-kind`)).toHaveText("Custom")
    await expect(page.getByText("Next sync 15 Nov, 03:00")).toBeVisible()
  })

  test("a time that isn't one says so and changes nothing", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: GD })
    await page.locator(`${sched}-time`).fill("3pm")
    await expect(page.getByText("Enter a 24-hour time, such as 03:30.")).toBeVisible()
    await expect(page.getByText("Next sync 15 Nov, 03:00")).toBeVisible()
  })

  test("Discard puts the time back in the Time field", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: GD })
    await page.locator(`${sched}-time`).fill("04:15")
    await page.getByRole("button", { name: "Discard" }).click()
    await expect(page.locator(`${sched}-time`)).toHaveValue("03:00")
  })

  test("a schedule the file holds as a number shows as Custom, with its error", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_invalid_schedule", { tab: "settings" })
    await expect(page.getByText("Custom: 5")).toBeVisible()
    await page.evaluate((h) => { location.hash = h }, GD)
    await expect(page.locator(sched)).toHaveValue("5")
    await expect(page.getByText("This value isn't the right kind for this setting.")).toBeVisible()
  })

  test("a new collection gets the staggered schedule, saves, and opens under its name", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_clean", { tab: "settings/internetarchive?add" })
    await expect(page.locator(`${sched.replace("collections\\.0", "collections\\.2")}-time`)).toHaveValue("06:00")
    await page.locator("#set-sources\\.0\\.collections\\.2\\.name").fill("bobby")
    await page.locator("#set-sources\\.0\\.collections\\.2\\.query").fill("collection:BobWeir")
    const body = await putBody(page, () => page.getByRole("button", { name: "Save", exact: true }).click())
    expect(body.document.sources[0].collections[2]).toMatchObject({
      name: "bobby", query: "collection:BobWeir", schedule: "0 6 * * 0", library_layout: "passthrough", on_complete: null,
    })
    await expect(page).toHaveURL(/#settings\/internetarchive\/bobby$/)
    await expect(page.getByRole("heading", { name: "bobby" })).toBeVisible()
    await expect(page.getByText("Fixed once created", { exact: false })).toBeVisible()
  })

  test("Discard on a new collection drops it and goes back to Settings", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: "settings/internetarchive?add" })
    await page.getByRole("button", { name: "Discard" }).click()
    await expect(page).toHaveURL(/#settings$/)
    await expect(page.getByText("Not saved yet")).toHaveCount(0)
  })

  test("Add back fills the name and says the shows are reused", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: "settings" })
    await page.getByRole("link", { name: "Add back" }).click()
    await expect(page.locator("#set-sources\\.0\\.collections\\.2\\.name")).toHaveValue("dmb")
    await expect(page.getByText("Shakedown still holds 312 shows (912 GB) for dmb.", { exact: false })).toBeVisible()
  })

  test("a cleared webhook saves as no handoff", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_clean", { tab: GD })
    await page.locator("#set-sources\\.0\\.collections\\.0\\.on_complete\\.webhook").fill("")
    const body = await putBody(page, () => page.getByRole("button", { name: "Save", exact: true }).click())
    expect(body.document.sources[0].collections[0].on_complete).toBeNull()
  })

  test("a command handoff is shown, not edited", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_variety", { tab: "settings/internetarchive/phish" })
    await expect(page.getByText("/scripts/beets-import.sh {staging_root}")).toBeVisible()
    await expect(page.locator("#set-sources\\.0\\.collections\\.1\\.on_complete\\.webhook")).toHaveCount(0)
  })

  test("removing saves the file without it, then starts the delete chosen", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_clean", { tab: "settings/internetarchive/phish" })
    await page.getByRole("button", { name: "Remove collection…" }).click()
    await page.locator("label[for=remove-library]").click()
    const purge = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/api/collections/purge"))
    const body = await putBody(page, () => page.getByRole("button", { name: "Remove, delete library folders" }).click())
    expect(body.document.sources[0].collections.map((c: { name: string }) => c.name)).toEqual(["grateful-dead"])
    expect((await purge).postDataJSON()).toEqual({ source: "internetarchive", collection: "phish", scope: "library" })
    await expect(page).toHaveURL(/#settings$/)
    await expect(page.getByText("Removed phish", { exact: true })).toBeVisible()
  })

  test("removing with files kept starts no delete", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_clean", { tab: "settings/internetarchive/phish" })
    let purged = false
    page.on("request", (r) => { if (r.url().endsWith("/api/collections/purge")) purged = true })
    await page.getByRole("button", { name: "Remove collection…" }).click()
    await page.getByRole("button", { name: "Remove, keep files" }).click()
    await expect(page.getByText("Its files stay, listed under Removed collections.")).toBeVisible()
    expect(purged).toBe(false)
  })

  test("a delete a run blocks leaves the collection removed with its files kept", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_remove_busy", { tab: "settings/internetarchive/phish" })
    await page.getByRole("button", { name: "Remove collection…" }).click()
    await page.locator("label[for=remove-everything]").click()
    await page.getByRole("button", { name: "Remove, delete everything" }).click()
    await expect(page.getByText("Removed phish, files kept", { exact: true })).toBeVisible()
  })

  test("Add source adds it and opens its first collection", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_clean", { tab: "settings" })
    await page.getByRole("button", { name: "Add source" }).click()
    await page.locator("#source-name").fill("internetarchive")
    await expect(page.getByText("There's already a source called internetarchive.")).toBeVisible()
    await page.locator("#source-name").fill("lma")
    await page.getByRole("dialog").getByRole("button", { name: "Add source" }).click()
    await expect(page).toHaveURL(/#settings\/lma\?add$/)
    await page.locator("#set-sources\\.2\\.collections\\.0\\.name").fill("lma-all")
    await page.locator("#set-sources\\.2\\.collections\\.0\\.query").fill("collection:etree")
    const body = await putBody(page, () => page.getByRole("button", { name: "Save", exact: true }).click())
    expect(body.document.sources[2]).toMatchObject({ name: "lma", type: "etree", collections: [{ name: "lma-all" }] })
  })

  test("a locked file's collection shows as text with no Remove", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "settings_locked", { tab: GD })
    await expect(page.getByText("Weekly, Sunday at 03:00")).toBeVisible()
    await expect(page.locator("main input, main textarea")).toHaveCount(0)
    await expect(page.getByRole("button", { name: "Remove collection…" })).toHaveCount(0)
  })

  test("an unknown collection says so", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: "settings/internetarchive/nope" })
    await expect(page.getByText("There's no collection called nope in internetarchive.")).toBeVisible()
  })
})

test.describe("pasted links", () => {
  test.beforeEach(({}, info) => {
    test.skip(info.project.name !== "1440-light", "behaviour, not layout: one project is enough")
  })
  const q = "#set-sources\\.0\\.collections\\.0\\.query"
  for (const [link, query] of [
    ["https://archive.org/details/GratefulDead", "collection:GratefulDead"],
    ["https://archive.org/details/etree?query=phish&tab=collection", "collection:etree AND (phish)"],
    ["https://archive.org/search?query=collection%3APhish+AND+year%3A1997", "collection:Phish AND year:1997"],
    ["https://archive.org/advancedsearch.php?q=creator%3A%22Grateful+Dead%22&fl%5B%5D=identifier", "creator:\"Grateful Dead\""],
  ]) {
    test(`a pasted ${new URL(link).pathname} link becomes its search`, async ({ page, baseURL }) => {
      await openScenario(page, baseURL!, "default", { tab: "settings/internetarchive/grateful-dead" })
      await page.locator(q).fill(link)
      await expect(page.locator(q)).toHaveValue(query)
      await expect(page.getByText("Read from the link you pasted. Check it before you save.")).toBeVisible()
    })
  }
  test("a link is read by the source's plugin, not the page", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: "settings/internetarchive/grateful-dead" })
    const asked = page.waitForRequest((r) => r.url().includes("/api/source-link?"))
    await page.locator(q).fill("https://archive.org/details/BobWeir")
    const url = new URL((await asked).url())
    expect(Object.fromEntries(url.searchParams)).toEqual({ type: "ia", url: "https://archive.org/details/BobWeir" })
    await expect(page.locator(q)).toHaveValue("collection:BobWeir")
  })
  test("a reading that comes back after Discard doesn't undo it", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: "settings/internetarchive/grateful-dead" })
    const before = await page.locator(q).inputValue()
    let release!: () => void
    const held = new Promise<void>((r) => { release = r })
    await page.route("**/api/source-link**", async (route) => { await held; await route.fallback() })
    const answered = page.waitForResponse((r) => r.url().includes("/api/source-link"))
    await page.locator(q).fill("https://archive.org/details/BobWeir")
    await page.getByRole("button", { name: "Discard" }).click()
    release()
    await answered
    await expect(page.locator(q)).toHaveValue(before)
    await expect(page.getByText("Unsaved changes")).toHaveCount(0)
  })

  test("a search that isn't a link is never sent for reading", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: "settings/internetarchive/grateful-dead" })
    let asked = false
    page.on("request", (r) => { if (r.url().includes("/api/source-link")) asked = true })
    await page.locator(q).fill("collection:GratefulDead AND year:1977")
    await expect(page.getByText("Paste a collection or search link from the source's site, or type the search.")).toBeVisible()
    expect(asked).toBe(false)
  })
  test("a read that fails keeps the link and says to try again", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: "settings/internetarchive/grateful-dead" })
    await page.route("**/api/source-link?**", (route) => route.fulfill({ status: 500, body: "{}" }))
    await page.locator(q).fill("https://archive.org/details/BobWeir")
    await expect(page.getByText("Shakedown couldn't read this link just now.", { exact: false })).toBeVisible()
    await expect(page.locator(q)).toHaveValue("https://archive.org/details/BobWeir")
  })
  test("a link Shakedown can't read stays as typed and says so", async ({ page, baseURL }) => {
    await openScenario(page, baseURL!, "default", { tab: "settings/internetarchive/grateful-dead" })
    await page.locator(q).fill("https://example.net/shows")
    await expect(page.locator(q)).toHaveValue("https://example.net/shows")
    await expect(page.getByText("Shakedown can't read this link for this source.", { exact: false })).toBeVisible()
  })
})
