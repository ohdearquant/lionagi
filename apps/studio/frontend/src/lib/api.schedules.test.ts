import { afterEach, expect, it, vi } from "vitest";
import { listSchedules } from "./api";

afterEach(() => vi.unstubAllGlobals());

it.each([undefined, false, true])(
  "serializes the recent-run opt-in %s without changing other filters",
  async (include) => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ schedules: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    await listSchedules({ enabled: true, project: "project one", include_recent_runs: include });
    const url = new URL(fetch.mock.calls[0][0], "http://localhost");
    expect(url.searchParams.get("enabled")).toBe("true");
    expect(url.searchParams.get("project")).toBe("project one");
    expect(url.searchParams.get("include_recent_runs")).toBe(
      include === undefined ? null : String(include),
    );
    expect(fetch).toHaveBeenCalledTimes(1);
  },
);

it("keeps the default list URL free of the summary opt-in", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(new Response(JSON.stringify({ schedules: [] }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  await listSchedules();
  expect(String(fetch.mock.calls[0][0])).toMatch(/\/api\/schedules\/$/);
});
