import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { useSchedulesData } from "./data";

afterEach(() => vi.unstubAllGlobals());

it("loads 100 definitions through one opted-in fetch and no history fetches", async () => {
  const schedules = Array.from({ length: 100 }, (_, index) => ({
    id: `schedule-${index}`,
    name: `Schedule ${index}`,
    recent_runs_summary: { version: 1, status: "available", limit: 25, has_next: false, runs: [] },
  }));
  const fetch = vi
    .fn()
    .mockResolvedValue(new Response(JSON.stringify({ schedules }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  const container = document.createElement("div");
  const root = createRoot(container);
  function Harness() {
    const data = useSchedulesData();
    return <div>{data.schedules.length}</div>;
  }
  try {
    await act(async () => root.render(<Harness />));
    expect(container.textContent).toBe("100");
    expect(fetch).toHaveBeenCalledTimes(1);
    const url = new URL(fetch.mock.calls[0][0], "http://localhost");
    expect(url.pathname).toBe("/api/schedules/");
    expect(url.searchParams.get("include_recent_runs")).toBe("true");
  } finally {
    act(() => root.unmount());
  }
});
