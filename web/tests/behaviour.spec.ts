// Behaviour tests (no screenshots) for states the screenshots do not reach.
// One project is enough: these check behaviour, not layout.
import { expect, test } from "playwright/test"
import { openScenario } from "../scripts/fixture-page.mjs"

test.beforeEach(({}, info) => {
  test.skip(info.project.name !== "1440-light", "behaviour, not layout: one project is enough")
})

test("a running verify with no download facts still draws", async ({ page, baseURL }) => {
  await openScenario(page, baseURL!, "verify_running")
  const running = page.locator('[data-slot="card"]').filter({ has: page.getByText("Running now", { exact: true }) })
  await expect(running.getByText("· Verify")).toHaveCount(1)
})

test("trim runs draw on the existing rows, and a trimmed run shows no error lines", async ({ page, baseURL }) => {
  await openScenario(page, baseURL!, "trims")
  const running = page.locator('[data-slot="card"]').filter({ has: page.getByText("Running now", { exact: true }) })
  await expect(running.getByText("· Trim history")).toHaveCount(1)
  await expect(running.getByText("—", { exact: true })).toHaveCount(1)
  await expect(running.getByText("Started 21:32 · 8 min ago")).toBeVisible()
  for (const fact of ["Shows", "Time left", "Downloaded"]) await expect(running.getByText(fact, { exact: true })).toHaveCount(0)
  await page.getByRole("tab", { name: "Runs" }).click()
  const table = page.locator("table")
  await expect(table.getByText("Trim history", { exact: true })).toHaveCount(3)
  await expect(table.getByText("compaction failed: database or disk is full")).toBeVisible()
  await expect(page.getByText("lma-ph2003-07-15: checksum mismatch after 3 attempts")).toHaveCount(0)
})

test("purge runs draw on the existing rows, with their collection and errors", async ({ page, baseURL }) => {
  await openScenario(page, baseURL!, "purges")
  const running = page.locator('[data-slot="card"]').filter({ has: page.getByText("Running now", { exact: true }) })
  await expect(running.getByText("· Delete files")).toHaveCount(1)
  await expect(running.getByText("phish", { exact: true })).toHaveCount(1)
  await expect(running.getByText("Started 21:34 · 6 min ago")).toBeVisible()
  for (const fact of ["Shows", "Time left", "Downloaded"]) await expect(running.getByText(fact, { exact: true })).toHaveCount(0)
  await expect(running.getByText(/No progress for/)).toHaveCount(0)
  await page.getByRole("tab", { name: "Runs" }).click()
  const table = page.locator("table")
  await expect(table.getByText("Delete files", { exact: true })).toHaveCount(3)
  await expect(table.getByText(/notes\.txt: could not delete: Permission denied/)).toBeVisible()
})

test("a serve that restarts with a broken config says so and keeps the data", async ({ page, baseURL }) => {
  await openScenario(page, baseURL!, "restarts_broken")
  await expect(page.getByText("shakedown.yaml has an error")).toBeVisible()
  await expect(page.getByText("Mirrored", { exact: true })).toBeVisible()
})

test("an endpoint that fails before its first answer is not passed off as fine", async ({ page, baseURL }) => {
  await openScenario(page, baseURL!, "library_fails")
  await expect(page.getByText("Can't reach Shakedown")).toBeVisible()
})

test("a disappeared show with no recorded date has no date", async ({ page, baseURL }) => {
  await openScenario(page, baseURL!, "removed_undated", { tab: "library" })
  await expect(page.getByText("gd1969-02-27.undated")).toBeVisible()
  await expect(page.getByText(/Invalid Date/)).toHaveCount(0)
})

test("'the last run ended' names the run that finished last", async ({ page, baseURL }) => {
  await openScenario(page, baseURL!, "quiet_finish_order")
  await expect(page.getByText("The last run ended 5 min ago.", { exact: false })).toBeVisible()
})

test("Back returns to the previous tab", async ({ page, baseURL }) => {
  await openScenario(page, baseURL!, "default")
  await page.getByRole("tab", { name: "Runs" }).click()
  await page.getByRole("tab", { name: "Sources" }).click()
  await page.goBack()
  await expect(page.getByRole("tab", { name: "Runs" })).toHaveAttribute("aria-selected", "true")
})

test("a run's progress bar tells assistive tech its value", async ({ page, baseURL }) => {
  await openScenario(page, baseURL!, "default")
  await expect(page.getByRole("progressbar").first()).toHaveAttribute("aria-valuenow", /\d/)
})
