import { test, expect } from "@playwright/test";

// Must match tests/e2e_studio/fixtures.py -- the seeded schedule name asserted
// on below is only ever produced by the seeded daemon's fixtures.
const SMOKE_SCHEDULE_NAME = "e2e-smoke-nightly-report";
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

test("retired workspace URLs resolve into their live consolidated spaces", async ({ page }) => {
  const aliases = [
    ["/designer", /\/library\?tab=workflow/, "Library catalog"],
    ["/canvas", /\/library\?tab=workflow/, "Library catalog"],
    ["/routing", /\/library\?tab=workflow/, "Library catalog"],
    ["/agents", /\/library\?tab=agent/, "Library catalog"],
    ["/history", /\/fleet/, "Session list"],
    ["/outcomes", /\/fleet/, "Session list"],
    ["/shows", /\/fleet/, "Session list"],
    ["/mission", /\/$/, "Mission Control"],
  ] as const;

  for (const [route, destination, landmark] of aliases) {
    await page.goto(route);
    await expect(page).toHaveURL(destination);
    if (landmark === "Mission Control") {
      await expect(page.getByRole("heading", { name: landmark, exact: true })).toBeVisible();
    } else {
      await expect(page.getByRole("region", { name: landmark, exact: true })).toBeVisible();
    }
    await expect(page.getByText("Not Found", { exact: true })).toHaveCount(0);
  }
});

test("primary Studio surfaces render without console failures", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push(err.message));

  for (const route of ["/", "/fleet", "/designer", "/library", "/schedules", "/system"]) {
    await page.goto(route);
    await expect(page.locator("#root")).not.toBeEmpty();
    await expect(page.getByText("Not Found", { exact: true })).toHaveCount(0);
  }

  expect(errors, `console errors:\n${errors.join("\n")}`).toEqual([]);
});

test.describe("narrow Studio shell", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("keeps primary actions and Operator inside the viewport", async ({ page }) => {
    await page.goto("/schedules");
    const operatorToggle = page.getByRole("button", { name: "Operator (⌘J)", exact: true });
    if ((await operatorToggle.getAttribute("aria-pressed")) === "true") {
      await page
        .getByRole("complementary", { name: "Operator conversation", exact: true })
        .getByRole("button", { name: "Close Operator", exact: true })
        .click();
      await expect(operatorToggle).toHaveAttribute("aria-pressed", "false");
    }
    await expect(page.getByText(SMOKE_SCHEDULE_NAME)).toBeVisible();
    const newSchedule = page.getByRole("button", { name: "+ New schedule", exact: true });
    await expect(newSchedule).toBeVisible();
    const scheduleBox = await newSchedule.boundingBox();
    expect(scheduleBox).not.toBeNull();
    expect(scheduleBox!.x).toBeGreaterThanOrEqual(0);
    expect(scheduleBox!.x + scheduleBox!.width).toBeLessThanOrEqual(390);

    await page.goto("/library");
    await expect(
      page
        .getByRole("region", { name: "Item detail", exact: true })
        .getByText("e2e-smoke-reviewer", { exact: true }),
    ).toBeVisible();

    if ((await operatorToggle.getAttribute("aria-pressed")) !== "true") {
      await operatorToggle.click();
    }
    await expect(page.getByLabel("Instruction")).toBeVisible();
    const operator = page.getByRole("complementary", { name: "Operator conversation" });
    const operatorBox = await operator.boundingBox();
    expect(operatorBox).not.toBeNull();
    expect(operatorBox!.x).toBeGreaterThanOrEqual(56);
    expect(operatorBox!.x + operatorBox!.width).toBeLessThanOrEqual(390);
  });
});
