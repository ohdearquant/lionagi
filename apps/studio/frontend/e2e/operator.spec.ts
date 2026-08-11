import { expect, test, type Page } from "@playwright/test";

async function createConversation(page: Page): Promise<string> {
  const title = await page.evaluate(() => crypto.randomUUID());
  await page.evaluate(async (conversationTitle) => {
    const response = await fetch("/api/operator/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: conversationTitle }),
    });
    if (!response.ok) {
      throw new Error(`Failed to create an isolated conversation: ${response.status}`);
    }
  }, title);
  return title;
}

async function selectFreshConversation(page: Page): Promise<void> {
  // The conversation switcher is a disclosure button ("Conversations") that
  // reveals a list of conversation rows, each a labeled button rather than a
  // <select>/<option> pair. The fixture conversation is created with a unique
  // title so its row can be found by accessible name instead of relying on a
  // DOM identifier the UI no longer exposes.
  const title = await createConversation(page);

  await page.reload();
  // The trigger's accessible name carries the selected conversation's title
  // after it (e.g. "Conversations: My chat"), so match on the stable
  // "Conversations" prefix rather than the whole string.
  const toggle = page.getByRole("button", { name: /^Conversations/ });
  await toggle.click();
  const row = page.getByRole("button", { name: title, exact: true });
  await expect(row).toHaveCount(1);
  await row.click();
  await expect(page.getByLabel("Instruction")).toBeEnabled();
}

test("Operator streams, persists, stops, records a run, and resumes it", async ({ page }) => {
  test.setTimeout(45_000);
  const discovery = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().endsWith("/api/operator/conversations"),
  );
  await page.goto("/");
  expect((await discovery).status()).toBe(200);

  // The daemon survives individual browser contexts and Playwright retries.
  // Start from an explicit new conversation so the deterministic engine script
  // is never coupled to history left by a previous attempt.
  await selectFreshConversation(page);
  const instruction = page.getByLabel("Instruction");
  await expect(instruction).toBeVisible();
  await instruction.fill("Show me the fleet readiness.");
  await page.getByRole("button", { name: "Send", exact: true }).click();

  await expect(page.getByText("Show me the fleet readiness.", { exact: true })).toBeVisible();
  await expect(page.getByText("Fleet ready.", { exact: true })).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText("Turn completed", { exact: true })).toBeVisible();

  const openRun = page.locator('a[href*="/fleet?s="]').filter({ hasText: "Open run" });
  await expect(openRun).toHaveCount(1);
  const runHref = await openRun.getAttribute("href");
  expect(runHref).toMatch(/^\/fleet\?s=.+/);

  await page.reload();
  await expect(page.getByText("Show me the fleet readiness.", { exact: true })).toBeVisible();
  await expect(page.getByText("Fleet ready.", { exact: true })).toBeVisible();

  await instruction.fill("wait until I stop you");
  await page.getByRole("button", { name: "Send", exact: true }).click();
  const stop = page.getByRole("button", { name: "Stop", exact: true });
  await expect(stop).toBeVisible();
  await stop.click();
  await expect(page.getByText("Turn stopped", { exact: true })).toBeVisible({
    timeout: 10_000,
  });

  await page.goto(runHref!);
  await expect(page).toHaveURL(/\/fleet\?s=.+/);
  await expect(page.getByRole("region", { name: "Continue this run" })).toBeVisible();

  const followUp = page.getByLabel("Follow-up instruction");
  await followUp.fill("Continue with the next check.");
  const activityPoll = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" && /\/api\/invocations\/[^/?]+$/.test(response.url()),
  );
  await page.getByRole("button", { name: "Resume", exact: true }).click();
  await expect(page.getByText("Follow-up accepted", { exact: true })).toBeVisible({
    timeout: 15_000,
  });
  expect((await activityPoll).status()).toBe(200);

  const activityLink = page.locator('a[href*="invocation="]').filter({ hasText: "View activity" });
  await expect(activityLink).toHaveCount(1);
  expect(await activityLink.getAttribute("href")).toMatch(/^\/fleet\?s=.+&invocation=.+/);
  await activityLink.click();
  await expect(page).toHaveURL(/\/fleet\?s=.+&invocation=.+/);

  // Acceptance is not enough: the detached CLI leg must reopen the exact
  // branch, execute, and flow its new durable message back into the run pane.
  const main = page.getByRole("main", { name: "Main content" });
  await expect(main.getByText("Continuation complete.", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await main.getByRole("tab", { name: "Conversation tab 5 of 5", exact: true }).click();
  await expect(main.getByText("Continue with the next check.", { exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await page.reload();
  await expect(main.getByText("Continuation complete.", { exact: true })).toBeVisible({
    timeout: 20_000,
  });
  await main.getByRole("tab", { name: "Conversation tab 5 of 5", exact: true }).click();
  await expect(main.getByText("Continue with the next check.", { exact: true })).toBeVisible({
    timeout: 20_000,
  });
});

test("Operator Deny and Allow decisions traverse the real permission route", async ({ page }) => {
  test.setTimeout(45_000);
  // Decisions go over the real permission route with no credential of any kind:
  // Studio is a loopback-local tool and carries no browser-held token.
  const discovery = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().endsWith("/api/operator/conversations"),
  );
  await page.goto("/");
  expect((await discovery).status()).toBe(200);

  await selectFreshConversation(page);
  const instruction = page.getByLabel("Instruction");

  await instruction.fill("Request a gated demo action and wait for me to deny it.");
  await page.getByRole("button", { name: "Send", exact: true }).click();

  const deniedProposal = page.getByRole("region", {
    name: "Action requiring permission",
    exact: true,
  });
  await expect(deniedProposal).toBeVisible({ timeout: 15_000 });
  // No click to reveal: the command must be on screen before Allow/Deny are reachable,
  // so asserting it directly is the contract. Re-adding a click here would collapse the
  // disclosure and assert the opposite of what this test is for.
  await expect(deniedProposal.getByText('"operation": "record_permission_decision"')).toBeVisible();

  const deniedResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/api\/operator\/conversations\/[^/]+\/proposals\/[^/]+\/decision$/.test(response.url()),
  );
  await deniedProposal.getByRole("button", { name: "Deny", exact: true }).click();
  const deniedResponse = await deniedResponsePromise;
  expect(deniedResponse.status()).toBe(200);
  expect(deniedResponse.request().postDataJSON()).toMatchObject({ decision: "deny" });
  await expect(deniedResponse.json()).resolves.toMatchObject({ status: "failed" });
  await expect(page.getByText("Action cancelled", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Demo action denied. Nothing was changed.", { exact: true }),
  ).toBeVisible();
  const completedTurns = page.getByText("Turn completed", { exact: true });
  await expect(completedTurns).toBeVisible();
  const completedBeforeAllow = await completedTurns.count();

  await instruction.fill("Request a gated demo action and wait for me to allow it.");
  await page.getByRole("button", { name: "Send", exact: true }).click();

  const proposals = page.getByRole("region", {
    name: "Action requiring permission",
    exact: true,
  });
  await expect(proposals).toHaveCount(2, { timeout: 15_000 });
  const allowedProposal = proposals.filter({
    has: page.getByRole("button", { name: "Allow", exact: true }),
  });
  await expect(allowedProposal).toHaveCount(1);

  const allowedResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/api\/operator\/conversations\/[^/]+\/proposals\/[^/]+\/decision$/.test(response.url()),
  );
  await allowedProposal.getByRole("button", { name: "Allow", exact: true }).click();
  const allowedResponse = await allowedResponsePromise;
  expect(allowedResponse.status()).toBe(200);
  expect(allowedResponse.request().postDataJSON()).toMatchObject({
    decision: "allow",
    expectedCommandHash: expect.stringMatching(/^[a-f0-9]{64}$/),
  });
  await expect(allowedResponse.json()).resolves.toMatchObject({
    status: "succeeded",
    result: { executed: true },
  });
  await expect(page.getByText("Approved action completed", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Demo action allowed and completed safely.", { exact: true }),
  ).toBeVisible();
  await expect(completedTurns).toHaveCount(completedBeforeAllow + 1);

  // Both decisions and outcomes are durable daemon history, not client-only UI.
  await page.reload();
  await expect(
    page.getByText("Demo action denied. Nothing was changed.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Demo action allowed and completed safely.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Action requiring permission", exact: true }),
  ).toHaveCount(2);
  await expect(page.getByRole("button", { name: "Allow", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Deny", exact: true })).toHaveCount(0);
});

test("a conversation can be renamed using only the keyboard", async ({ page }) => {
  test.setTimeout(30_000);
  await page.goto("/");
  const title = await createConversation(page);
  await page.reload();

  const toggle = page.getByRole("button", { name: /^Conversations/ });
  await toggle.click();
  const row = page.getByRole("button", { name: title, exact: true });
  await expect(row).toHaveCount(1);

  // Reach the rename control by accessible name and activate it with the
  // keyboard, the way a screen-reader or keyboard-only user would — never a
  // pointer double-click.
  const renamed = `${title}-renamed`;
  const renameButton = page.getByRole("button", { name: `Rename ${title}`, exact: true });
  await renameButton.focus();
  await page.keyboard.press("Enter");

  const input = page.getByPlaceholder("Conversation title");
  await expect(input).toBeFocused();
  await input.fill(renamed);
  await page.keyboard.press("Enter");

  await expect(page.getByRole("button", { name: renamed, exact: true })).toBeVisible();
});

test("the conversation trigger's accessible name announces the selection", async ({ page }) => {
  await page.goto("/");
  await selectFreshConversation(page);

  // selectFreshConversation generates its own title, so read the selected
  // name back off the trigger rather than hardcoding it. toHaveAttribute
  // auto-retries, unlike a one-shot getAttribute() read, so this doesn't
  // race the render that follows conversation selection.
  const trigger = page.getByRole("button", { name: /^Conversations/ });
  await expect(trigger).toHaveAttribute("aria-label", /^Conversations: .+/);
  const closedName = await trigger.getAttribute("aria-label");

  await trigger.click();
  await expect(trigger).toHaveAttribute("aria-expanded", "true");
  await expect(trigger).toHaveAttribute("aria-label", closedName!);
});
