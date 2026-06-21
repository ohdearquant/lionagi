import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BackendManager } from "../src/backend/lifecycle.js";
import { __resetVscodeMock } from "./mocks/vscode.js";

// Drives probeHealth / _pollHealth: when `healthy` is true, GET /health
// resolves to {status:"ok"}; otherwise the fetch rejects (backend unreachable).
let healthy = false;

// A configured den.url puts start() on the attach-only path: it probes and never
// spawns a child, so the real state machine is exercised without child_process.
function makeManager(): BackendManager {
  return new BackendManager(
    () => "", // pythonPath
    () => "http://127.0.0.1:8765", // configuredUrl
    () => 8765, // port
    () => "127.0.0.1", // host
    () => "" // token
  );
}

describe("BackendManager resilience", () => {
  let bm: BackendManager | undefined;

  beforeEach(() => {
    __resetVscodeMock();
    healthy = false;
    vi.stubGlobal("fetch", async () => {
      if (!healthy) {
        throw new Error("ECONNREFUSED");
      }
      return {
        ok: true,
        json: async () => ({ status: "ok" }),
      } as unknown as Response;
    });
  });

  afterEach(() => {
    bm?.dispose();
    bm = undefined;
    vi.unstubAllGlobals();
  });

  it("attaches to a healthy configured backend", async () => {
    healthy = true;
    bm = makeManager();
    await bm.start();
    expect(bm.state).toBe("running");
  });

  it("errors when the configured backend is unreachable", async () => {
    healthy = false;
    bm = makeManager();
    await bm.start();
    expect(bm.state).toBe("error");
  });

  it("self-heals error → running when the backend becomes reachable", async () => {
    bm = makeManager();
    await bm.start();
    expect(bm.state).toBe("error");

    healthy = true;
    await bm.reconcile();
    expect(bm.state).toBe("running");
  });

  it("condemns running → error only after two consecutive missed probes", async () => {
    healthy = true;
    bm = makeManager();
    await bm.start();
    expect(bm.state).toBe("running");

    healthy = false;
    await bm.reconcile();
    expect(bm.state).toBe("running"); // one miss tolerated (hysteresis)
    await bm.reconcile();
    expect(bm.state).toBe("error"); // second consecutive miss condemns
  });

  it("does not resurrect an intentionally stopped backend", async () => {
    healthy = true;
    bm = makeManager();
    await bm.start();
    bm.stop();
    expect(bm.state).toBe("stopped");

    await bm.reconcile(); // backend is healthy, but the stop was deliberate
    expect(bm.state).toBe("stopped");
  });

  it("a Stop during an in-flight start() wins (no resurrection to running)", async () => {
    // fetch resolves healthy, but stop() runs synchronously while start() is
    // parked on the probe's await — the resumed start() must see itself
    // superseded (epoch bumped) and not write "running" over the user's Stop.
    healthy = true;
    bm = makeManager();
    const starting = bm.start(); // parks at `await probeHealth`
    bm.stop(); // bumps epoch, sets "stopped", before the probe resolves
    await starting;
    expect(bm.state).toBe("stopped");
  });
});
