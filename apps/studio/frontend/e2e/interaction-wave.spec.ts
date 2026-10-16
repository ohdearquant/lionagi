import { expect, test } from "@playwright/test";

const SMOKE_SCHEDULE_NAME = "e2e-smoke-nightly-report";
const SMOKE_SESSION_NAME = "e2e-smoke-completed-run";

// React Flow's viewport before any fit has run: with no defaultViewport given,
// the store starts at transform [0, 0, 1], which the canvas serializes into the
// viewport's style as `translate(0px,0px) scale(1)`.
const initialViewport = { x: 0, y: 0, zoom: 1 };

type ViewportTransform = { x: number; y: number; zoom: number };

/** The run graph's viewport translate and scale, parsed out of its style. */
function viewportTransform(style: string): ViewportTransform {
  const parts = /translate\(([^,]+),\s*([^)]+)\)\s*scale\(([^)]+)\)/.exec(style);
  if (parts === null) {
    throw new Error(`run graph viewport has no transform: ${style}`);
  }
  const [x, y, zoom] = [parts[1], parts[2], parts[3]].map((part) => Number.parseFloat(part));
  if (![x, y, zoom].every(Number.isFinite)) {
    throw new Error(`run graph viewport has an unparseable transform: ${style}`);
  }
  return { x, y, zoom };
}

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
  // The panel grows to the layout's reported height in the same commit that lays
  // the nodes out, and React Flow fits the view in the call that measures those
  // nodes; this canvas fits once more on a later animation frame. The nodes can
  // therefore be drawn before that second fit has run, while the viewport is still
  // on its unfitted initial transform, so take the baseline only once the transform
  // has both moved off that initial viewport and held still between two reads.
  await expect(page.locator("#run-dag .react-flow__node").first()).toBeVisible();
  let lastRead: ViewportTransform | null = null;
  await expect
    .poll(
      async () => {
        const style = await inlineViewport.getAttribute("style");
        const fitted = style === null ? null : viewportTransform(style);
        if (fitted === null) {
          lastRead = null;
          return false;
        }
        const movedOffInitial =
          fitted.x !== initialViewport.x ||
          fitted.y !== initialViewport.y ||
          fitted.zoom !== initialViewport.zoom;
        const held =
          lastRead !== null &&
          fitted.x === lastRead.x &&
          fitted.y === lastRead.y &&
          fitted.zoom === lastRead.zoom;
        lastRead = fitted;
        return movedOffInitial && held;
      },
      { intervals: [250] },
    )
    .toBe(true);
  const inlineTransform = await inlineViewport.getAttribute("style");
  expect(inlineTransform).not.toBeNull();

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
  await expect(inlineViewport).toHaveAttribute("style", inlineTransform!);

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
