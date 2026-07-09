import { test, expect } from "@playwright/test";

// Must match tests/e2e_studio/fixtures.py -- the seeded schedule name asserted
// on below is only ever produced by the seeded daemon's fixtures.
const SMOKE_SCHEDULE_NAME = "e2e-smoke-nightly-report";

// index.html loads the analytics script from an external host. A deferred
// script participates in the window load event, so a slow or unreachable
// external fetch stalls page.goto past the test timeout on CI runners.
// Fulfill (not abort: a failed resource load is itself a console error,
// which the boot test asserts against) so the suite never touches the
// network beyond the app under test.
test.beforeEach(async ({ page }) => {
  await page.route("https://analytics.khive.ai/**", (route) =>
    route.fulfill({ status: 200, contentType: "application/javascript", body: "" }),
  );
});

test("app boots, root renders, and the page logs no console errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push(err.message));

  await page.goto("/");
  await expect(page.locator("#root")).not.toBeEmpty();
  expect(errors, `console errors:\n${errors.join("\n")}`).toEqual([]);
});

test("schedules page renders data that only the seeded db could supply", async ({ page }) => {
  await page.goto("/schedules");
  await expect(page.getByText(SMOKE_SCHEDULE_NAME)).toBeVisible();
});

test("direct URL navigation renders /schedules and /agents, never a blank screen", async ({
  page,
}) => {
  for (const route of ["/schedules", "/agents"]) {
    await page.goto(route);
    const rootText = (await page.locator("#root").innerText()).trim();
    expect(rootText.length, `${route} rendered a blank page`).toBeGreaterThan(0);
  }
});
