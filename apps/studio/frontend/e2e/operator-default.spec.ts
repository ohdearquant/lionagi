import { expect, test } from "@playwright/test";

// The Operator dock's default visibility is responsive: a fresh session (no
// persisted choice) opens the dock only when the window is wide enough to hold
// it without collapsing the SplitPane beside it. These tests pin that contract
// in a real browser, at both sides of the threshold, so a change to either the
// threshold or the mount behavior shows up here rather than as a silent
// timeout in the Operator flow suite.

test.beforeEach(async ({ page }) => {
  await page.route("https://analytics.khive.ai/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/javascript", body: "" }),
  );
});

test.describe("fresh session below the default-open width", () => {
  test.use({ viewport: { width: 1280, height: 720 } });

  test("does not mount the Operator dock", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("button", { name: /Operator/ })).toBeVisible();
    await expect(page.getByRole("complementary", { name: "Operator conversation" })).toHaveCount(0);
  });
});

test.describe("fresh session at the default-open width", () => {
  test.use({ viewport: { width: 1364, height: 900 } });

  test("mounts the Operator dock", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("complementary", { name: "Operator conversation" })).toHaveCount(1);
  });
});
