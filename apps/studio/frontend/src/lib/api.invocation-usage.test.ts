import { afterEach, expect, it, vi } from "vitest";
import { listInvocations, type InvocationListResponse } from "./api";

afterEach(() => vi.unstubAllGlobals());

it.each([
  { name: "older server", aggregate: {} },
  { name: "recorded start", aggregate: { latest_started_at: 1234, finished_total: 300 } },
  { name: "no records", aggregate: { latest_started_at: null, finished_total: 0 } },
])("preserves the additive aggregate from a $name response", async ({ aggregate }) => {
  const body: InvocationListResponse = {
    invocations: [],
    limit: 5,
    offset: 10,
    has_next: false,
    total: 320,
    completed_total: 240,
    ...aggregate,
  };
  const fetch = vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetch);
  const response = await listInvocations({
    skill: "skill / one",
    plugin: "package",
    status: "completed",
    limit: 5,
    offset: 10,
  });
  expect(response).toEqual(body);
  expect(fetch).toHaveBeenCalledTimes(1);
  const url = new URL(fetch.mock.calls[0][0], "http://localhost");
  expect(url.pathname).toBe("/api/invocations/");
  expect(Object.fromEntries(url.searchParams)).toEqual({
    skill: "skill / one",
    plugin: "package",
    status: "completed",
    limit: "5",
    offset: "10",
  });
});

it("sends the usage flag only when the caller asks for the aggregates", async () => {
  const body: InvocationListResponse = {
    invocations: [],
    limit: 5,
    offset: 0,
    has_next: false,
    total: 0,
    completed_total: 0,
  };
  // A Response body can be read once, so each call gets its own.
  const fetch = vi.fn().mockImplementation(
    async () =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", fetch);

  await listInvocations({ skill: "one", limit: 5, includeUsage: true });
  const withUsage = new URL(fetch.mock.calls[0][0], "http://localhost");
  expect(withUsage.searchParams.get("include_usage")).toBe("true");

  await listInvocations({ skill: "one", limit: 5 });
  const withoutUsage = new URL(fetch.mock.calls[1][0], "http://localhost");
  expect(withoutUsage.searchParams.has("include_usage")).toBe(false);
});
