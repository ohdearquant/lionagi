import { expect, test } from "@playwright/test";
import { settleRunGraphViewport } from "../src/lib/runGraphViewport";

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
  await expect(page.locator("#run-dag .react-flow__node").first()).toBeVisible();
  // The panel takes the layout's reported height in the same commit that lays the
  // nodes out and this canvas fits the view on the frame that follows, and the
  // panel then resizes the canvas, which fits it once more, so the viewport can
  // hold the view from before those fits for a while. The baseline is taken from
  // the view that holds once every node is drawn — the state React Flow reveals a
  // node in when it has measured it, and fits the view from — and the wait is
  // spent on real frames, which is where the later fits run.
  const inlineTransform = await settleRunGraphViewport(
    () => {
      // The style and the nodes are read in one round trip, so both describe the
      // same frame: read separately, a fit landing between them would report a
      // settled transform beside nodes it drew, and the hold would restart on
      // the next read.
      return inlineViewport.evaluate((viewport) => {
        const nodes = Array.from(
          viewport.closest("#run-dag")?.querySelectorAll(".react-flow__node") ?? [],
        );
        return {
          style: viewport.getAttribute("style"),
          // React Flow holds a node at `visibility: hidden` until it has measured
          // it, and fits the view from the update that measures the last one, so
          // "every node visible" is the graph React Flow has fitted.
          drawn:
            nodes.length > 0 &&
            nodes.every((node) => getComputedStyle(node).visibility !== "hidden"),
        };
      });
    },
    async () => {
      await page.evaluate(
        () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())),
      );
    },
  );

  await expand.click();
  const dialog = page.getByRole("dialog", { name: "Execution graph", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(":focus")).toHaveCount(1);
  // The dialog is inset, so the view behind it stays on screen. The routed surface
  // scrolls in its own container rather than on body, so that container is the one
  // that has to be frozen for the view to actually hold still.
  await expect(page.locator("#main-content")).toHaveCSS("overflow-y", "hidden");

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
  await expect(page.locator("#main-content")).toHaveCSS("overflow-y", "auto");
  await expect(inlineViewport).toHaveAttribute("style", inlineTransform);

  await expand.click();
  await dialog.getByRole("button", { name: "Collapse execution graph", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(expand).toBeFocused();

  // A click in the corner outside the inset dialog lands on the backdrop rather
  // than on whatever the view had painted there, which is what says it is covered.
  await expand.click();
  await expect(dialog).toBeVisible();
  await page.mouse.click(4, 4);
  await expect(dialog).toBeHidden();
});
