import { expect, test } from "playwright/test"

test("kit sheet", async ({ page }) => {
  await page.goto("./?view=kit")
  await page.evaluate(() => document.fonts.ready)
  await expect(page).toHaveScreenshot("kit.png", { fullPage: true })
})
