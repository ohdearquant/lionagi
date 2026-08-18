import { expect, test } from "@playwright/test";

const SMOKE_SCHEDULE_NAME = "e2e-smoke-nightly-report";
const SMOKE_SESSION_NAME = "e2e-smoke-completed-run";

test.describe("mobile schedule detail controls", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("keeps deletion and firing policies reachable without weakening dirty-close", async ({
    page,
  }) => {
    await page.goto("/schedules");
    const operatorToggle = page.getByRole("button", { name: "Operator (⌘J)", exact: true });
    if ((await operatorToggle.getAttribute("aria-pressed")) === "true") {
      await page.getByRole("button", { name: "Close Operator", exact: true }).last().click();
    }
    await page.getByRole("button", { name: SMOKE_SCHEDULE_NAME, exact: true }).click();

    const dialog = page.getByRole("dialog", { name: SMOKE_SCHEDULE_NAME, exact: true });
    const deleteButton = dialog.getByRole("button", { name: "Delete", exact: true });
    const missedFire = dialog.getByLabel("Missed fire", { exact: true });
    const overlap = dialog.getByLabel("Overlap", { exact: true });

    await expect(deleteButton).toBeVisible();
    await expect(missedFire).toBeVisible();
    await expect(overlap).toBeVisible();
    await expect(deleteButton).toHaveCount(1);
    await expect(missedFire).toHaveCount(1);
    await expect(overlap).toHaveCount(1);

    for (const control of [deleteButton, missedFire, overlap]) {
      const box = await control.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(390);
    }

    await deleteButton.focus();
    await page.keyboard.press("Tab");
    await expect(missedFire).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(overlap).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(missedFire).toBeFocused();

    await missedFire.selectOption("run_once");
    await page.keyboard.press("Escape");
    await expect(dialog.getByRole("alert")).toContainText("You have unsaved changes.");
    await dialog.getByRole("button", { name: "Keep editing", exact: true }).click();

    await deleteButton.click();
    await expect(dialog.getByRole("button", { name: "Confirm delete", exact: true })).toBeVisible();

    await page.keyboard.press("Escape");
    await dialog.getByRole("button", { name: "Discard changes", exact: true }).click();
    await expect(dialog).toBeHidden();
  });
});

test("expanded run graph keeps the keyboard inside it and hands it back", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/fleet");
  await page.getByText(SMOKE_SESSION_NAME, { exact: true }).click();

  const expand = page.getByRole("button", { name: "Expand execution graph", exact: true });
  await expect(expand).toBeVisible();
  const inlineViewport = page.locator("#run-dag .react-flow__viewport").first();
  const inlineTransform = await inlineViewport.getAttribute("style");

  await expand.click();
  const dialog = page.getByRole("dialog", { name: "Execution graph", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(":focus")).toHaveCount(1);

  for (let step = 0; step < 12; step += 1) {
    await page.keyboard.press("Tab");
    await expect(dialog.locator(":focus")).toHaveCount(1);
  }
  for (let step = 0; step < 12; step += 1) {
    await page.keyboard.press("Shift+Tab");
    await expect(dialog.locator(":focus")).toHaveCount(1);
  }

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(expand).toBeFocused();
  await expect(inlineViewport).toHaveAttribute("style", inlineTransform ?? "");

  await expand.click();
  await dialog.getByRole("button", { name: "Collapse execution graph", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(expand).toBeFocused();
});
