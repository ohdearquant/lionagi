/**
 * System page — admin events section source contract.
 *
 * GET /admin/events is a filterable, working backend route with no client
 * function and no UI before this change. Component wiring is asserted
 * against the source (this project has no @testing-library/react — see
 * SchedulesTable.test.tsx); the API client shape itself is exercised
 * directly against a mocked fetch.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import * as fs from "node:fs";
import * as path from "node:path";

const SRC = fs.readFileSync(path.resolve(__dirname, "system.tsx"), "utf-8");

describe("system.tsx — AdminEventsSection source contract", () => {
  it("renders the section and wires it into the page", () => {
    expect(SRC).toContain("function AdminEventsSection()");
    expect(SRC).toContain("<AdminEventsSection />");
  });

  it("fetches via getAdminEvents with the current action/target filters", () => {
    expect(SRC).toContain("getAdminEvents({");
    expect(SRC).toMatch(/action:\s*action\.trim\(\)\s*\|\|\s*undefined/);
    expect(SRC).toMatch(/target_id:\s*targetId\.trim\(\)\s*\|\|\s*undefined/);
  });

  it("submitting the filter form re-triggers the load with current input state", () => {
    expect(SRC).toContain("function applyFilters(e: React.FormEvent)");
    expect(SRC).toContain("load(actionFilter, targetFilter)");
  });

  it("distinguishes loading, error, empty, and populated states", () => {
    const body = SRC.slice(SRC.indexOf("function AdminEventsSection"));
    expect(body).toContain("adminEvents.loadError");
    expect(body).toContain("adminEvents.empty");
    expect(body).toContain("events.map((ev)");
  });

  it("never renders raw event.details in the table (mask_credentials only masks the log, not this view)", () => {
    const body = SRC.slice(
      SRC.indexOf("function AdminEventsSection"),
      SRC.indexOf("// ─── Settings section"),
    );
    expect(body).not.toMatch(/\{ev\.details\}/);
  });
});

// ─── getAdminEvents client (lib/api.ts) ────────────────────────────────────

describe("getAdminEvents — query param forwarding", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("forwards action, target_id, and limit as query params", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        json: () => Promise.resolve({ events: [] }),
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { getAdminEvents } = await import("@/lib/api");
    await getAdminEvents({ action: "transition", target_id: "sess-1", limit: 50 });

    const calledUrl = String(fetchMock.mock.calls[0]?.[0]);
    expect(calledUrl).toContain("/api/admin/events?");
    expect(calledUrl).toContain("action=transition");
    expect(calledUrl).toContain("target_id=sess-1");
    expect(calledUrl).toContain("limit=50");

    vi.unstubAllGlobals();
  });

  it("omits query params entirely when called with no filters", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({ "content-type": "application/json" }),
        json: () => Promise.resolve({ events: [] }),
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { getAdminEvents } = await import("@/lib/api");
    await getAdminEvents();

    const calledUrl = String(fetchMock.mock.calls[0]?.[0]);
    expect(calledUrl).toMatch(/\/api\/admin\/events$/);

    vi.unstubAllGlobals();
  });
});
