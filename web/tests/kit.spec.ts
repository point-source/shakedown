import { expect, test } from "playwright/test"

test("kit sheet", async ({ page }) => {
  await page.goto("./?view=kit")
  await page.evaluate(() => document.fonts.ready)
  await expect(page).toHaveScreenshot("kit.png", { fullPage: true })
})

// The confirm dialog and the toast, open over the sheet (Design round S1, Q-S1).
test("kit sheet overlays", async ({ page }) => {
  await page.goto("./?view=kit&overlay=1")
  await page.evaluate(() => document.fonts.ready)
  await expect(page.getByRole("alertdialog")).toBeVisible()
  await expect(page).toHaveScreenshot("kit-overlay.png")
})
