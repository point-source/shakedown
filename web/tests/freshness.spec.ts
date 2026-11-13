// Behaviour test (no screenshot): "Can't reach Shakedown" depends on whether
// answers keep arriving, not on how the server's clock compares with the
// browser's (§spec:dashboard "Old data is labelled").
import { expect, test } from "playwright/test"
import { openScenario } from "../scripts/fixture-page.mjs"

test("a server clock behind the browser's is not 'can't reach'", async ({ page, baseURL }, info) => {
  test.skip(info.project.name !== "1440-light", "behaviour, not layout: one project is enough")
  await openScenario(page, baseURL!, "server_clock_behind")
  await page.clock.runFor(12_000)
  await expect(page.getByText("Mirrored", { exact: true })).toBeVisible()
  await expect(page.getByText("Can't reach Shakedown")).toHaveCount(0)
})
